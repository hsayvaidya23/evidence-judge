"""What the cascade decides for an element: whether it is removed, hidden or may be obscured,
and what its children inherit."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from evidence_judge.ingest.css.backgrounds import (
    Backdrop,
    UnreadableError,
    background_colour,
    background_images,
)
from evidence_judge.ingest.css.colors import CURRENT, parse_color
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

_OBSCURERS: Final = words(
    "opacity filter transform scale translate rotate zoom clip-path offset offset-path "
    "mask mask-image mask-border mask-border-source -webkit-mask -webkit-mask-image "
    "-webkit-mask-box-image"
)


_SIZES: Final = words(
    "height max-height width max-width block-size max-block-size inline-size max-inline-size"
)


_MARGINS: Final = words("margin-left margin-top margin-inline-start margin-block-start")


_OFFSETS: Final = (
    ("left", 0, 0),
    ("right", 0, 0),
    ("inset-inline-start", 0, 0),
    ("inset-inline-end", 0, 0),
    ("top", -1, 1),
    ("inset-block-start", -1, 1),
    ("bottom", 1, 1),
    ("inset-block-end", 1, 1),
)
"""The offset properties, the direction in which an offset moves a box off-screen (-1 only
negative, 1 only positive, 0 either), and the viewport side a percentage is of (0 width,
1 height). Logical ones are read as in a horizontal left-to-right page; either way the far
test is the same."""


_TRANSFORMS: Final = ("transform", "rotate", "scale")


_MAX_COMBINATIONS: Final = 16


_SAFE_FILTERS: Final = words("drop-shadow grayscale saturate hue-rotate")
"""Filter functions that keep a pixel's luminance, so text stays as readable as it was."""


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


_BOX: Final = (
    _OBSCURERS
    | words("clip position contain overflow-x overflow-y")
    | {prop for prop, _, _ in _OFFSETS}
    | _MARGINS
    | _SIZES
)


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
    alpha = parent.alpha
    if cascade.sets("opacity", "filter"):
        alpha = alpha * _opacity(cascade)
    if present.isdisjoint(_TEXT):
        if unsure == parent.unsure and visibility == parent.visibility and alpha == parent.alpha:
            return parent
        return replace(parent, removed=False, unsure=unsure, visibility=visibility, alpha=alpha)
    font_px, font_tiny = parent.font_px, parent.font_tiny
    if cascade.sets("font-size"):
        font_px, font_tiny = _font(cascade.values("font-size"), parent, viewport)
    colors, clear, paints = parent.colors, parent.clear, parent.paints
    if cascade.sets("color", "-webkit-text-fill-color"):
        colors, clear, paints = _paint(cascade, parent)
    background = parent.background
    if cascade.sets("background-color", "background-image", "background-size"):
        background, unreadable = _backdrop(cascade, parent.background, colors)
        unsure = unsure or unreadable
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
    fills, fill_none = parent.fills, parent.fill_none
    if cascade.sets("fill"):
        fills, fill_none = _fills(cascade, parent, colors)
    return Computed(
        removed=False,
        unsure=unsure,
        visibility=visibility,
        font_px=font_px,
        font_tiny=font_tiny,
        colors=colors,
        clear=clear,
        paints=paints,
        background=background,
        clip_text=clip_text,
        indent_far=indent_far,
        fill_none=fill_none,
        fill_faint=_inherits(cascade, "fill-opacity", parent.fill_faint, _faint),
        fills=fills,
        alpha=alpha,
        svg=parent.svg,
        custom=parent.custom,
        registered=parent.registered,
    )


def _inherits(cascade: Cascade, prop: str, inherited: bool, test: Callable[[str], bool]) -> bool:
    """Whether an inherited property may take a value `test` holds for."""
    if not cascade.sets(prop):
        return inherited
    return any(
        inherited if v in (UNSET, "inherit", "unset") else v is None or test(v)
        for v in cascade.values(prop)
    )


def _faint(value: str) -> bool:
    amount = parse_amount(value)
    return amount is None or amount <= FAINT


def _opacity(cascade: Cascade) -> float:
    """The least this element's ``opacity`` (and ``filter: opacity()``) may multiply what it
    paints by; a value the reader cannot read counts as 1 (`_obscured` makes it unsure)."""
    least = 1.0
    for value in cascade.values("opacity"):
        amount = None if value is None or value in _WIDE else parse_amount(value)
        if amount is not None:
            least = min(least, max(0.0, min(1.0, amount)))
    for value in cascade.values("filter"):
        for name, args in (functions(value) or []) if value is not None else []:
            amount = parse_amount(args) if name == "opacity" and args else None
            if amount is not None:
                least = min(least, max(0.0, min(1.0, amount)))
    return least


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
        return abs(share) >= 100
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
    if cascade.sets(*_TRANSFORMS) and _transformed_away(cascade):
        return True
    positions = cascade.values("position") if cascade.sets("position") else (UNSET,)
    fixed = any(v is None or v == "fixed" for v in positions)
    placed = fixed or any(v == "absolute" for v in positions)
    moved = placed or any(v in ("relative", "sticky") for v in positions)
    if placed and any(v is None or _clip_rect_hides(v) for v in cascade.values("clip")):
        return True
    sides = viewport if viewport is not None else (None, None)
    if moved:
        for prop, sign, side in _OFFSETS:
            if prop in cascade.present and any(
                _offset_far(v, viewport, sign, sides[side])
                or (fixed and _off_viewport(v, viewport, sides[side]))
                for v in cascade.values(prop)
            ):
                return True
    for prop in _MARGINS & cascade.present:
        if any(_offset_far(v, viewport, -1, sides[0]) for v in cascade.values(prop)):
            return True
    if cascade.sets(*_SIZES) and cascade.sets("overflow-x", "overflow-y", "contain"):
        clipped = any(
            v is None or v in ("hidden", "clip", "auto", "scroll", "overlay")
            for p in ("overflow-x", "overflow-y")
            for v in cascade.values(p)
        ) or any(
            v is None or not {"paint", "strict", "content"}.isdisjoint(v.split())
            for v in cascade.values("contain")
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


def _off_viewport(value: str | None, viewport: Viewport | None, side: int | None) -> bool:
    """Whether an offset may place a fixed box outside the viewport, which never scrolls to it:
    half the viewport or more out past one edge, or the whole viewport in from the other."""
    if value is None or value in _WIDE or value == "auto" or side is None:
        return False
    px = length_px(value, viewport, 16.0, side)
    return px is not None and (px <= -side / 2 or px >= side)


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
    """Whether an obscuring property's value may hide the box (or cannot be read). Masks and
    motion paths always may."""
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
        return _filter_hides(value)
    if prop == "translate":
        return any(_moves_far(t, viewport) for t in tokens(value)[:2])
    if prop == "clip-path":
        return _clip_path_hides(value, viewport)
    if prop == "transform":
        return _transform_moves(value, viewport)
    return prop not in ("rotate", "scale")


def _filter_hides(value: str) -> bool:
    """Whether a filter may make text unreadable: anything but luminance-keeping functions,
    a slight blur, and mild brightness, contrast, invert, sepia or opacity changes."""
    found = functions(value)
    if not found or value.replace(" ", "") != "".join(f"{n}({a})" for n, a in found).replace(
        " ", ""
    ):
        return True
    for name, args in found:
        if name in _SAFE_FILTERS:
            continue
        if name == "blur":
            px = length_px(args, None) if args else 0.0
            if px is None or px > 0.5:
                return True
            continue
        amount = parse_amount(args) if args else 1.0
        if amount is None:
            return True
        if name == "opacity":
            hides = amount <= FAINT
        elif name in ("brightness", "contrast"):
            hides = not 0.5 <= amount <= 2
        elif name in ("invert", "sepia"):
            hides = amount > 0.1
        else:
            hides = True
        if hides:
            return True
    return False


def _moves_far(value: str, viewport: Viewport | None) -> bool:
    """Whether a translation may move a box off-screen: by its own size or more (100%), or far;
    a value the reader cannot read may."""
    fixed = length_px(value, viewport, 16.0, 0.0)
    scaled = length_px(value, viewport, 16.0, 100.0)
    if fixed is None or scaled is None:
        return True
    return abs(scaled - fixed) >= 100 or is_far(fixed, 0)


Matrix = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


_IDENTITY: Final[Matrix] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class _UnreadableError(Exception):
    pass


def _transform_moves(value: str, viewport: Viewport | None) -> bool:
    """Whether a ``transform``'s translations may move the box off-screen, or a function is one
    the reader does not know (how it shapes the box is `_transformed_away`)."""
    try:
        _transform_matrix(value, viewport)
    except _UnreadableError:
        return True
    return False


def _transformed_away(cascade: Cascade) -> bool:
    """Whether ``rotate``, ``scale`` and ``transform`` together may squash the box to nothing,
    turn it edge-on, or stretch it past reading."""
    candidates = [
        [v for v in cascade.values(p) if v is not None and v not in _WIDE and v != "none"] or [""]
        for p in _TRANSFORMS
    ]
    combinations = 1
    for found in candidates:
        combinations *= len(found)
    if combinations > _MAX_COMBINATIONS:
        return True
    transforms, rotates, scales = candidates
    for transform in transforms:
        for rotate in rotates:
            for scale in scales:
                try:
                    matrix = _multiply(
                        _rotate_property(rotate),
                        _multiply(_scale_property(scale), _transform_matrix(transform, None)),
                    )
                except _UnreadableError:
                    return True
                if _degenerate(matrix):
                    return True
    return False


def _degenerate(matrix: Matrix) -> bool:
    """Whether a linear map leaves the box (seen face-on, without perspective) too thin, too
    small or too stretched to read (or overflows what the reader can compute)."""
    if not all(math.isfinite(c) for row in matrix for c in row):
        return True
    ux, uy = matrix[0][0], matrix[1][0]
    vx, vy = matrix[0][1], matrix[1][1]
    across, down = (ux * ux + uy * uy) ** 0.5, (vx * vx + vy * vy) ** 0.5
    area = abs(ux * vy - uy * vx)
    return area <= FAINT**2 or min(across, down) <= FAINT or max(across, down) >= 1 / FAINT


def _transform_matrix(value: str, viewport: Viewport | None) -> Matrix:
    """The linear part of a ``transform`` ("" for none); raises `_UnreadableError` for a function the
    reader does not know or cannot read, a far translation, or a translation in depth under
    perspective."""
    if not value:
        return _IDENTITY
    found = functions(value)
    if not found:
        raise _UnreadableError
    matrix = _IDENTITY
    perspective = depth = False
    for name, args in found:
        values = split_args(args)
        step = _IDENTITY
        if name in ("translate", "translatex", "translatey", "translate3d", "translatez"):
            moved = values if name != "translatez" else []
            if name == "translatey":
                moved = ["0", *values]
            if any(_moves_far(v, viewport) for v in moved[:2]):
                raise _UnreadableError
            z = values[-1] if name in ("translate3d", "translatez") else "0"
            depth = depth or length_px(z, viewport) != 0
        elif name == "perspective":
            perspective = perspective or values[0] != "none"
        elif name.startswith("scale"):
            amounts = [_number(v) for v in values]
            x = amounts[0]
            y = amounts[1] if len(amounts) > 1 and name in ("scale", "scale3d") else x
            if name == "scalex":
                y = 1.0
            elif name == "scaley":
                x, y = 1.0, amounts[0]
            elif name == "scalez":
                x = y = 1.0
            step = ((x, 0.0, 0.0), (0.0, y, 0.0), (0.0, 0.0, 1.0))
        elif name in ("rotate", "rotatez"):
            step = _rotation(0.0, 0.0, 1.0, _angle(values[0]))
        elif name == "rotatex":
            step = _rotation(1.0, 0.0, 0.0, _angle(values[0]))
        elif name == "rotatey":
            step = _rotation(0.0, 1.0, 0.0, _angle(values[0]))
        elif name == "rotate3d" and len(values) == 4:
            x, y, z = (_number(v) for v in values[:3])
            step = _rotation(x, y, z, _angle(values[3]))
        elif name in ("skew", "skewx", "skewy"):
            ax = _angle(values[0]) if name != "skewy" else 0.0
            ay = _angle(values[-1]) if name == "skewy" or len(values) == 2 else 0.0
            step = ((1.0, _tan(ax), 0.0), (_tan(ay), 1.0, 0.0), (0.0, 0.0, 1.0))
        elif name == "matrix" and len(values) == 6:
            a, b, c, d, e, f = (_number(v) for v in values)
            if max(abs(e), abs(f)) >= FAR:
                raise _UnreadableError
            step = ((a, c, 0.0), (b, d, 0.0), (0.0, 0.0, 1.0))
        elif name == "matrix3d" and len(values) == 16:
            m = [_number(v) for v in values]
            if any(abs(m[i]) > 1e-9 for i in (3, 7, 11)) or m[15] <= 0:
                raise _UnreadableError
            if max(abs(m[12]), abs(m[13])) / m[15] >= FAR:
                raise _UnreadableError
            step = ((m[0], m[4], m[8]), (m[1], m[5], m[9]), (m[2], m[6], m[10]))
        else:
            raise _UnreadableError
        matrix = _multiply(matrix, step)
    if perspective and depth:
        raise _UnreadableError
    return matrix


def _rotate_property(value: str) -> Matrix:
    """The ``rotate`` property: an angle, or an axis (``x``, ``y``, ``z`` or a vector) and an
    angle."""
    if not value:
        return _IDENTITY
    parts = tokens(value)
    axes = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
    if len(parts) == 1:
        return _rotation(0.0, 0.0, 1.0, _angle(parts[0]))
    if len(parts) == 2 and parts[0] in axes:
        return _rotation(*axes[parts[0]], _angle(parts[1]))
    if len(parts) == 4:
        x, y, z = (_number(p) for p in parts[:3])
        return _rotation(x, y, z, _angle(parts[3]))
    raise _UnreadableError


def _scale_property(value: str) -> Matrix:
    if not value:
        return _IDENTITY
    amounts = [_number(p) for p in tokens(value)]
    if not 1 <= len(amounts) <= 3:
        raise _UnreadableError
    x = amounts[0]
    y = amounts[1] if len(amounts) > 1 else x
    return ((x, 0.0, 0.0), (0.0, y, 0.0), (0.0, 0.0, 1.0))


def _number(value: str) -> float:
    found = parse_amount(value)
    if found is None:
        raise _UnreadableError
    return found


def _angle(value: str) -> float:
    found = parse_degrees(value)
    if found is None:
        raise _UnreadableError
    return math.radians(found)


def _tan(angle: float) -> float:
    cos = math.cos(angle)
    return math.sin(angle) / cos if abs(cos) > 1e-9 else 1e9


def _rotation(x: float, y: float, z: float, angle: float) -> Matrix:
    length = (x * x + y * y + z * z) ** 0.5
    if length == 0:
        raise _UnreadableError
    x, y, z = x / length, y / length, z / length
    c, s, t = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return (
        (t * x * x + c, t * x * y - s * z, t * x * z + s * y),
        (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
        (t * x * z - s * y, t * y * z + s * x, t * z * z + c),
    )


def _multiply(one: Matrix, two: Matrix) -> Matrix:
    rows = [tuple(sum(one[i][k] * two[k][j] for k in range(3)) for j in range(3)) for i in range(3)]
    return (
        (rows[0][0], rows[0][1], rows[0][2]),
        (rows[1][0], rows[1][1], rows[1][2]),
        (rows[2][0], rows[2][1], rows[2][2]),
    )


def _clip_rect_hides(value: str) -> bool:
    if value in _WIDE or value == "auto":
        return False
    found = functions(value)
    if not found or found[0][0] != "rect":
        return True
    sides = [s for s in re.split(r"[\s,]+", found[0][1]) if s]
    return any(s != "auto" for s in sides)


_LINE: Final = (300.0, 20.0)
"""A line of text, the box a clip path is tried on besides the viewport."""


_BOXES: Final = words("border-box padding-box content-box margin-box fill-box stroke-box view-box")


def _clip_path_hides(value: str, viewport: Viewport | None) -> bool:
    """Whether a clip path may cut away most of a box the size of a line of text or of the
    viewport; shapes the reader does not model (``path()``, ``url()``, ``rect()``, ...) may."""
    found = functions(value)
    if found is None:
        return True
    if not found:
        return not set(value.split()) <= _BOXES
    name, args = found[0]
    if len(found) > 1 or name not in ("inset", "circle", "ellipse", "polygon"):
        return True
    boxes = [_LINE, (float(viewport[0]), float(viewport[1]))] if viewport else [_LINE]
    for width, height in boxes:
        kept = _clip_area(name, args, width, height)
        if kept is None or kept <= FAINT * width * height:
            return True
    return False


def _clip_area(name: str, args: str, width: float, height: float) -> float | None:
    """The area a basic shape keeps of a `width` x `height` box, None when unreadable."""
    diagonal = ((width * width + height * height) / 2) ** 0.5

    def size(text: str, side: float) -> float | None:
        return length_px(text, None, 16.0, side)

    if name == "inset":
        sides = tokens(args.split(" round ")[0])[:4]
        if not sides:
            return None
        found = [
            size(side, height if i % 2 == 0 else width) for i, side in enumerate(_box_of(sides))
        ]
        if any(f is None for f in found):
            return None
        top, right, bottom, left = (f or 0.0 for f in found)
        return max(0.0, width - left - right) * max(0.0, height - top - bottom)
    radii = tokens(args.split(" at ")[0])
    if name in ("circle", "ellipse"):
        keywords = ("closest-side", "farthest-side", "closest-corner", "farthest-corner")
        if not radii or all(r in keywords for r in radii):
            return width * height
        if any(r in keywords for r in radii):
            return None
        if name == "circle":
            r = size(radii[0], diagonal)
            return None if r is None else min(width * height, math.pi * r * r)
        rx, ry = size(radii[0], width), size(radii[-1], height)
        return None if rx is None or ry is None else min(width * height, math.pi * rx * ry)
    points: list[tuple[float, float]] = []
    for point in split_args(args):
        parts = tokens(point)
        if len(parts) != 2:
            if len(parts) == 1 and parts[0] in ("nonzero", "evenodd"):
                continue
            return None
        x, y = size(parts[0], width), size(parts[1], height)
        if x is None or y is None:
            return None
        points.append((x, y))
    twice = sum(
        a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1], strict=True)
    )
    return abs(twice) / 2


def _box_of(values: list[str]) -> list[str]:
    if len(values) == 1:
        return [values[0]] * 4
    if len(values) == 2:
        return [values[0], values[1], values[0], values[1]]
    if len(values) == 3:
        return [values[0], values[1], values[2], values[1]]
    return values[:4]


def _colours(value: str, current: tuple[Rgba, ...]) -> tuple[Rgba, ...] | None:
    """The colours `value` may be (several when it holds ``currentcolor`` and that may be
    several), None when the reader cannot read it."""
    if CURRENT not in value:
        rgba = parse_color(value)
        return None if rgba is None else (rgba,)
    found = [parse_color(value, c) for c in current]
    return None if any(f is None for f in found) else tuple(f for f in found if f is not None)


def _paint(
    cascade: Cascade, parent: Computed
) -> tuple[tuple[Rgba, ...], bool, tuple[Rgba, ...] | None]:
    """The colours text may be painted in, whether one may be transparent or unreadable, and
    the ``-webkit-text-fill-color`` colours that paint it instead (None: its colour)."""
    colors: list[Rgba] = []
    clear = False
    for value in cascade.values("color"):
        if value in _INHERIT:
            colors.extend(parent.colors)
            clear = clear or parent.clear
            continue
        found = (
            (BLACK,)
            if value in _WIDE
            else (None if value is None else _colours(value, parent.colors))
        )
        if found is None:
            clear = True
            continue
        colors.extend(found)
        clear = clear or any(c[3] <= FAINT for c in found)
    colors = list(dict.fromkeys(colors)) or [BLACK]
    paints: list[Rgba] = []
    own = False
    for value in cascade.values("-webkit-text-fill-color"):
        if value in _INHERIT:
            paints.extend(parent.paints if parent.paints is not None else colors)
            own = own or parent.paints is not None
            continue
        if value in _WIDE or value == CURRENT:
            paints.extend(colors)
            continue
        found = None if value is None else _colours(value, tuple(colors))
        own = True
        if found is None:
            clear = True
            continue
        paints.extend(found)
        clear = clear or any(c[3] <= FAINT for c in found)
    return tuple(colors), clear, (tuple(dict.fromkeys(paints)) if own else None)


def _fills(
    cascade: Cascade, parent: Computed, colors: tuple[Rgba, ...]
) -> tuple[tuple[Rgba, ...], bool]:
    """The colours SVG text may be filled with, and whether it may be filled with nothing (or
    with something the reader cannot read, such as a gradient)."""
    fills: list[Rgba] = []
    none = False
    for value in cascade.values("fill"):
        if value in _INHERIT:
            fills.extend(parent.fills)
            none = none or parent.fill_none
            continue
        if value in _WIDE:
            fills.append(BLACK)
            continue
        found = None if value in (None, "none") else _colours(value, colors)
        if found is None or any(c[3] <= FAINT for c in found):
            none = True
            continue
        fills.extend(found)
    return (tuple(dict.fromkeys(fills)) or parent.fills), none


def _backdrop(
    cascade: Cascade, behind: Backdrop | None, colors: tuple[Rgba, ...]
) -> tuple[Backdrop | None, bool]:
    """The colours an element's background paints behind its text, and whether it paints a
    gradient the reader cannot read."""
    found = {background_colour(v, behind) for v in cascade.values("background-color")}
    base = found.pop() if len(found) == 1 else None
    images = set(cascade.values("background-image"))
    if len(images) != 1:
        unknown = any(v not in _WIDE and v != "none" for v in images)
        return (None if unknown else base), False
    current = colors[0] if len(colors) == 1 else None
    try:
        painted = background_images(images.pop(), cascade.values("background-size"), base, current)
    except UnreadableError:
        return None, True
    return painted, False
