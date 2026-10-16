"""CSS text: blocks, declarations, the tracked longhands of a shorthand, and escapes."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from evidence_judge.ingest.css.model import words
from evidence_judge.ingest.css.values import FONT_KEYWORDS, parse_color, parse_number

TRACKED: Final = words(
    "display visibility content-visibility opacity filter transform scale translate zoom "
    "clip clip-path position left right top bottom margin-left margin-top "
    "margin-inline-start margin-block-start height max-height width max-width block-size "
    "max-block-size inline-size max-inline-size overflow-x overflow-y font-size color "
    "-webkit-text-fill-color text-indent background-color background-clip "
    "-webkit-background-clip fill fill-opacity"
)
"""The properties the reader applies: what removes an element, and what can hide its text."""


_SHORTHANDS: Final = frozenset({"all", "font", "background", "inset", "margin", "overflow"})


_SPECIAL = re.compile(r"[{};\"'\\]")


_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6}[ \t\n]?|.)", re.DOTALL)


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
    prop, colon, raw = text.partition(":")
    prop = prop.strip().lower()
    if not colon or (prop not in TRACKED and prop not in _SHORTHANDS):
        return
    value = " ".join(raw.lower().split())
    important = re.search(r"!\s*important$", value)
    if important is not None:
        value = value[: important.start()].strip()
    if "\\" in value:
        value = " ".join(unescape(value).split())
    if not value:
        return
    readable = not any(f in value for f in ("var(", "env(", "attr("))
    flag = important is not None
    for name, found in _expand(prop, value):
        yield name, (found if readable else None), flag


def _expand(prop: str, value: str) -> Iterator[tuple[str, str]]:
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
                    next((t for t in _tokens(value) if parse_color(t) is not None), "transparent"),
                )
            if "text" in value.split():
                yield "background-clip", "text"
        case "inset":
            top, right, bottom, left = _sides(value)
            yield from (("top", top), ("right", right), ("bottom", bottom), ("left", left))
        case "margin":
            top, _, _, left = _sides(value)
            yield from (("margin-top", top), ("margin-left", left))
        case "overflow":
            first, *rest = value.split() or [""]
            yield from (("overflow-x", first), ("overflow-y", rest[0] if rest else first))
        case _:
            yield prop, value


def _tokens(value: str) -> list[str]:
    """Space-separated tokens, keeping functions whole."""
    out: list[str] = []
    depth, start = 0, 0
    for i, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == " " and depth == 0:
            out.append(value[start:i])
            start = i + 1
    out.append(value[start:])
    return [t for t in out if t]


def _sides(value: str) -> tuple[str, str, str, str]:
    tokens = _tokens(value) or ["0"]
    values = [*tokens[:4]]
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
    for token in _tokens(value):
        size = token.split("/", 1)[0]
        if size in FONT_KEYWORDS or size in ("smaller", "larger"):
            return size
        if parse_number(size) is not None and (parse_number(size) or (0, ""))[1]:
            return size
        if size == "0":
            return size
    return "medium" if len(_tokens(value)) == 1 else "unknown"


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


def closing_paren(text: str, at: int) -> int | None:
    depth = 1
    for i in range(at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def unescape(text: str) -> str:
    def replace_escape(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] not in "0123456789abcdefABCDEF":
            return code
        point = int(code.strip(), 16)
        return chr(point) if 0 < point <= 0x10FFFF else "�"

    return _ESCAPE.sub(replace_escape, text)
