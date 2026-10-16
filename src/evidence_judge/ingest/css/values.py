"""CSS values: colours, numbers, angles, lengths and font sizes."""

from __future__ import annotations

import re
from typing import Final

from evidence_judge.ingest.css.model import UNSET, Rgba, Viewport

_NUMBER = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-z%]*)$")


FUNCTION = re.compile(r"([a-z0-9-]+)\(([^()]*)\)")


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


_NAMED: Final[dict[str, Rgba]] = {
    "transparent": (0, 0, 0, 0),
    "black": (0, 0, 0, 1),
    "white": (255, 255, 255, 1),
    "red": (255, 0, 0, 1),
    "green": (0, 128, 0, 1),
    "blue": (0, 0, 255, 1),
    "gray": (128, 128, 128, 1),
    "grey": (128, 128, 128, 1),
    "silver": (192, 192, 192, 1),
    "yellow": (255, 255, 0, 1),
    "orange": (255, 165, 0, 1),
    "purple": (128, 0, 128, 1),
    "navy": (0, 0, 128, 1),
}


def parse_color(value: str) -> Rgba | None:
    value = value.strip()
    if value in _NAMED:
        return _NAMED[value]
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(d * 2 for d in digits)
        if len(digits) in (6, 8) and all(c in "0123456789abcdef" for c in digits):
            alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
            return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), alpha)
        return None
    found = FUNCTION.fullmatch(value)
    if found is None or found.group(1) not in ("rgb", "rgba", "hsl", "hsla"):
        return None
    parts = [p for p in re.split(r"[\s,/]+", found.group(2).strip()) if p]
    if len(parts) not in (3, 4):
        return None
    alpha = 1.0
    if len(parts) == 4:
        given = parse_percent(parts[3])
        alpha_value = given / 100 if given is not None else parse_amount(parts[3])
        if alpha_value is None:
            return None
        alpha = alpha_value
    if found.group(1).startswith("hsl"):
        hue = (
            parse_degrees(parts[0])
            if not parts[0].replace(".", "").isdigit()
            else parse_amount(parts[0])
        )
        saturation, lightness = parse_percent(parts[1]), parse_percent(parts[2])
        if hue is None or saturation is None or lightness is None:
            return None
        return (*_hsl(hue, saturation / 100, lightness / 100), alpha)
    channels: list[float] = []
    for part in parts[:3]:
        share = parse_percent(part)
        amount = share * 2.55 if share is not None else parse_amount(part)
        if amount is None:
            return None
        channels.append(round(amount))
    return (channels[0], channels[1], channels[2], alpha)


def _hsl(hue: float, saturation: float, lightness: float) -> tuple[float, float, float]:
    def channel(n: float) -> float:
        k = (n + hue / 30) % 12
        a = saturation * min(lightness, 1 - lightness)
        return round(255 * (lightness - a * max(-1, min(k - 3, 9 - k, 1))))

    return channel(0), channel(8), channel(4)


def parse_number(value: str) -> tuple[float, str] | None:
    found = _NUMBER.match(value.strip())
    if found is None:
        return None
    return float(found.group(1)), found.group(2)


def parse_amount(value: str) -> float | None:
    """A plain number, or a percentage as a fraction."""
    found = parse_number(value)
    if found is None:
        return None
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


def parse_degrees(value: str) -> float | None:
    found = parse_number(value)
    if found is None:
        return None
    number, unit = found
    return {
        "deg": number,
        "turn": number * 360,
        "rad": number * 57.29578,
        "grad": number * 0.9,
    }.get(unit)


def length_px(value: str, viewport: Viewport | None, font: float | None = 16.0) -> float | None:
    """A length in CSS pixels, or None when it is relative to something unknown."""
    found = parse_number(value)
    if found is None:
        return None
    number, unit = found
    if not unit:
        return number if number == 0 else None
    if unit in _UNITS:
        return number * _UNITS[unit]
    if unit in ("em", "ex", "ch") and font is not None:
        return number * font * (1 if unit == "em" else 0.5)
    if unit in ("vw", "vh", "vmin", "vmax") and viewport is not None:
        width, height = viewport
        side = {"vw": width, "vh": height, "vmin": min(viewport), "vmax": max(viewport)}[unit]
        return number * side / 100
    return None


def far_offset(value: str | None, viewport: Viewport | None, sign: int) -> bool:
    """Whether an offset of `value` moves a box off-screen: `sign` -1 only counts large negative
    offsets, 1 large positive ones, 0 either."""
    if value is None or value == UNSET:
        return False
    px = length_px(value, viewport)
    if px is None:
        return False
    return px <= -FAR if sign < 0 else (px >= FAR if sign > 0 else abs(px) >= FAR)


def tiny_size(value: str | None, viewport: Viewport | None) -> bool:
    if value is None or value == UNSET:
        return False
    if parse_percent(value) == 0:
        return True
    px = length_px(value, viewport)
    return px is not None and px <= 1


def text_indent_far(value: str | None, viewport: Viewport | None) -> bool:
    if value is None:
        return False
    share = parse_percent(value.split()[0] if value.split() else value)
    if share is not None:
        return share <= -100
    return far_offset(value.split()[0] if value.split() else value, viewport, 0)


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
    return length_px(value, viewport, inherited)
