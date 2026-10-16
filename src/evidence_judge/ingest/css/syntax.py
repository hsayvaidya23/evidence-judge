"""CSS text: blocks, declarations, the tracked longhands of a shorthand, and escapes."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from evidence_judge.ingest.css.colors import parse_color
from evidence_judge.ingest.css.model import words
from evidence_judge.ingest.css.values import FONT_KEYWORDS, parse_number, tokens

TRACKED: Final = words(
    "display visibility content-visibility opacity filter transform scale translate rotate zoom "
    "clip clip-path position left right top bottom inset-inline-start inset-inline-end "
    "inset-block-start inset-block-end margin-left margin-top margin-inline-start "
    "margin-block-start height max-height width max-width block-size max-block-size "
    "inline-size max-inline-size overflow-x overflow-y contain font-size color "
    "-webkit-text-fill-color text-indent background-color background-image background-clip "
    "-webkit-background-clip fill fill-opacity offset offset-path mask mask-image mask-border "
    "mask-border-source -webkit-mask -webkit-mask-image -webkit-mask-box-image"
)
"""The properties the reader applies: what removes an element, and what can hide its text."""


SHORTHANDS: Final = words(
    "all font background inset inset-inline inset-block margin margin-inline margin-block overflow"
)
"""The shorthands whose tracked longhands the reader applies. One whose value holds a
``var()`` is kept whole (as a declaration of the shorthand) until the cascade substitutes it."""


_SPECIAL = re.compile(r"[{};\"'\\]")


_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6}[ \t\n]?|.)", re.DOTALL)


_ESCAPED_NAME = re.compile(r"((?:[^:\\]|\\.)*):", re.DOTALL)


def blocks(text: str) -> Iterator[tuple[str, str | None]]:
    """The items of one level of `text`: ``(prelude, body)`` for a ``prelude { body }`` block,
    ``(declaration, None)`` for text that ends with ``;`` or the end of the level."""
    start, depth, at, quote = 0, 0, 0, ""
    opened = 0
    while (match := _SPECIAL.search(text, at)) is not None:
        char, at = match.group(), match.end()
        if quote:
            if char == "\\":
                at += 1
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "\\":
            at += 1
        elif char == "{":
            if depth == 0:
                opened = at
            depth += 1
        elif char == "}":
            if depth == 0:
                start = at
                continue
            depth -= 1
            if depth == 0:
                yield text[start : opened - 1].strip(), text[opened : at - 1]
                start = at
        elif char == ";" and depth == 0:
            yield text[start : at - 1].strip(), None
            start = at
    if depth == 0 and text[start:].strip():
        yield text[start:].strip(), None
    elif depth > 0:
        yield text[start : opened - 1].strip(), text[opened:]


_CONTENT = re.compile(r"(?:^|[;{\s])content\s*:\s*([^;}]*)", re.IGNORECASE)


_QUOTED = re.compile(r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'")


def generates_text(body: str) -> bool:
    """A declaration list sets ``content`` to text: an ``attr()``, a counter, or a string with
    a letter or digit (an icon font's private-use character and ``""`` are not text)."""
    for match in _CONTENT.finditer(body):
        value = match.group(1)
        if re.search(r"\b(?:attr|counters?)\(", value, re.IGNORECASE):
            return True
        for quoted in _QUOTED.finditer(value):
            if any(c.isalnum() for c in unescape(quoted.group(1) or quoted.group(2) or "")):
                return True
    return False


def declarations(text: str) -> Iterator[tuple[str, str | None, bool]]:
    """The tracked properties a declaration list sets: (property, value, important)."""
    for declaration, body in blocks(text):
        if body is None:
            yield from parse_declaration(declaration)


def parse_declaration(text: str) -> Iterator[tuple[str, str | None, bool]]:
    if "\\" in text and (named := _ESCAPED_NAME.match(text)) is not None:
        prop, colon, raw = unescape(named.group(1)).strip(), ":", text[named.end() :]
    else:
        prop, colon, raw = text.partition(":")
        prop = prop.strip()
    custom = prop.startswith("--")
    if not custom:
        prop = prop.lower()
    if not colon or not (custom or prop in TRACKED or prop in SHORTHANDS):
        return
    if "\\" in raw:
        raw = unescape(raw)
    value = _lowered(" ".join(raw.split()))
    important = re.search(r"!\s*important$", value)
    if important is not None:
        value = value[: important.start()].strip()
    flag = important is not None
    if custom:
        yield prop, (value or " ") if "attr(" not in value else None, flag
        return
    if not value:
        return
    if "attr(" in value:
        for name, _found in expand(prop, value):
            yield name, None, flag
        return
    if prop in SHORTHANDS and ("var(" in value or "env(" in value):
        yield prop, value, flag
        return
    yield from ((name, found, flag) for name, found in expand(prop, value))


_CUSTOM_NAME = re.compile(r"--[^\s,():;!]*")


def _lowered(value: str) -> str:
    """`value` in lower case, but for the custom property names it holds: those are
    case-sensitive."""
    out: list[str] = []
    at = 0
    for found in _CUSTOM_NAME.finditer(value):
        out.append(value[at : found.start()].lower())
        out.append(found.group())
        at = found.end()
    out.append(value[at:].lower())
    return "".join(out)


def expand(prop: str, value: str) -> Iterator[tuple[str, str]]:
    """The tracked longhands a declaration sets."""
    match prop:
        case "all":
            if value in ("initial", "unset", "inherit"):
                yield "display", "inline" if value != "inherit" else "inherit"
                yield "visibility", "visible" if value == "initial" else "inherit"
            else:
                yield "display", "revert"
                yield "visibility", "revert"
        case "font":
            yield "font-size", _font_size(value)
        case "background":
            if "url(" in value or "gradient(" in value or "image(" in value:
                yield "background-color", "image"
            else:
                yield (
                    "background-color",
                    next((t for t in tokens(value) if parse_color(t) is not None), "transparent"),
                )
            if "text" in value.split():
                yield "background-clip", "text"
        case "inset":
            top, right, bottom, left = _sides(value)
            yield from (("top", top), ("right", right), ("bottom", bottom), ("left", left))
        case "margin":
            top, _, _, left = _sides(value)
            yield from (("margin-top", top), ("margin-left", left))
        case "inset-inline" | "inset-block" | "margin-inline" | "margin-block":
            start, *rest = tokens(value) or ["0"]
            if prop.startswith("margin"):
                yield f"{prop}-start", start
            else:
                yield from ((f"{prop}-start", start), (f"{prop}-end", rest[0] if rest else start))
        case "overflow":
            first, *rest = value.split() or [""]
            yield from (("overflow-x", first), ("overflow-y", rest[0] if rest else first))
        case _:
            yield prop, value


def _sides(value: str) -> tuple[str, str, str, str]:
    values = (tokens(value) or ["0"])[:4]
    if len(values) == 1:
        return values[0], values[0], values[0], values[0]
    if len(values) == 2:
        return values[0], values[1], values[0], values[1]
    if len(values) == 3:
        return values[0], values[1], values[2], values[1]
    return values[0], values[1], values[2], values[3]


def _font_size(value: str) -> str:
    """The size a ``font`` shorthand sets ("medium" for a system font keyword, "unknown" when it
    cannot be read)."""
    if value in ("inherit", "initial", "unset"):
        return value
    for token in tokens(value):
        size = token.split("/", 1)[0]
        if size in FONT_KEYWORDS or size in ("smaller", "larger"):
            return size
        if parse_number(size) is not None and (parse_number(size) or (0, ""))[1]:
            return size
        if size == "0":
            return size
    return "medium" if len(tokens(value)) == 1 else "unknown"


def split_commas(text: str) -> list[str]:
    parts: list[str] = []
    depth, start = 0, 0
    for i, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def unescape(text: str) -> str:
    def replace_escape(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] not in "0123456789abcdefABCDEF":
            return code
        point = int(code.strip(), 16)
        return chr(point) if 0 < point <= 0x10FFFF else "�"

    return _ESCAPE.sub(replace_escape, text)
