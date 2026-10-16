"""What an element's background paints behind its text: its colour over what is behind it, and
the colours of the gradients its ``background-image`` layers paint over that."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from evidence_judge.ingest.css.colors import CURRENT, parse_color
from evidence_judge.ingest.css.model import UNSET, Rgba, over
from evidence_judge.ingest.css.syntax import split_commas
from evidence_judge.ingest.css.values import (
    FAINT,
    functions,
    length_px,
    parse_amount,
    parse_degrees,
    parse_percent,
    split_args,
    tokens,
    too_deep,
)

Backdrop = tuple[Rgba, ...]
"""The opaque colours text may sit over (each stop of a gradient); None where the reader cannot
tell (an image it cannot see)."""


MAX_BACKDROP: Final = 64
"""How many colours a background may paint before the reader gives up on it (and is unsure)."""


class UnreadableError(Exception):
    """A background the reader cannot read where it may hide text: a gradient whose colours it
    cannot tell."""


_GRADIENT: Final = re.compile(r"^(?:-webkit-)?(?:repeating-)?(?:linear|radial|conic)-gradient$")


_WEBKIT_STOPS: Final = frozenset({"from", "to", "color-stop"})


_WIDE: Final = frozenset({UNSET, "inherit", "initial", "unset", "revert", "revert-layer"})


def background_colour(value: str | None, behind: Backdrop | None) -> Backdrop | None:
    """What a ``background-color`` paints over `behind`."""
    if value in (UNSET, "transparent", "initial", "unset"):
        return behind
    if value is None or value == "inherit":
        return None
    rgba = parse_color(value)
    if rgba is None:
        return None
    if rgba[3] <= FAINT:
        return behind
    if rgba[3] >= 1:
        return (rgba,)
    return None if behind is None else tuple(over(rgba, b) for b in behind)


def background_images(
    image: str | None,
    sizes: Sequence[str | None],
    base: Backdrop | None,
    current: Rgba | None,
) -> Backdrop | None:
    """The colours a ``background-image`` paints over `base`, its ``background-size`` any of
    `sizes`: those of each gradient stop, and `base` where a layer may cover nothing. None when
    a layer is an image the reader cannot see (a ``url()``, ``image-set()``, ...); raises
    `UnreadableError` for a gradient it cannot read or one that paints too many colours."""
    if image is None:
        return None
    if image in _WIDE or image == "none":
        return base
    found: dict[Rgba, None] = {}
    bare = False
    listed = [split_commas(size) if size else [] for size in sizes]
    for at, layer in enumerate(split_commas(image)):
        stops = _layer(layer, current)
        if stops is None:
            return None
        covers = [_covers(each[at % len(each)] if each else None) for each in listed]
        if not stops or not any(covers):
            continue
        bare = bare or not all(covers)
        for stop in stops:
            if stop[3] <= FAINT:
                bare = True
            elif stop[3] >= 1:
                found[stop] = None
            elif base is None:
                return None
            else:
                found.update(dict.fromkeys(over(stop, b) for b in base))
            if len(found) > MAX_BACKDROP:
                raise UnreadableError
    if not found:
        return base
    if bare:
        if base is None:
            return None
        found.update(dict.fromkeys(base))
    if len(found) > MAX_BACKDROP:
        raise UnreadableError
    return tuple(found)


def _layer(layer: str, current: Rgba | None) -> list[Rgba] | None:
    """The colours one image layer paints: none for ``none`` or an empty image, None for an
    image the reader cannot see."""
    if layer == "none":
        return []
    called = None if too_deep(layer) else functions(layer)
    if called is None or len(called) != 1 or not layer.endswith(")"):
        raise UnreadableError
    name, args = called[0]
    if name == "url":
        return [] if _empty_url(args) else None
    if _GRADIENT.match(name) or name == "-webkit-gradient":
        stops = _stops(args, current)
        if not stops:
            raise UnreadableError
        return stops
    return None


def _empty_url(args: str) -> bool:
    """A ``url()`` that loads nothing: an empty one, or a ``data:`` URL with no data."""
    url = args.strip().strip("\"'").strip()
    return not url or (url.lower().startswith("data:") and not url.partition(",")[2].strip())


def _stops(args: str, current: Rgba | None) -> list[Rgba]:
    stops: list[Rgba] = []
    for arg in split_args(args):
        for token in tokens(arg):
            colour = parse_color(token, current)
            if colour is not None:
                stops.append(colour)
                continue
            if token == CURRENT:
                raise UnreadableError
            if "(" not in token:
                continue
            called = functions(token)
            if called is None or len(called) != 1:
                raise UnreadableError
            name, inner = called[0]
            if name in _WEBKIT_STOPS:
                stops.extend(_stops(inner, current))
            elif not _position(token):
                raise UnreadableError
    return stops


def _position(token: str) -> bool:
    """A math function a gradient may place a stop or turn its line by."""
    return any(
        found is not None
        for found in (length_px(token, None, None, 0.0), parse_degrees(token), parse_amount(token))
    )


def _covers(size: str | None) -> bool:
    """Whether a layer of `size` may paint some area (a zero width or height paints none)."""
    if size is None:
        return True
    for part in tokens(size)[:2]:
        if parse_percent(part) == 0 or length_px(part, None, 16.0, 0.0) == 0:
            return False
    return True
