"""Which elements of a snapshot its stylesheets hide: a small CSS reader for page text.

It reads the rules that set ``display``, ``visibility`` or ``content-visibility`` and applies
them with the cascade (``!important``, specificity, order; inline styles over sheets). Selectors
are compound selectors (tag, ``*``, ``.class``, ``#id``, attribute tests, ``:not()``, ``:root``)
joined by descendant or child combinators. A selector it cannot read (sibling combinators, any
other pseudo-class, a pseudo-element) matches nothing, as a state the snapshot does not show
would. ``@media`` blocks apply when their query holds at the snapshot's viewport and are dropped
when it cannot be read; ``@supports`` and ``@layer`` blocks apply; other at-rules and
``@import`` are ignored. Nested rules (CSS nesting) are read.

Common utility classes hide even when the stylesheet defining them is not in the recording
(`UTILITY_CSS`): the site's own rules come later and override them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final

UTILITY_CSS: Final = (
    ".hidden,.d-none,.sr-only,.visually-hidden{display:none}.invisible{visibility:hidden}"
)
"""Tailwind's and Bootstrap's hiding classes, as if their stylesheet were present: screen-reader
only text counts as hidden too, since no sighted user sees it."""
MAX_SHEET_CHARS: Final = 1024 * 1024
"""How much of each stylesheet is read."""
_PROPERTIES: Final = frozenset({"display", "visibility", "content-visibility"})
_HIDING: Final = {"display": "none", "content-visibility": "hidden"}
_INVISIBLE: Final = frozenset({"hidden", "collapse"})
_INHERITED: Final = frozenset({"inherit", "unset"})
_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_SPECIAL = re.compile(r"[{};\"'\\]")
_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6}[ \t\n]?|.)", re.DOTALL)
_IDENT = r"-?(?:[\w-]|\\[0-9a-fA-F]{1,6}[ \t\n]?|\\.)+"
_TOKEN = re.compile(
    rf"""(?P<space>\s+)|(?P<child>>)|(?P<sibling>[+~])|(?P<star>\*)
    |(?P<tag>{_IDENT})|\#(?P<id>{_IDENT})|\.(?P<cls>{_IDENT})
    |\[\s*(?P<attr>{_IDENT})\s*(?:(?P<op>[~|^$*]?=)\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'
    |(?P<bare>{_IDENT}))\s*(?P<flag>[iIsS])?\s*)?\]
    |(?P<pseudo>::?{_IDENT})(?P<args>\()?""",
    re.VERBOSE,
)
_AT_RULE = re.compile(r"@([\w-]+)\s*(.*)", re.DOTALL)
_LENGTH = re.compile(r"^(-?\d+(?:\.\d+)?)(px|em|rem)?$")
_RANGE = re.compile(
    r"^(?:([^<>=]+?)\s*([<>]=?|=))?\s*(width|height)\s*(?:([<>]=?|=)\s*([^<>=]+))?$"
)
_FLIPPED: Final = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "="}


@dataclass(frozen=True, slots=True)
class _Compound:
    tag: str | None = None
    ids: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    attrs: tuple[tuple[str, str | None, str, bool], ...] = ()
    negated: tuple[_Compound, ...] = ()
    root: bool = False

    @property
    def specificity(self) -> tuple[int, int, int]:
        ids, classes, tags = len(self.ids), len(self.classes) + len(self.attrs), int(bool(self.tag))
        classes += int(self.root)
        for n in self.negated:
            a, b, c = n.specificity
            ids, classes, tags = ids + a, classes + b, tags + c
        return ids, classes, tags


@dataclass(frozen=True, slots=True)
class _Selector:
    parts: tuple[_Compound, ...]
    """Compound selectors from left to right."""
    children: tuple[bool, ...]
    """For each combinator between parts: True for ``>``, False for a descendant."""

    @property
    def specificity(self) -> tuple[int, int, int]:
        total = (0, 0, 0)
        for part in self.parts:
            a, b, c = part.specificity
            total = (total[0] + a, total[1] + b, total[2] + c)
        return total


@dataclass(frozen=True, slots=True)
class _Rule:
    selector: _Selector
    prop: str
    value: str
    rank: tuple[int, int, tuple[int, int, int], int]
    """(important, inline, specificity, order): the larger wins."""


@dataclass(slots=True)
class Element:
    """An element as the cascade sees it: its tag, attributes and parent."""

    tag: str
    attrs: dict[str, str]
    parent: Element | None
    classes: frozenset[str] = field(init=False)
    keys: frozenset[str] = field(init=False)
    """What a rule is indexed by: its tag, ``.class`` for each class and ``#id``."""
    _lineage: frozenset[str] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.classes = frozenset((self.attrs.get("class") or "").split())
        keys = {self.tag, *(f".{c}" for c in self.classes)}
        if element_id := self.attrs.get("id"):
            keys.add(f"#{element_id}")
        self.keys = frozenset(keys)

    @property
    def ancestry(self) -> frozenset[str]:
        """The keys of every ancestor."""
        return self.parent.lineage if self.parent is not None else frozenset()

    @property
    def lineage(self) -> frozenset[str]:
        """The keys of this element and every ancestor, computed once."""
        if self._lineage is None:
            self._lineage = self.ancestry | self.keys
        return self._lineage


@dataclass(slots=True)
class _Bucket:
    """The rules whose rightmost compound has one key, split by a key an ancestor must have."""

    free: list[_Rule] = field(default_factory=list["_Rule"])
    by_ancestor: dict[str, list[_Rule]] = field(default_factory=dict[str, list["_Rule"]])


@dataclass(frozen=True, slots=True)
class Style:
    """What the cascade decides for one element."""

    removed: bool
    """``display: none`` or ``content-visibility: hidden``: the element and its subtree."""
    invisible: bool | None
    """Its ``visibility``: True hidden, False visible, None inherited from its parent."""


class StyleSheets:
    """The rules of a snapshot's stylesheets that hide or show elements, indexed by the key of
    their rightmost compound selector."""

    def __init__(self, sheets: Iterable[str], viewport: tuple[int, int] | None) -> None:
        self._viewport = viewport
        self._by_key: dict[str, _Bucket] = {}
        self._order = 0
        for sheet in (UTILITY_CSS, *sheets):
            self._read(_COMMENT.sub(" ", sheet[:MAX_SHEET_CHARS]), [])

    def style(self, element: Element) -> Style:
        best: dict[str, _Rule] = {}
        for rule in self._candidates(element):
            current = best.get(rule.prop)
            if (current is None or rule.rank > current.rank) and _matches(
                rule.selector, len(rule.selector.parts) - 1, element
            ):
                best[rule.prop] = rule
        for prop, value, important in _declarations(element.attrs.get("style") or ""):
            rank = (int(important), 1, (0, 0, 0), 0)
            current = best.get(prop)
            if current is None or rank > current.rank:
                best[prop] = _Rule(_Selector((), ()), prop, value, rank)
        removed = any(
            (rule := best.get(prop)) is not None and rule.value == value
            for prop, value in _HIDING.items()
        )
        visibility = best.get("visibility")
        invisible = None
        if visibility is not None and visibility.value not in _INHERITED:
            invisible = visibility.value in _INVISIBLE
        return Style(removed, invisible)

    def _candidates(self, element: Element) -> Iterator[_Rule]:
        for key in ("*", *element.keys):
            bucket = self._by_key.get(key)
            if bucket is None:
                continue
            yield from bucket.free
            if not bucket.by_ancestor:
                continue
            ancestry = element.ancestry
            if len(ancestry) <= len(bucket.by_ancestor):
                for found in ancestry:
                    yield from bucket.by_ancestor.get(found, ())
            else:
                for found, rules in bucket.by_ancestor.items():
                    if found in ancestry:
                        yield from rules

    def _read(self, text: str, parents: list[_Selector]) -> None:
        for prelude, body in _blocks(text):
            if body is None:
                if parents:
                    self._declare(parents, prelude)
                continue
            at_rule = _AT_RULE.match(prelude)
            if at_rule is not None:
                name, condition = at_rule.group(1).lower(), at_rule.group(2)
                if name in ("supports", "layer") or (
                    name == "media" and media_matches(condition, self._viewport)
                ):
                    self._read(body, parents)
                continue
            selectors = _selectors(prelude, parents)
            if selectors:
                self._read(body, selectors)

    def _declare(self, selectors: list[_Selector], text: str) -> None:
        for prop, value, important in _declaration(text):
            for selector in selectors:
                self._order += 1
                rule = _Rule(
                    selector, prop, value, (int(important), 0, selector.specificity, self._order)
                )
                bucket = self._by_key.setdefault(_key(selector.parts[-1]), _Bucket())
                ancestor = next(
                    (k for part in reversed(selector.parts[:-1]) if (k := _key(part)) != "*"),
                    None,
                )
                if ancestor is None:
                    bucket.free.append(rule)
                else:
                    bucket.by_ancestor.setdefault(ancestor, []).append(rule)


def _key(part: _Compound) -> str:
    """The key a compound is indexed by: its id, else its last class (a modifier is rarer
    than the base class it modifies), else its tag."""
    if part.ids:
        return f"#{part.ids[0]}"
    if part.classes:
        return f".{part.classes[-1]}"
    return part.tag or "*"


def _blocks(text: str) -> Iterator[tuple[str, str | None]]:
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


def _declarations(text: str) -> Iterator[tuple[str, str, bool]]:
    """The hiding properties a declaration list sets: (property, value, important)."""
    for declaration, body in _blocks(text):
        if body is None:
            yield from _declaration(declaration)


def _declaration(text: str) -> Iterator[tuple[str, str, bool]]:
    prop, colon, value = text.partition(":")
    prop = prop.strip().lower()
    if not colon or prop not in _PROPERTIES:
        return
    value = " ".join(value.lower().split())
    important = re.search(r"!\s*important$", value) is not None
    value = value.rsplit("!", 1)[0].strip() if important else value
    if value and "var(" not in value:
        yield prop, value, important


def _selectors(prelude: str, parents: list[_Selector]) -> list[_Selector]:
    out: list[_Selector] = []
    for text in _split_commas(prelude):
        if not parents:
            parsed = _selector(text)
            if parsed is not None:
                out.append(parsed)
            continue
        for parent in parents:
            nested = _nested(text, parent)
            if nested is not None:
                out.append(nested)
    return out


def _nested(text: str, parent: _Selector) -> _Selector | None:
    """A nested rule's selector under `parent`: a leading ``&`` stands for it (``&.open``,
    ``& > p``), and a selector without one is its descendant (``> p`` its child)."""
    if text.count("&") > 1 or ("&" in text and not text.startswith("&")):
        return None
    if text.startswith("&"):
        tail = text[1:]
        if not tail:
            return parent
        joined = tail[0] not in " \t\n>"
    else:
        tail, joined = (text if text.startswith(">") else f" {text}"), False
    own = _selector(f"*{tail}")
    if own is None:
        return None
    if joined:
        merged = _merge(parent.parts[-1], own.parts[0])
        return _Selector(
            (*parent.parts[:-1], merged, *own.parts[1:]), (*parent.children, *own.children)
        )
    return _Selector((*parent.parts, *own.parts[1:]), (*parent.children, *own.children))


def _merge(a: _Compound, b: _Compound) -> _Compound:
    return _Compound(
        a.tag or b.tag,
        a.ids + b.ids,
        a.classes + b.classes,
        a.attrs + b.attrs,
        a.negated + b.negated,
        a.root or b.root,
    )


def _split_commas(text: str) -> list[str]:
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


def _selector(text: str) -> _Selector | None:
    """A complex selector, or None when it uses anything this reader cannot match."""
    parts: list[_Compound] = []
    children: list[bool] = []
    found: dict[str, list[object]] = {}
    tag: str | None = None
    seen = False
    combinator: bool | None = None
    at = 0
    text = text.strip()
    while at < len(text):
        match = _TOKEN.match(text, at)
        if match is None:
            return None
        at = match.end()
        groups = match.groupdict()
        if groups["space"] is not None or groups["child"] is not None:
            combinator = combinator or groups["child"] is not None
            continue
        if groups["sibling"] is not None:
            return None
        if combinator is not None:
            if not seen:
                return None
            parts.append(_compound(tag, found))
            children.append(combinator)
            found, tag, combinator = {}, None, None
        seen = True
        if groups["tag"] is not None:
            tag = _unescape(groups["tag"]).upper()
        elif groups["id"] is not None:
            found.setdefault("ids", []).append(_unescape(groups["id"]))
        elif groups["cls"] is not None:
            found.setdefault("classes", []).append(_unescape(groups["cls"]))
        elif groups["attr"] is not None:
            value = groups["dq"] if groups["dq"] is not None else groups["sq"]
            if value is None and groups["bare"] is not None:
                value = _unescape(groups["bare"])
            folded = (groups["flag"] or "").lower() == "i"
            entry = (_unescape(groups["attr"]).lower(), groups["op"], value or "", folded)
            found.setdefault("attrs", []).append(entry)
        elif groups["pseudo"] is not None:
            name = groups["pseudo"].lower()
            if groups["args"] is not None:
                close = _closing(text, at)
                if close is None or name != ":not":
                    return None
                negated = [_selector(t) for t in _split_commas(text[at:close])]
                if not negated or any(n is None or len(n.parts) != 1 for n in negated):
                    return None
                found.setdefault("negated", []).extend(n.parts[0] for n in negated if n)
                at = close + 1
            elif name == ":root":
                found["root"] = [True]
            else:
                return None
    if not seen or combinator is not None:
        return None
    parts.append(_compound(tag, found))
    return _Selector(tuple(parts), tuple(children))


def _compound(tag: str | None, found: dict[str, list[object]]) -> _Compound:
    return _Compound(
        tag,
        tuple(str(i) for i in found.get("ids", [])),
        tuple(str(c) for c in found.get("classes", [])),
        tuple(found.get("attrs", [])),  # pyright: ignore[reportArgumentType]
        tuple(found.get("negated", [])),  # pyright: ignore[reportArgumentType]
        bool(found.get("root")),
    )


def _closing(text: str, at: int) -> int | None:
    depth = 1
    for i in range(at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _unescape(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] not in "0123456789abcdefABCDEF":
            return code
        point = int(code.strip(), 16)
        return chr(point) if 0 < point <= 0x10FFFF else "\ufffd"

    return _ESCAPE.sub(replace, text)


def _matches(selector: _Selector, index: int, element: Element) -> bool:
    if not _compound_matches(selector.parts[index], element):
        return False
    if index == 0:
        return True
    parent = element.parent
    if selector.children[index - 1]:
        return parent is not None and _matches(selector, index - 1, parent)
    while parent is not None:
        if _matches(selector, index - 1, parent):
            return True
        parent = parent.parent
    return False


def _compound_matches(part: _Compound, element: Element) -> bool:
    if part.tag is not None and part.tag != element.tag:
        return False
    if part.root and (element.tag != "HTML" or element.parent is not None):
        return False
    if any(i != element.attrs.get("id") for i in part.ids):
        return False
    if any(c not in element.classes for c in part.classes):
        return False
    if not all(_attr_matches(a, element.attrs) for a in part.attrs):
        return False
    return not any(_compound_matches(n, element) for n in part.negated)


def _attr_matches(test: tuple[str, str | None, str, bool], attrs: dict[str, str]) -> bool:
    name, op, wanted, folded = test
    actual = next((v for k, v in attrs.items() if k.lower() == name), None)
    if actual is None:
        return False
    if op is None:
        return True
    if folded:
        actual, wanted = actual.lower(), wanted.lower()
    match op:
        case "=":
            return actual == wanted
        case "~=":
            return wanted in actual.split()
        case "|=":
            return actual == wanted or actual.startswith(f"{wanted}-")
        case "^=":
            return bool(wanted) and actual.startswith(wanted)
        case "$=":
            return bool(wanted) and actual.endswith(wanted)
        case _:
            return bool(wanted) and wanted in actual


def media_matches(query: str, viewport: tuple[int, int] | None) -> bool:
    """Whether a media query list holds on a screen of `viewport` (CSS pixels, light colour
    scheme, a fine pointer that hovers); False when it cannot be read."""
    results = [_media_query(q, viewport) for q in _split_commas(query.lower())] or [True]
    return any(r is True for r in results)


def _media_query(query: str, viewport: tuple[int, int] | None) -> bool | None:
    negate = query.startswith("not ")
    query = query.removeprefix("not ").removeprefix("only ").strip()
    result: bool | None = True
    for term in re.split(r"\s+and\s+", query):
        term = term.strip()
        value = (
            _media_feature(term[1:-1].strip(), viewport)
            if term.startswith("(")
            else ({"all": True, "screen": True, "print": False, "speech": False}.get(term))
        )
        if value is None:
            return None
        result = result and value
    return (not result) if negate else result


_FEATURES: Final = {
    "orientation": None,
    "prefers-color-scheme": {"light": True, "dark": False},
    "prefers-reduced-motion": {"no-preference": True, "reduce": False},
    "hover": {"hover": True, "none": False},
    "any-hover": {"hover": True, "none": False},
    "pointer": {"fine": True, "coarse": False, "none": False},
    "any-pointer": {"fine": True, "coarse": False, "none": False},
}


def _media_feature(feature: str, viewport: tuple[int, int] | None) -> bool | None:
    ranged = _RANGE.match(feature)
    if ranged is not None and (ranged.group(1) or ranged.group(5)):
        low, low_op, dimension, high_op, high = ranged.groups()
        checks = [(_FLIPPED[low_op], low)] if low else []
        checks += [(high_op, high)] if high else []
        results = [_compare(dimension, op, length, viewport) for op, length in checks]
        return None if None in results else all(results)
    name, _, value = (p.strip() for p in feature.partition(":"))
    for prefix, op in (("min-", ">="), ("max-", "<="), ("", "=")):
        dimension = name.removeprefix(prefix)
        if name.startswith(prefix) and dimension in ("width", "height") and value:
            return _compare(dimension, op, value, viewport)
    if name == "orientation" and viewport is not None:
        return value == ("portrait" if viewport[1] >= viewport[0] else "landscape")
    choices = _FEATURES.get(name)
    return None if choices is None else choices.get(value)


def _compare(dimension: str, op: str, length: str, viewport: tuple[int, int] | None) -> bool | None:
    found = _LENGTH.match(length.strip())
    if viewport is None or found is None:
        return None
    pixels = float(found.group(1)) * (16 if found.group(2) in ("em", "rem") else 1)
    actual = viewport[0] if dimension == "width" else viewport[1]
    match op:
        case ">=":
            return actual >= pixels
        case "<=":
            return actual <= pixels
        case ">":
            return actual > pixels
        case "<":
            return actual < pixels
        case _:
            return actual == pixels
