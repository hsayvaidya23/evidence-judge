"""CSS values: functions, math functions, numbers, angles, lengths and font sizes."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Final

from evidence_judge.ingest.css.model import UNSET, Viewport

_NUMBER = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-z%]*)$")


_UNITS: Final = {
    "px": 1.0,
    "pt": 4 / 3,
    "pc": 16.0,
    "in": 96.0,
    "cm": 96 / 2.54,
    "mm": 96 / 25.4,
    "q": 96 / 101.6,
    "rem": 16.0,
}


FAR: Final = 1000.0
"""How far (CSS pixels) an offset, margin or indent moves text before the reader is unsure it
is on screen."""


FAINT: Final = 0.05
"""An opacity or scale at or below which the reader is unsure text can be seen."""


MAX_NESTING: Final = 32
"""How deeply functions, ``var()`` fallbacks, selectors and blocks may nest before the reader
gives up on them (and is unsure)."""


def too_deep(text: str, limit: int = MAX_NESTING) -> bool:
    """Whether parentheses nest more than `limit` deep in `text`."""
    if text.count("(") <= limit:
        return False
    depth = 0
    for match in _PARENS.finditer(text):
        depth += 1 if match.group() == "(" else -1
        if depth > limit:
            return True
    return False


_PARENS = re.compile(r"[()]")


_CALL = re.compile(r"(-?[a-z][a-z0-9-]*)?\(")


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


def functions(value: str) -> list[tuple[str, str]] | None:
    """The top-level function calls in `value` as (name, arguments), nested calls kept whole in
    the arguments; None when a parenthesis is not closed."""
    found: list[tuple[str, str]] = []
    at = 0
    while (call := _CALL.search(value, at)) is not None:
        close = closing_paren(value, call.end())
        if close is None:
            return None
        found.append((call.group(1) or "", value[call.end() : close].strip()))
        at = close + 1
    return found


def split_args(args: str) -> list[str]:
    """Comma-separated arguments, keeping nested calls whole."""
    parts: list[str] = []
    depth, start = 0, 0
    for i, char in enumerate(args):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(args[start:i].strip())
            start = i + 1
    parts.append(args[start:].strip())
    return parts


def tokens(value: str) -> list[str]:
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


Leaf = Callable[[float, str], float | None]
"""What a number with a unit (``""`` for none) is in the unit a math function computes in, or
None when the reader cannot tell."""


MATH: Final = frozenset({"calc", "min", "max", "clamp", "abs", "-webkit-calc"})


_MATH_TOKEN = re.compile(
    r"\s*(?:(\d+\.?\d*(?:e[+-]?\d+)?|\.\d+(?:e[+-]?\d+)?)([a-z%]*)|([a-z-]+)\(|([-+*/(),]))"
)


def evaluate(value: str, leaf: Leaf) -> float | None:
    """A ``calc()``, ``min()``, ``max()``, ``clamp()`` or ``abs()`` expression, its numbers
    converted by `leaf`; None when it is not one or the reader cannot evaluate it."""
    found = functions(value)
    if found is None or len(found) != 1 or found[0][0] not in MATH or not value.endswith(")"):
        return None
    parsed: list[tuple[str, str]] = []
    at = depth = 0
    while at < len(value):
        match = _MATH_TOKEN.match(value, at)
        if match is None:
            if value[at:].strip():
                return None
            break
        number, unit, call, symbol = match.groups()
        if number is not None:
            parsed.append(("n", f"{number} {unit}"))
        elif call is not None:
            parsed.append(("f", call))
            depth += 1
        else:
            parsed.append(("s", symbol))
            depth += {"(": 1, ")": -1}.get(symbol, 0)
        if depth > MAX_NESTING:
            return None
        at = match.end()
    try:
        result, rest = _sum(parsed, 0, leaf)
    except (ArithmeticError, ValueError, IndexError):
        return None
    return result if rest == len(parsed) and math.isfinite(result) else None


_Parsed = list[tuple[str, str]]


def _sum(parsed: _Parsed, at: int, leaf: Leaf) -> tuple[float, int]:
    total, at = _product(parsed, at, leaf)
    while at < len(parsed) and parsed[at] in (("s", "+"), ("s", "-")):
        sign = parsed[at][1]
        right, at = _product(parsed, at + 1, leaf)
        total = total + right if sign == "+" else total - right
    return total, at


def _product(parsed: _Parsed, at: int, leaf: Leaf) -> tuple[float, int]:
    total, at = _unary(parsed, at, leaf)
    while at < len(parsed) and parsed[at] in (("s", "*"), ("s", "/")):
        operator = parsed[at][1]
        right, at = _unary(parsed, at + 1, leaf)
        total = total * right if operator == "*" else total / right
    return total, at


def _unary(parsed: _Parsed, at: int, leaf: Leaf) -> tuple[float, int]:
    sign = 1.0
    while parsed[at] in (("s", "+"), ("s", "-")):
        sign = -sign if parsed[at][1] == "-" else sign
        at += 1
    found, at = _operand(parsed, at, leaf)
    return sign * found, at


def _operand(parsed: _Parsed, at: int, leaf: Leaf) -> tuple[float, int]:
    kind, text = parsed[at]
    if kind == "n":
        number, unit = text.split(" ")
        found = float(number) if not unit else leaf(float(number), unit)
        if found is None:
            raise ValueError(text)
        return found, at + 1
    if kind == "s" and text == "(":
        found, at = _sum(parsed, at + 1, leaf)
        if parsed[at] != ("s", ")"):
            raise ValueError(text)
        return found, at + 1
    if kind != "f" or text not in MATH:
        raise ValueError(text)
    args: list[float] = []
    at += 1
    while True:
        found, at = _sum(parsed, at, leaf)
        args.append(found)
        if parsed[at] == ("s", ","):
            at += 1
            continue
        if parsed[at] != ("s", ")"):
            raise ValueError(text)
        break
    at += 1
    if text in ("calc", "-webkit-calc", "abs") and len(args) == 1:
        return (abs(args[0]) if text == "abs" else args[0]), at
    if text == "min":
        return min(args), at
    if text == "max":
        return max(args), at
    if text == "clamp" and len(args) == 3:
        return max(args[0], min(args[1], args[2])), at
    raise ValueError(text)


def parse_number(value: str) -> tuple[float, str] | None:
    found = _NUMBER.match(value.strip())
    if found is None:
        return None
    number = float(found.group(1))
    return (number, found.group(2)) if math.isfinite(number) else None


def parse_amount(value: str) -> float | None:
    """A plain number, or a percentage as a fraction."""
    found = parse_number(value)
    if found is None:
        return evaluate(value, lambda n, u: n / 100 if u == "%" else None)
    number, unit = found
    if unit == "%":
        return number / 100
    return number if not unit else None


def parse_percent(value: str) -> float | None:
    found = parse_number(value)
    if found is None:
        return None
    number, unit = found
    if unit == "%" or (number == 0 and not unit):
        return number
    return None


_ANGLES: Final = {"deg": 1.0, "turn": 360.0, "rad": 57.29578, "grad": 0.9}


def parse_degrees(value: str) -> float | None:
    found = parse_number(value)
    if found is None:
        return evaluate(value, lambda n, u: n * _ANGLES[u] if u in _ANGLES else None)
    number, unit = found
    if not unit and number == 0:
        return 0.0
    return number * _ANGLES[unit] if unit in _ANGLES else None


def length_px(
    value: str,
    viewport: Viewport | None,
    font: float | None = 16.0,
    percent: float | None = None,
) -> float | None:
    """A length in CSS pixels, `percent` the pixels 100% stands for; None when it is relative
    to something unknown."""

    def leaf(number: float, unit: str) -> float | None:
        return _unit_px(number, unit, viewport, font, percent)

    found = parse_number(value)
    if found is None:
        return evaluate(value, leaf)
    number, unit = found
    if not unit:
        return number if number == 0 else None
    px = leaf(number, unit)
    return px if px is None or math.isfinite(px) else None


def _unit_px(
    number: float,
    unit: str,
    viewport: Viewport | None,
    font: float | None,
    percent: float | None,
) -> float | None:
    if unit in _UNITS:
        return number * _UNITS[unit]
    if unit == "%":
        return None if percent is None else number * percent / 100
    if unit in ("em", "ex", "ch", "cap", "ic", "lh") and font is not None:
        return number * font * (0.5 if unit in ("ex", "ch") else 1.0)
    if unit == "rlh":
        return number * 16.0
    base = unit[1:] if unit[:1] in ("s", "l", "d") and unit[1:] in _VIEWPORT_UNITS else unit
    if base in _VIEWPORT_UNITS and viewport is not None:
        width, height = viewport
        side = {"vw": width, "vh": height, "vmin": min(viewport), "vmax": max(viewport)}
        return number * side[_VIEWPORT_UNITS[base]] / 100
    return None


_VIEWPORT_UNITS: Final = {
    "vw": "vw",
    "vi": "vw",
    "vh": "vh",
    "vb": "vh",
    "vmin": "vmin",
    "vmax": "vmax",
}


def far_offset(value: str | None, viewport: Viewport | None, sign: int) -> bool:
    """Whether an offset of `value` certainly moves a box off-screen: `sign` -1 only counts
    large negative offsets, 1 large positive ones, 0 either."""
    if value is None or value == UNSET:
        return False
    px = length_px(value, viewport)
    return px is not None and is_far(px, sign)


def is_far(px: float, sign: int, far: float = FAR) -> bool:
    return px <= -far if sign < 0 else (px >= far if sign > 0 else abs(px) >= far)


def tiny_size(value: str | None, viewport: Viewport | None) -> bool:
    if value is None or value == UNSET:
        return False
    if parse_percent(value) == 0:
        return True
    px = length_px(value, viewport)
    return px is not None and px <= 1


FONT_KEYWORDS: Final = {
    "xx-small": 9.0,
    "x-small": 10.0,
    "small": 13.0,
    "medium": 16.0,
    "large": 18.0,
    "x-large": 24.0,
    "xx-large": 32.0,
    "xxx-large": 48.0,
    "initial": 16.0,
}


def font_size_px(
    value: str | None, inherited: float | None, viewport: Viewport | None
) -> float | None:
    if value is None:
        return None
    if value in (UNSET, "inherit", "unset"):
        return inherited
    if value in FONT_KEYWORDS:
        return FONT_KEYWORDS[value]
    if value in ("smaller", "larger"):
        return None if inherited is None else inherited * (1.2 if value == "larger" else 1 / 1.2)
    if (share := parse_percent(value)) is not None:
        return None if inherited is None else inherited * share / 100
    return length_px(value, viewport, inherited, inherited)
