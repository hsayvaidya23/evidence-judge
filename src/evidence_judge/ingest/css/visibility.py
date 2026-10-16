"""What the cascade decides for an element: whether it is removed, hidden or may be obscured,
and what its children inherit."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from evidence_judge.ingest.css.colors import parse_color
from evidence_judge.ingest.css.model import (
    BLACK,
    DISPLAY,
    HIDDEN,
    MAYBE,
    NO,
    REMOVED,
    SHOWN,
    UNSET,
    UNSURE,
    YES,
    Computed,
    Rgba,
    Rule,
    Viewport,
    words,
)
from evidence_judge.ingest.css.syntax import TRACKED
from evidence_judge.ingest.css.values import (
    FAINT,
    FAR,
    font_size_px,
    functions,
    is_far,
    length_px,
    parse_amount,
    parse_degrees,
    parse_percent,
    split_args,
    tokens,
)

_OBSCURERS: Final = words("opacity filter transform scale translate zoom clip-path")


_SIZES: Final = words(
    "height max-height width max-width block-size max-block-size inline-size max-inline-size"
)


_MARGINS: Final = words("margin-left margin-top margin-inline-start margin-block-start")


_TINY_FONT: Final = 4.0


_WIDE: Final = frozenset({UNSET, "inherit", "initial", "unset", "revert", "revert-layer"})
"""Values every property takes that never hide a box: its initial value (none of the
obscuring properties' hides anything), or its parent's."""


_INHERIT: Final = frozenset({UNSET, "inherit", "unset"})


_FREE_SIZES: Final = words(
    "auto none fit-content min-content max-content stretch -webkit-fill-available -moz-available"
)


@dataclass(frozen=True, slots=True)
class Cascade:
    best: dict[str, Rule]
    maybe: dict[str, list[Rule]]
    present: frozenset[str]
    """The properties some rule sets."""

    def values(self, prop: str) -> tuple[str | None, ...]:
        """Every value `prop` may take: the certain winner's (or UNSET), and those of the rules
        that may apply and would beat it (or that are the winner's other values)."""
        winner = self.best.get(prop)
        found: list[str | None] = [UNSET if winner is None else winner.value]
        for rule in self.maybe.get(prop, ()):
            if winner is None or rule.rank >= winner.rank:
                found.append(rule.value)
        return tuple(found)

    def sets(self, *props: str) -> bool:
        return not self.present.isdisjoint(props)


_REMOVERS: Final = words("display content-visibility")


_BOX: Final = _OBSCURERS | words("clip position left right top bottom") | _MARGINS | _SIZES


_TEXT: Final = TRACKED - _REMOVERS - _BOX - {"visibility"}


def compute(cascade: Cascade, parent: Computed, viewport: Viewport | None) -> Computed:
    present = cascade.present
    unsure = parent.unsure
    if not present.isdisjoint(_REMOVERS):
        shown = [_display_hides(v) for v in cascade.values("display")]
        contents = [_content_hides(v) for v in cascade.values("content-visibility")]
        if all(h == YES for h in shown) or all(h == YES for h in contents):
            return REMOVED
        unsure = unsure or any(h != NO for h in (*shown, *contents))
    visibility = parent.visibility
    if "visibility" in present:
        states = {_visibility(v, parent.visibility) for v in cascade.values("visibility")}
        visibility = states.pop() if len(states) == 1 else UNSURE
    if not unsure and not present.isdisjoint(_BOX):
        unsure = _obscured(cascade, viewport)
    if present.isdisjoint(_TEXT):
        if unsure == parent.unsure and visibility == parent.visibility:
            return parent
        return replace(parent, removed=False, unsure=unsure, visibility=visibility)
    font_px, font_tiny = parent.font_px, parent.font_tiny
    if cascade.sets("font-size"):
        font_px, font_tiny = _font(cascade.values("font-size"), parent, viewport)
    color, clear = parent.color, parent.clear
    if cascade.sets("color", "-webkit-text-fill-color"):
        color, clear = _paint(cascade, parent)
    background = parent.background
    if cascade.sets("background-color", "background-image"):
        found = {_background(v, parent.background) for v in cascade.values("background-color")}
        background = found.pop() if len(found) == 1 else None
        if any(v not in _WIDE and v != "none" for v in cascade.values("background-image")):
            background = None
    clip_text = parent.clip_text or any(
        v is not None and "text" in v.split()
        for p in ("background-clip", "-webkit-background-clip")
        for v in cascade.values(p)
    )
    indent_far = parent.indent_far
    if cascade.sets("text-indent"):
        indent_far = any(
            parent.indent_far if v in _INHERIT else _indent_far(v, viewport)
            for v in cascade.values("text-indent")
        )
    return Computed(
        False,
        unsure,
        visibility,
        font_px,
        font_tiny,
        color,
        clear,
        background,
        clip_text,
        indent_far,
        _inherits(cascade, "fill", parent.fill_none, _fill_none),
        _inherits(cascade, "fill-opacity", parent.fill_faint, _faint),
    )


def _inherits(cascade: Cascade, prop: str, inherited: bool, test: Callable[[str], bool]) -> bool:
    """Whether an inherited property may take a value `test` holds for."""
    if not cascade.sets(prop):
        return inherited
    return any(
        inherited if v in (UNSET, "inherit", "unset") else v is None or test(v)
        for v in cascade.values(prop)
    )


def _fill_none(value: str) -> bool:
    if value in ("none", "transparent"):
        return True
    rgba = parse_color(value)
    return rgba is not None and rgba[3] <= FAINT


def _faint(value: str) -> bool:
    amount = parse_amount(value)
    return amount is None or amount <= FAINT


def _font(
    values: tuple[str | None, ...], parent: Computed, viewport: Viewport | None
) -> tuple[float | None, bool]:
    """The smallest font size an element may have (None when not known), and whether it may be
    too small to read."""
    sizes: list[float | None] = []
    tiny = False
    for value in values:
        if value in _INHERIT:
            sizes.append(parent.font_px)
            tiny = tiny or parent.font_tiny
            continue
        if value in ("revert", "revert-layer"):
            value = "medium"
        px = None if value is None else font_size_px(value, parent.font_px, viewport)
        sizes.append(px)
        tiny = tiny or px is None or px < _TINY_FONT
    known = [px for px in sizes if px is not None]
    return (min(known) if len(known) == len(sizes) else None), tiny


def _indent_far(value: str | None, viewport: Viewport | None) -> bool:
    if value is None:
        return True
    if value in _WIDE:
        return False
    first = (tokens(value) or [value])[0]
    share = parse_percent(first)
    if share is not None:
        return share <= -100
    px = length_px(first, viewport, 16.0, viewport[0] if viewport else None)
    return px is None or is_far(px, 0)


def _display_hides(value: str | None) -> int:
    if value is None:
        return MAYBE
    if value == UNSET:
        return NO
    if value == "none":
        return YES
    tokens = value.split()
    if tokens and all(t in DISPLAY for t in tokens):
        return NO
    return MAYBE


def _content_hides(value: str | None) -> int:
    if value is None:
        return MAYBE
    if value == "hidden":
        return YES
    if value in (UNSET, "visible", "auto", "inherit", "initial", "unset"):
        return NO
    return MAYBE


def _visibility(value: str | None, inherited: int) -> int:
    if value in (UNSET, "inherit", "unset"):
        return inherited
    if value in ("visible", "initial", "revert"):
        return SHOWN
    if value in ("hidden", "collapse"):
        return HIDDEN
    return UNSURE


def _obscured(cascade: Cascade, viewport: Viewport | None) -> bool:
    """Whether this element's box may be invisible or off-screen. A value the reader cannot
    read may."""
    for prop in _OBSCURERS & cascade.present:
        if any(
            v is None or (v not in _WIDE and _hides(prop, v, viewport))
            for v in cascade.values(prop)
        ):
            return True
    positions = cascade.values("position") if cascade.sets("position") else (UNSET,)
    placed = any(v is None or v in ("absolute", "fixed") for v in positions)
    moved = placed or any(v in ("relative", "sticky") for v in positions)
    if placed and any(v is None or _clip_rect_hides(v) for v in cascade.values("clip")):
        return True
    width, height = viewport if viewport is not None else (None, None)
    if moved and cascade.sets("left", "right", "top", "bottom"):
        for prop, sign, side in (
            ("left", 0, width),
            ("right", 0, width),
            ("top", -1, height),
            ("bottom", 1, height),
        ):
            if any(_offset_far(v, viewport, sign, side) for v in cascade.values(prop)):
                return True
    for prop in _MARGINS & cascade.present:
        if any(_offset_far(v, viewport, -1, width) for v in cascade.values(prop)):
            return True
    if cascade.sets(*_SIZES) and cascade.sets("overflow-x", "overflow-y"):
        clipped = any(
            v is None or v in ("hidden", "clip")
            for p in ("overflow-x", "overflow-y")
            for v in cascade.values(p)
        )
        sizes = _SIZES & cascade.present
        if clipped and any(_tiny(v, viewport) for p in sizes for v in cascade.values(p)):
            return True
    return False


def _offset_far(value: str | None, viewport: Viewport | None, sign: int, side: int | None) -> bool:
    """Whether an offset may move a box off-screen: a percentage counts as one of the
    viewport's `side`, and a value the reader cannot read may."""
    if value is None:
        return True
    if value in _WIDE or value == "auto":
        return False
    px = length_px(value, viewport, 16.0, side)
    return px is None or is_far(px, sign)


def _tiny(value: str | None, viewport: Viewport | None) -> bool:
    """Whether a size may be at most a pixel."""
    if value is None:
        return True
    if value in _WIDE or value in _FREE_SIZES or value.startswith("fit-content("):
        return False
    share = parse_percent(value)
    if share is not None:
        return share == 0
    px = length_px(value, viewport, 16.0, viewport[1] if viewport else None)
    return px is None or px <= 1


def _hides(prop: str, value: str, viewport: Viewport | None) -> bool:
    """Whether an obscuring property's value may hide the box (or cannot be read)."""
    if value == "none" and prop not in ("opacity", "zoom"):
        return False
    if prop in ("opacity", "zoom"):
        if prop == "zoom" and value in ("normal", "reset"):
            return False
        amount = parse_amount(value)
        if amount is None:
            return True
        return amount <= FAINT and not (prop == "zoom" and amount == 0)
    if prop == "filter":
        found = functions(value)
        if not found:
            return True
        for name, args in found:
            if name == "opacity":
                amount = parse_amount(args) if args else 1.0
                if amount is None or amount <= FAINT:
                    return True
        return False
    if prop == "scale":
        amounts = [parse_amount(t) for t in tokens(value)]
        return not amounts or any(a is None or abs(a) <= FAINT for a in amounts)
    if prop == "translate":
        return any(_moves_far(t, viewport) for t in tokens(value)[:2])
    if prop == "clip-path":
        return _clip_path_hides(value)
    return _transform_hides(value, viewport)


def _moves_far(value: str, viewport: Viewport | None) -> bool:
    """Whether a translation may move a box off-screen: by its own size or more (100%), or far;
    a value the reader cannot read may."""
    fixed = length_px(value, viewport, 16.0, 0.0)
    scaled = length_px(value, viewport, 16.0, 100.0)
    if fixed is None or scaled is None:
        return True
    return abs(scaled - fixed) >= 100 or is_far(fixed, 0)


def _transform_hides(value: str, viewport: Viewport | None) -> bool:
    found = functions(value)
    if not found:
        return True
    for name, args in found:
        values = split_args(args)
        if name in ("scale", "scalex", "scaley", "scale3d"):
            amounts = [parse_amount(v) for v in values]
            if any(a is None or abs(a) <= FAINT for a in amounts):
                return True
        elif name in ("translate", "translatex", "translatey", "translate3d"):
            if any(_moves_far(v, viewport) for v in values[:2]):
                return True
        elif name == "matrix":
            numbers = [parse_amount(v) for v in values]
            if len(numbers) != 6 or any(n is None for n in numbers):
                return True
            a, b, c, d, e, f = (n or 0.0 for n in numbers)
            if abs(a * d - b * c) <= FAINT**2 or max(abs(e), abs(f)) >= FAR:
                return True
        elif name in ("rotatex", "rotatey"):
            angle = parse_degrees(values[0])
            if angle is None or abs((angle % 180) - 90) < 1:
                return True
    return False


def _clip_rect_hides(value: str) -> bool:
    if value in _WIDE or value == "auto":
        return False
    found = functions(value)
    if not found or found[0][0] != "rect":
        return True
    sides = [s for s in re.split(r"[\s,]+", found[0][1]) if s]
    return any(s != "auto" for s in sides)


def _clip_path_hides(value: str) -> bool:
    found = functions(value)
    if found is None:
        return True
    if not found:
        return False
    name, args = found[0]
    if name == "inset":
        sides = tokens(args.split(" round ")[0])[:4]
        percents = [p for side in sides if (p := parse_percent(side)) is not None]
        if not sides or len(percents) != len(sides):
            return any((px := length_px(side, None)) is None or is_far(px, 0) for side in sides)
        top, right, bottom, left = _box(percents)
        return top + bottom >= 100 or left + right >= 100
    if name in ("circle", "ellipse"):
        radii = tokens(args.split(" at ")[0])
        if any(
            r not in ("closest-side", "farthest-side", "closest-corner", "farthest-corner")
            and parse_percent(r) is None
            and length_px(r, None) is None
            for r in radii
        ):
            return True
        return bool(radii) and all(parse_amount(r) == 0 or parse_percent(r) == 0 for r in radii)
    if name == "polygon":
        points = {" ".join(p.split()) for p in split_args(args)}
        return len(points) == 1
    return False


def _box(values: list[float]) -> tuple[float, float, float, float]:
    if len(values) == 1:
        return values[0], values[0], values[0], values[0]
    if len(values) == 2:
        return values[0], values[1], values[0], values[1]
    if len(values) == 3:
        return values[0], values[1], values[2], values[1]
    return values[0], values[1], values[2], values[3]


def _paint(cascade: Cascade, parent: Computed) -> tuple[Rgba | None, bool]:
    """The text colour and whether it may be transparent, or cannot be read."""
    colors: set[Rgba | None] = set()
    clear = False
    for value in cascade.values("color"):
        if value in _INHERIT:
            colors.add(parent.color)
            clear = clear or parent.clear
            continue
        if value in ("initial", "revert", "revert-layer"):
            rgba: Rgba | None = BLACK
        else:
            rgba = None if value is None else parse_color(value, parent.color)
        colors.add(rgba)
        clear = clear or rgba is None or rgba[3] <= FAINT
    for value in cascade.values("-webkit-text-fill-color"):
        if value in _WIDE or value == "currentcolor":
            continue
        rgba = None if value is None else parse_color(value)
        clear = clear or rgba is None or rgba[3] <= FAINT
    color = colors.pop() if len(colors) == 1 else None
    return color, clear


def _background(value: str | None, behind: Rgba | None) -> Rgba | None:
    if value in (UNSET, "transparent", "initial", "unset"):
        return behind
    if value is None or value == "inherit":
        return None
    rgba = parse_color(value)
    if rgba is None:
        return None
    if rgba[3] <= FAINT:
        return behind
    return rgba if rgba[3] >= 1 else None
