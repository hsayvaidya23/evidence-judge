"""What the cascade decides for an element: whether it is removed, hidden or may be obscured,
and what its children inherit."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

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
    FUNCTION,
    far_offset,
    font_size_px,
    parse_amount,
    parse_color,
    parse_degrees,
    parse_percent,
    text_indent_far,
    tiny_size,
)

_OBSCURERS: Final = words("opacity filter transform scale translate zoom clip-path")


_SIZES: Final = words(
    "height max-height width max-width block-size max-block-size inline-size max-inline-size"
)


_MARGINS: Final = words("margin-left margin-top margin-inline-start margin-block-start")


_TINY_FONT: Final = 4.0


@dataclass(frozen=True, slots=True)
class Cascade:
    best: dict[str, Rule]
    maybe: dict[str, list[Rule]]
    present: frozenset[str]
    """The properties some rule sets."""

    def values(self, prop: str) -> tuple[str | None, ...]:
        """Every value `prop` may take: the certain winner's (or UNSET), and those of the rules
        that may apply and would beat it."""
        winner = self.best.get(prop)
        found: list[str | None] = [UNSET if winner is None else winner.value]
        for rule in self.maybe.get(prop, ()):
            if winner is None or rule.rank > winner.rank:
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
        sizes = [font_size_px(v, parent.font_px, viewport) for v in cascade.values("font-size")]
        font_tiny = any(
            (px is not None and px < _TINY_FONT)
            or (px is None and v in (UNSET, "inherit", "unset") and parent.font_tiny)
            for px, v in zip(sizes, cascade.values("font-size"), strict=True)
        )
        font_px = sizes[0] if len(set(sizes)) == 1 else None
    color, clear = parent.color, parent.clear
    if cascade.sets("color", "-webkit-text-fill-color"):
        color, clear = _paint(cascade, parent)
    background = parent.background
    if cascade.sets("background-color"):
        found = {_background(v, parent.background) for v in cascade.values("background-color")}
        background = found.pop() if len(found) == 1 else None
    clip_text = parent.clip_text or any(
        v is not None and "text" in v.split()
        for p in ("background-clip", "-webkit-background-clip")
        for v in cascade.values(p)
    )
    indent_far = parent.indent_far
    if cascade.sets("text-indent"):
        indent_far = any(
            parent.indent_far if v in (UNSET, "inherit", "unset") else text_indent_far(v, viewport)
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
    return amount is not None and amount <= FAINT


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
    """Whether this element's box may be invisible or off-screen."""
    for prop in _OBSCURERS & cascade.present:
        if any(v is not None and _hides(prop, v) for v in cascade.values(prop)):
            return True
    positions = cascade.values("position") if cascade.sets("position") else (UNSET,)
    placed = any(v in ("absolute", "fixed") for v in positions)
    moved = placed or any(v in ("relative", "sticky") for v in positions)
    if placed and any(v is not None and _clip_rect_hides(v) for v in cascade.values("clip")):
        return True
    if moved and cascade.sets("left", "right", "top", "bottom"):
        for prop, sign in (("left", 0), ("right", 0), ("top", -1), ("bottom", 1)):
            if any(far_offset(v, viewport, sign) for v in cascade.values(prop)):
                return True
    for prop in _MARGINS & cascade.present:
        if any(far_offset(v, viewport, -1) for v in cascade.values(prop)):
            return True
    if cascade.sets(*_SIZES) and cascade.sets("overflow-x", "overflow-y"):
        clipped = any(
            v in ("hidden", "clip") for p in ("overflow-x", "overflow-y") for v in cascade.values(p)
        )
        sizes = _SIZES & cascade.present
        if clipped and any(tiny_size(v, viewport) for p in sizes for v in cascade.values(p)):
            return True
    return False


def _hides(prop: str, value: str) -> bool:
    if prop in ("opacity", "zoom"):
        amount = parse_amount(value)
        if prop == "zoom" and amount == 0:
            return False
        return amount is not None and amount <= FAINT
    if prop == "filter":
        return any(
            name == "opacity" and (a := parse_amount(args)) is not None and a <= FAINT
            for name, args in FUNCTION.findall(value)
        )
    if prop == "scale":
        return any((a := parse_amount(t)) is not None and abs(a) <= FAINT for t in value.split())
    if prop == "translate":
        return any(far_offset(t, None, 0) for t in value.split())
    if prop == "clip-path":
        return _clip_path_hides(value)
    return _transform_hides(value)


def _transform_hides(value: str) -> bool:
    for name, args in FUNCTION.findall(value):
        values = [a.strip() for a in args.split(",")]
        if name in ("scale", "scalex", "scaley", "scale3d"):
            if any((a := parse_amount(v)) is not None and abs(a) <= FAINT for v in values):
                return True
        elif name in ("translate", "translatex", "translatey", "translate3d"):
            if any(far_offset(v, None, 0) for v in values):
                return True
        elif name == "matrix" and len(values) == 6:
            numbers = [parse_amount(v) for v in values]
            if all(n is not None for n in numbers):
                a, b, c, d, e, f = (n or 0.0 for n in numbers)
                if abs(a * d - b * c) <= FAINT**2 or max(abs(e), abs(f)) >= FAR:
                    return True
        elif name in ("rotatex", "rotatey"):
            angle = parse_degrees(values[0])
            if angle is not None and abs((angle % 180) - 90) < 1:
                return True
    return False


def _clip_rect_hides(value: str) -> bool:
    found = FUNCTION.search(value)
    if found is None or found.group(1) != "rect":
        return False
    sides = [s for s in re.split(r"[\s,]+", found.group(2).strip()) if s]
    return any(s != "auto" for s in sides)


def _clip_path_hides(value: str) -> bool:
    found = FUNCTION.search(value)
    if found is None:
        return False
    name, args = found.group(1), found.group(2)
    if name == "inset":
        sides = args.split(" round ")[0].split()[:4]
        percents = [p for side in sides if (p := parse_percent(side)) is not None]
        if not sides or len(percents) != len(sides):
            return any(far_offset(side, None, 0) for side in sides)
        top, right, bottom, left = _box(percents)
        return top + bottom >= 100 or left + right >= 100
    if name in ("circle", "ellipse"):
        radii = args.split(" at ")[0].split()
        return bool(radii) and all(parse_amount(r) == 0 or parse_percent(r) == 0 for r in radii)
    if name == "polygon":
        points = {" ".join(p.split()) for p in args.split(",")}
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
    colors: set[Rgba | None] = set()
    clear = False
    for value in cascade.values("color"):
        if value in (UNSET, "inherit", "unset"):
            colors.add(parent.color)
            clear = clear or parent.clear
            continue
        rgba = BLACK if value == "initial" else (None if value is None else parse_color(value))
        colors.add(rgba)
        clear = clear or (rgba is not None and rgba[3] <= FAINT)
    for value in cascade.values("-webkit-text-fill-color"):
        if value is None or value in (UNSET, "inherit", "unset", "currentcolor", "initial"):
            continue
        rgba = parse_color(value)
        clear = clear or (rgba is not None and rgba[3] <= FAINT)
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
