"""``@supports`` and ``@media`` conditions: whether they hold, or None when the reader cannot
tell."""

from __future__ import annotations

import re
from typing import Final

from evidence_judge.ingest.css.model import DISPLAY, Viewport
from evidence_judge.ingest.css.selector import foreign
from evidence_judge.ingest.css.syntax import split_commas
from evidence_judge.ingest.css.values import closing_paren, length_px

_RANGE = re.compile(
    r"^(?:([^<>=]+?)\s*([<>]=?|=))?\s*(width|height)\s*(?:([<>]=?|=)\s*([^<>=]+))?$"
)


_FLIPPED: Final = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "="}


def supports(condition: str) -> bool | None:
    """Whether an ``@supports`` condition holds in Chromium; None when the reader does not
    know."""
    text = condition.strip()
    lowered = text.lower()
    if lowered.startswith("not ") or lowered.startswith("not("):
        inner = supports(text[3:])
        return None if inner is None else not inner
    for joiner in (" or ", " and "):
        pieces = _split_top(text, joiner)
        if len(pieces) > 1:
            results = [supports(p) for p in pieces]
            if joiner == " or ":
                return True if True in results else (None if None in results else False)
            return False if False in results else (None if None in results else True)
    if text.startswith("(") and closing_paren(text, 1) == len(text) - 1:
        inner = text[1:-1].strip()
        prop, colon, value = inner.partition(":")
        if not colon or "(" in prop:
            return supports(inner)
        return _supported(prop.strip().lower(), " ".join(value.lower().split()))
    return None


def _supported(prop: str, value: str) -> bool | None:
    if foreign(prop) or foreign(value):
        return False
    if prop == "display":
        tokens = value.split()
        return True if tokens and all(t in DISPLAY for t in tokens) else None
    return None


def _split_top(text: str, joiner: str) -> list[str]:
    pieces: list[str] = []
    depth, start, i = 0, 0, 0
    lowered = text.lower()
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and lowered.startswith(joiner, i):
            pieces.append(text[start:i])
            i += len(joiner)
            start = i
            continue
        i += 1
    pieces.append(text[start:])
    return pieces


def media_holds(query: str, viewport: Viewport | None) -> bool | None:
    """Whether a media query list holds on a screen of `viewport` (CSS pixels, light colour
    scheme, a fine pointer that hovers); None when the reader cannot tell."""
    results = [_media_query(q, viewport) for q in split_commas(query.lower())] or [True]
    if True in results:
        return True
    return None if None in results else False


def _media_query(query: str, viewport: Viewport | None) -> bool | None:
    negate = query.startswith("not ")
    query = query.removeprefix("not ").removeprefix("only ").strip()
    result: bool | None = True
    for term in re.split(r"\s+and\s+", query):
        term = term.strip()
        value = (
            _media_feature(term[1:-1].strip(), viewport)
            if term.startswith("(")
            else ({"all": True, "screen": True}.get(term, False))
        )
        if value is False:
            result = False
        elif value is None and result is not False:
            result = None
    if result is None:
        return None
    return (not result) if negate else result


_FEATURES: Final = {
    "prefers-color-scheme": {"light": True, "dark": False},
    "prefers-reduced-motion": {"no-preference": True, "reduce": False},
    "prefers-contrast": {"no-preference": True, "more": False, "less": False, "custom": False},
    "forced-colors": {"none": True, "active": False},
    "inverted-colors": {"none": True, "inverted": False},
    "scripting": {"enabled": True, "none": False, "initial-only": False},
    "hover": {"hover": True, "none": False},
    "any-hover": {"hover": True, "none": False},
    "pointer": {"fine": True, "coarse": False, "none": False},
    "any-pointer": {"fine": True, "coarse": False, "none": False},
    "display-mode": {"browser": True},
    "update": {"fast": True, "slow": False, "none": False},
}


def _media_feature(feature: str, viewport: Viewport | None) -> bool | None:
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
    if name == "orientation":
        if viewport is None:
            return None
        return value == ("portrait" if viewport[1] >= viewport[0] else "landscape")
    if name == "color" and not value:
        return True
    if name in ("monochrome", "grid") and not value:
        return False
    choices = _FEATURES.get(name)
    if choices is None:
        return None
    return choices.get(value, None) if value else True


def _compare(dimension: str, op: str, length: str, viewport: Viewport | None) -> bool | None:
    pixels = length_px(length.strip(), None)
    if viewport is None or pixels is None:
        return None
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
