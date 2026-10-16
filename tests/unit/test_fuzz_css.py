"""Deterministic fuzzing of the CSS reader's value parser and colour functions.

Each case builds values from a seeded random.Random over the reader's grammar (math and colour
functions, ``var()`` with fallbacks, extreme and malformed numbers), nested deep or shallow, so a
failure reproduces from the seed alone. The reader may answer anything for such a value, but it
must answer quickly and never raise.
"""

from __future__ import annotations

import random
import time

import pytest

from evidence_judge.ingest.css import HIDDEN, ROOT, SHOWN, UNSURE, Element, Sheet, StyleSheets
from evidence_judge.ingest.css.colors import parse_color
from evidence_judge.ingest.css.values import length_px, parse_amount, parse_degrees

NUMBERS = ("0", "1", "-1", ".5", "1e308", "-1e308", "1e999", "1e-999", "255", "50%", "-50%")
UNITS = ("", "px", "%", "em", "vw", "deg", "turn", "in", "q")
KEYWORDS = ("none", "transparent", "red", "#fff", "#0000", "currentcolor", "auto", "inherit")
SPACES = ("srgb", "oklab", "lch", "hsl", "display-p3", "bogus")
CASES = 400
CASE_BUDGET_S = 2.0


def _number(rng: random.Random) -> str:
    return rng.choice(NUMBERS) + rng.choice(UNITS)


KINDS = tuple(range(10))


def _value(rng: random.Random, depth: int, stop: float, kinds: tuple[int, ...]) -> str:
    if depth <= 0 or rng.random() < stop:
        return rng.choice((_number(rng), rng.choice(KEYWORDS), f"var(--v{rng.randrange(4)})"))
    inner = _value(rng, depth - 1, stop, kinds)
    match rng.choice(kinds):
        case 0:
            return f"calc({inner} {rng.choice('+-*/')} {_number(rng)})"
        case 1:
            return f"{rng.choice(('min', 'max', 'abs', 'clamp'))}({inner}, {_number(rng)})"
        case 2:
            return f"var(--v{rng.randrange(4)}, {inner})"
        case 3:
            mixed = rng.choice(KEYWORDS)
            return f"color-mix(in {rng.choice(SPACES)}, {inner} {_number(rng)}, {mixed})"
        case 4:
            return f"light-dark({inner}, {rng.choice(KEYWORDS)})"
        case 5:
            name = rng.choice(("rgb", "hsl", "hwb", "lab", "lch", "oklab", "oklch"))
            channels = [_number(rng) for _ in range(rng.randint(1, 4))]
            channels[rng.randrange(len(channels))] = inner
            return f"{name}({' '.join(channels)})"
        case 6:
            return f"color({rng.choice(SPACES)} {_number(rng)} {_number(rng)} {inner})"
        case 7:
            return f"env(safe-area-inset-top, {inner})"
        case 8:
            return "-" * rng.randint(1, 50) + inner
        case _:
            return f"{inner}{rng.choice(('(', ')', ',', '/', ''))}" if rng.random() < 0.1 else inner


def _random_value(rng: random.Random) -> str:
    """A value nested shallow or deep, of one kind of function or of any."""
    depth, stop = rng.choice(((3, 0.3), (12, 0.1), (60, 0.02), (600, 0.0)))
    kinds = KINDS if rng.random() < 0.5 else (rng.choice(KINDS[:8]),)
    return _value(rng, depth, stop, kinds)


def _style(rng: random.Random) -> str:
    links = rng.choice((4, 4, 40, 600))
    chain = "".join(f"--v{i}:var(--v{i + 1});" for i in range(4, links))
    custom = "".join(f"--v{i}:{_random_value(rng)};" for i in range(4))
    props = ("color", "opacity", "display", "transform", "font-size", "background", "left")
    own = "".join(f"{p}:{_random_value(rng)};" for p in props)
    tail = f"--v{links}:{_random_value(rng)};display:var(--v4)" if links > 4 else ""
    return f":root{{{custom}{chain}}}.x{{{own}position:absolute;{tail}}}"


def _text_state(css: str) -> int:
    sheets = StyleSheets([Sheet(css, "https://shop.test/")], (1000, 800))
    parent: Element | None = None
    style = ROOT
    for tag, attrs in (("HTML", {}), ("BODY", {}), ("P", {"class": "x"})):
        element = Element(tag, attrs, parent)
        style = sheets.style(element, style)
        if style.removed:
            return HIDDEN
        parent = element
    return style.text


@pytest.mark.parametrize("seed", range(4))
def test_values_parse_without_raising(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(CASES):
        value = _random_value(rng)
        start = time.perf_counter()
        colour = parse_color(value)
        length_px(value, (1000, 800))
        parse_amount(value)
        parse_degrees(value)
        assert time.perf_counter() - start < CASE_BUDGET_S, value[:200]
        if colour is not None:
            assert all(0 <= c <= 255 for c in colour[:3]), value[:200]
            assert 0 <= colour[3] <= 1, value[:200]


@pytest.mark.parametrize("seed", range(4))
def test_stylesheets_of_random_values_read_without_raising(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(CASES // 4):
        css = _style(rng)
        start = time.perf_counter()
        assert _text_state(css) in (SHOWN, UNSURE, HIDDEN)
        assert time.perf_counter() - start < CASE_BUDGET_S, css[:200]
