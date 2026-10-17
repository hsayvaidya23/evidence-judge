"""``@keyframes`` and the ``animation`` an element runs: whether its text may be hidden at some
point, and whether the animation comes to rest where it shows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from evidence_judge.ingest.css.model import ROOT, SHOWN, UNSET, Compound, Rule, Selector, Viewport
from evidence_judge.ingest.css.syntax import blocks, parse_declaration, split_commas
from evidence_judge.ingest.css.values import parse_number, parse_percent
from evidence_judge.ingest.css.visibility import Cascade, compute

SETTLED: Final = 5.0
"""How many seconds after the page loads an animation must end by for the reader to take its
resting state as the one the text shows in."""


Frame = tuple[tuple[float | None, ...], tuple[tuple[str, str | None], ...]]
"""A keyframe: its offsets (None for one the reader cannot place) and its declarations."""


@dataclass(frozen=True, slots=True)
class Keyframes:
    """What an ``@keyframes`` rule (every one of the name) does to text."""

    hides: bool = False
    """A keyframe may remove, hide or obscure text (or sets a custom property, which a value
    of the element's may use)."""
    starts_hidden: bool = False
    """The ``from`` (0%) keyframe may."""
    ends_hidden: bool = False
    """The ``to`` (100%) keyframe may."""
    unread: bool = False
    """A keyframe the reader cannot place or follow: an offset it does not know, a ``var()``,
    a custom property, or a stepped timing function."""
    frames: tuple[Frame, ...] = ()
    """Each keyframe's offsets and declarations, in source order."""


_FRAME: Final = Selector((Compound(),), ())


def read_keyframes(body: str, viewport: Viewport | None, before: Keyframes) -> Keyframes:
    """`before` with the keyframes of one more ``@keyframes`` rule of its name."""
    hides, starts, ends = before.hides, before.starts_hidden, before.ends_hidden
    unread = before.unread
    frames = list(before.frames)
    for prelude, frame in blocks(body):
        if frame is None:
            continue
        declared = [
            found
            for text, nested in blocks(frame)
            if nested is None
            for found in parse_declaration(text)
        ]
        best = {
            prop: Rule(_FRAME, prop, value, (1, 1, 0, (0,), (0, 0, 0), 0), False)
            for prop, value, _ in declared
        }
        computed = compute(Cascade(best, {}, frozenset(best)), ROOT, viewport)
        followed = not any(_unreadable(prop, value) for prop, value, _ in declared)
        hiding = computed.removed or computed.text != SHOWN or not followed
        offsets = [_offset(item) for item in split_commas(prelude.lower())]
        unread = unread or None in offsets or not followed
        hides = hides or hiding
        starts = starts or (hiding and 0 in offsets)
        ends = ends or (hiding and 100 in offsets)
        frames.append((tuple(offsets), tuple((prop, value) for prop, value, _ in declared)))
    return Keyframes(hides, starts, ends, unread, tuple(frames))


def _offset(selector: str) -> float | None:
    if selector == "from":
        return 0
    if selector == "to":
        return 100
    share = parse_percent(selector)
    return share if share is not None and 0 <= share <= 100 else None


def _unreadable(prop: str, value: str | None) -> bool:
    return (
        value is None
        or prop.startswith("--")
        or "var(" in value
        or (prop == "animation-timing-function" and "step" in value)
    )


def settles_shown(cascade: Cascade, frames: Keyframes) -> bool:
    """Whether an element's only animation, one whose `frames` may hide its text, certainly ends
    within `SETTLED` seconds and leaves the text as it is without it, or at a keyframe that
    shows it (`resting`)."""
    rest = resting(cascade, frames)
    return rest is not None and not any(
        frames.starts_hidden if offset == 0 else frames.ends_hidden for offset in rest
    )


def resting(cascade: Cascade, frames: Keyframes) -> tuple[int, ...] | None:
    """The end keyframes (0 or 100) an element's only animation, of `frames`, may come to rest
    on: none when it fills nothing, None when it may not certainly end within `SETTLED` seconds.
    It ends when it runs a known number of times, is not paused, steps or follows a scroll; it
    rests on an end keyframe when it fills forwards, for its direction and count."""
    if frames.unread:
        return None
    plays = _firsts(cascade, "animation-play-state", "running")
    timelines = _firsts(cascade, "animation-timeline", "auto")
    triggers = _firsts(cascade, "animation-trigger", "none")
    easings = _firsts(cascade, "animation-timing-function", "ease")
    if not all(v == "running" for v in plays) or not all(v == "auto" for v in timelines):
        return None
    if not all(v == "none" for v in triggers):
        return None
    if any(v is None or "step" in v for v in easings):
        return None
    durations = _all([_seconds(v) for v in _firsts(cascade, "animation-duration", "0s")])
    delays = _all([_seconds(v) for v in _firsts(cascade, "animation-delay", "0s")])
    counts = _all([_count(v) for v in _firsts(cascade, "animation-iteration-count", "1")])
    if not durations or not delays or not counts or min(*durations, *counts) < 0:
        return None
    if max(delays) + max(durations) * max(counts) > SETTLED:
        return None
    fills = _firsts(cascade, "animation-fill-mode", "none")
    if all(v in ("none", "backwards") for v in fills):
        return ()
    compositions = _firsts(cascade, "animation-composition", "replace")
    if not all(v in ("none", "backwards", "forwards", "both") for v in fills):
        return None
    if not all(v == "replace" for v in compositions):
        return None
    ends: set[int] = set()
    for direction in _firsts(cascade, "animation-direction", "normal"):
        for count in counts:
            end = _end(direction, count)
            if end is None:
                return None
            ends.add(end)
    return tuple(sorted(ends))


def _all(found: list[float | None]) -> list[float] | None:
    known = [value for value in found if value is not None]
    return known if len(known) == len(found) else None


def _end(direction: str | None, count: float) -> int | None:
    """The keyframe (0 or 100) a filling animation ends on; None when the reader cannot tell."""
    if count < 1 or count != int(count):
        return None
    odd = int(count) % 2 == 1
    if direction == "normal":
        return 100
    if direction == "reverse":
        return 0
    if direction == "alternate":
        return 100 if odd else 0
    if direction == "alternate-reverse":
        return 0 if odd else 100
    return None


def _firsts(cascade: Cascade, prop: str, initial: str) -> list[str | None]:
    """The first item of each list `prop` may be (that of the element's only animation): its
    initial value for an unset or reset one, None for one the reader cannot read."""
    found: list[str | None] = []
    for value in cascade.values(prop):
        if value in (UNSET, "initial", "unset"):
            found.append(initial)
        elif value is None or value in ("inherit", "revert", "revert-layer"):
            found.append(None)
        else:
            items = split_commas(value)
            found.append(items[0] if items else None)
    return found


def _seconds(value: str | None) -> float | None:
    if value == "auto":
        return 0.0
    number = None if value is None else parse_number(value)
    if number is None:
        return None
    amount, unit = number
    if unit == "s":
        return amount
    if unit == "ms":
        return amount / 1000
    return 0.0 if amount == 0 and not unit else None


def _count(value: str | None) -> float | None:
    number = None if value is None else parse_number(value)
    return number[0] if number is not None and not number[1] else None
