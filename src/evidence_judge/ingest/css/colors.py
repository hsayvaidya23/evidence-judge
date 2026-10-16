"""CSS colours: named and system colours, the colour functions and ``color-mix()``."""

from __future__ import annotations

import math
import re
from typing import Final

from evidence_judge.ingest.css.model import Rgba, linear
from evidence_judge.ingest.css.values import (
    MAX_NESTING,
    functions,
    parse_amount,
    parse_degrees,
    parse_percent,
    split_args,
)

_NAMES: Final = """
aliceblue f0f8ff antiquewhite faebd7 aqua 00ffff aquamarine 7fffd4 azure f0ffff beige f5f5dc
bisque ffe4c4 black 000000 blanchedalmond ffebcd blue 0000ff blueviolet 8a2be2 brown a52a2a
burlywood deb887 cadetblue 5f9ea0 chartreuse 7fff00 chocolate d2691e coral ff7f50
cornflowerblue 6495ed cornsilk fff8dc crimson dc143c cyan 00ffff darkblue 00008b
darkcyan 008b8b darkgoldenrod b8860b darkgray a9a9a9 darkgreen 006400 darkgrey a9a9a9
darkkhaki bdb76b darkmagenta 8b008b darkolivegreen 556b2f darkorange ff8c00 darkorchid 9932cc
darkred 8b0000 darksalmon e9967a darkseagreen 8fbc8f darkslateblue 483d8b darkslategray 2f4f4f
darkslategrey 2f4f4f darkturquoise 00ced1 darkviolet 9400d3 deeppink ff1493 deepskyblue 00bfff
dimgray 696969 dimgrey 696969 dodgerblue 1e90ff firebrick b22222 floralwhite fffaf0
forestgreen 228b22 fuchsia ff00ff gainsboro dcdcdc ghostwhite f8f8ff gold ffd700
goldenrod daa520 gray 808080 green 008000 greenyellow adff2f grey 808080 honeydew f0fff0
hotpink ff69b4 indianred cd5c5c indigo 4b0082 ivory fffff0 khaki f0e68c lavender e6e6fa
lavenderblush fff0f5 lawngreen 7cfc00 lemonchiffon fffacd lightblue add8e6 lightcoral f08080
lightcyan e0ffff lightgoldenrodyellow fafad2 lightgray d3d3d3 lightgreen 90ee90 lightgrey d3d3d3
lightpink ffb6c1 lightsalmon ffa07a lightseagreen 20b2aa lightskyblue 87cefa
lightslategray 778899 lightslategrey 778899 lightsteelblue b0c4de lightyellow ffffe0 lime 00ff00
limegreen 32cd32 linen faf0e6 magenta ff00ff maroon 800000 mediumaquamarine 66cdaa
mediumblue 0000cd mediumorchid ba55d3 mediumpurple 9370db mediumseagreen 3cb371
mediumslateblue 7b68ee mediumspringgreen 00fa9a mediumturquoise 48d1cc mediumvioletred c71585
midnightblue 191970 mintcream f5fffa mistyrose ffe4e1 moccasin ffe4b5 navajowhite ffdead
navy 000080 oldlace fdf5e6 olive 808000 olivedrab 6b8e23 orange ffa500 orangered ff4500
orchid da70d6 palegoldenrod eee8aa palegreen 98fb98 paleturquoise afeeee palevioletred db7093
papayawhip ffefd5 peachpuff ffdab9 peru cd853f pink ffc0cb plum dda0dd powderblue b0e0e6
purple 800080 rebeccapurple 663399 red ff0000 rosybrown bc8f8f royalblue 4169e1
saddlebrown 8b4513 salmon fa8072 sandybrown f4a460 seagreen 2e8b57 seashell fff5ee sienna a0522d
silver c0c0c0 skyblue 87ceeb slateblue 6a5acd slategray 708090 slategrey 708090 snow fffafa
springgreen 00ff7f steelblue 4682b4 tan d2b48c teal 008080 thistle d8bfd8 tomato ff6347
turquoise 40e0d0 violet ee82ee wheat f5deb3 white ffffff whitesmoke f5f5f5 yellow ffff00
yellowgreen 9acd32
canvas ffffff canvastext 000000 linktext 0000ee visitedtext 551a8b activetext ff0000
buttonface efefef buttontext 000000 buttonborder 767676 field ffffff fieldtext 000000
graytext 6d6d6d mark ffff00 marktext 000000 selecteditem 0075ff selecteditemtext ffffff
"""
"""The CSS named colours, and the system colours as Chromium paints them in a light scheme."""


def _hex(digits: str) -> Rgba:
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), 1.0)


_NAMED: Final[dict[str, Rgba]] = {
    name: _hex(digits)
    for name, digits in zip(_NAMES.split()[::2], _NAMES.split()[1::2], strict=True)
} | {"transparent": (0, 0, 0, 0)}


CURRENT: Final = "currentcolor"


def parse_color(value: str, current: Rgba | None = None) -> Rgba | None:
    """The sRGB colour `value` paints, `current` standing for ``currentcolor``; None when the
    reader cannot tell."""
    return _parse(value, current, 0)


def _parse(value: str, current: Rgba | None, depth: int) -> Rgba | None:
    value = value.strip()
    if value == CURRENT:
        return current
    if value in _NAMED:
        return _NAMED[value]
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(d * 2 for d in digits)
        if len(digits) in (6, 8) and all(c in "0123456789abcdef" for c in digits):
            alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
            return (*_hex(digits)[:3], alpha)
        return None
    found = functions(value)
    if found is None or len(found) != 1 or not value.endswith(")"):
        return None
    name, args = found[0]
    if name in ("color-mix", "light-dark") and depth >= MAX_NESTING:
        return None
    if name == "color-mix":
        return _mix(args, current, depth + 1)
    if name == "light-dark":
        parts = split_args(args)
        return _parse(parts[0], current, depth + 1) if len(parts) == 2 else None
    channels, alpha = _channels(args)
    if channels is None or alpha is None:
        return None
    match name:
        case "rgb" | "rgba":
            rgb = _rgb(channels)
        case "hsl" | "hsla":
            rgb = _hsl(channels)
        case "hwb":
            rgb = _hwb(channels)
        case "lab" | "lch" | "oklab" | "oklch":
            rgb = _lab(name, channels)
        case "color":
            rgb = _color(channels)
        case _:
            rgb = None
    return None if rgb is None else (*rgb, alpha)


def _channels(args: str) -> tuple[list[str] | None, float | None]:
    """The channel arguments of a colour function and its alpha."""
    if "from " in f"{args} " or "(" in args:
        return None, None
    parts = [p for p in re.split(r"[\s,]+", args.replace("/", " / ").strip()) if p]
    alpha = 1.0
    if "/" in parts:
        at = parts.index("/")
        if at != len(parts) - 2:
            return None, None
        parts, given = parts[:at], parts[at + 1]
        found = _fraction(given)
        if found is None:
            return None, None
        alpha = found
    elif len(parts) == 4 and parts[0] not in _SPACES:
        found = _fraction(parts[3])
        if found is None:
            return None, None
        parts, alpha = parts[:3], found
    return parts, max(0.0, min(1.0, alpha))


def _fraction(text: str) -> float | None:
    if text == "none":
        return 0.0
    share = parse_percent(text)
    return share / 100 if share is not None else parse_amount(text)


def _number(text: str, scale: float = 1.0) -> float | None:
    """A channel: a number, or a percentage of `scale`."""
    if text == "none":
        return 0.0
    share = parse_percent(text)
    if share is not None and text.endswith("%"):
        return share / 100 * scale
    return parse_amount(text)


def _hue(text: str) -> float | None:
    if text == "none":
        return 0.0
    return parse_degrees(text) if parse_amount(text) is None else parse_amount(text)


def _rgb(parts: list[str]) -> tuple[float, float, float] | None:
    if len(parts) != 3:
        return None
    channels = [_number(p, 255) for p in parts]
    if any(c is None for c in channels):
        return None
    r, g, b = (round(max(0.0, min(255.0, c or 0.0))) for c in channels)
    return r, g, b


def _hsl(parts: list[str]) -> tuple[float, float, float] | None:
    if len(parts) != 3:
        return None
    hue, saturation, lightness = _hue(parts[0]), _number(parts[1], 100), _number(parts[2], 100)
    if hue is None or saturation is None or lightness is None:
        return None
    s, lt = max(0.0, min(1.0, saturation / 100)), max(0.0, min(1.0, lightness / 100))

    def channel(n: float) -> float:
        k = (n + hue / 30) % 12
        a = s * min(lt, 1 - lt)
        return round(255 * (lt - a * max(-1, min(k - 3, 9 - k, 1))))

    return channel(0), channel(8), channel(4)


def _hwb(parts: list[str]) -> tuple[float, float, float] | None:
    if len(parts) != 3:
        return None
    hue, white, black = _hue(parts[0]), _number(parts[1], 100), _number(parts[2], 100)
    if hue is None or white is None or black is None:
        return None
    w, k = max(0.0, white / 100), max(0.0, black / 100)
    if w + k >= 1:
        grey = round(255 * w / (w + k))
        return grey, grey, grey
    base = _hsl([f"{hue}", "100%", "50%"])
    if base is None:
        return None
    r, g, b = (round(c * (1 - w - k) + 255 * w) for c in base)
    return r, g, b


_SPACES: Final = frozenset({"srgb", "srgb-linear", "display-p3"})


def _color(parts: list[str]) -> tuple[float, float, float] | None:
    """``color(srgb ...)``, ``color(srgb-linear ...)``; ``display-p3`` read as sRGB (close
    enough for contrast)."""
    if len(parts) != 4 or parts[0] not in _SPACES:
        return None
    channels = [_number(p) for p in parts[1:]]
    if any(c is None for c in channels):
        return None
    values = [max(0.0, min(1.0, c or 0.0)) for c in channels]
    if parts[0] == "srgb-linear":
        values = [_gamma(v) for v in values]
    r, g, b = (round(255 * v) for v in values)
    return r, g, b


def _lab(name: str, parts: list[str]) -> tuple[float, float, float] | None:
    if len(parts) != 3:
        return None
    ok = name.startswith("ok")
    lightness = _number(parts[0], 1.0 if ok else 100.0)
    if lightness is None:
        return None
    lightness = max(0.0, min(1.0 if ok else 100.0, lightness))
    reach = _OK_REACH if ok else _LAB_REACH
    if name.endswith("lch"):
        chroma, hue = _number(parts[1], 0.4 if ok else 150.0), _hue(parts[2])
        if chroma is None or hue is None:
            return None
        chroma = max(0.0, min(reach, chroma))
        a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    else:
        a, b = _number(parts[1], 0.4 if ok else 125.0), _number(parts[2], 0.4 if ok else 125.0)
        if a is None or b is None:
            return None
        a, b = max(-reach, min(reach, a)), max(-reach, min(reach, b))
    lit = _oklab_to_linear(lightness, a, b) if ok else _lab_to_linear(lightness, a, b)
    r, g, b2 = (round(255 * _gamma(max(0.0, min(1.0, c)))) for c in lit)
    return r, g, b2


_LAB_REACH: Final = 1000.0
"""How far from grey the reader takes a ``lab()`` / ``lch()`` colour's a, b or chroma: far past
what sRGB shows, and small enough to cube."""


_OK_REACH: Final = 4.0
"""The same for ``oklab()`` / ``oklch()``."""


def _gamma(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _oklab_to_linear(lightness: float, a: float, b: float) -> tuple[float, float, float]:
    lc = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    mc = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    sc = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc,
        -1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc,
        -0.0041960863 * lc - 0.7034186147 * mc + 1.7076147010 * sc,
    )


def _linear_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    lc = _cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    mc = _cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    sc = _cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * lc + 0.7936177850 * mc - 0.0040720468 * sc,
        1.9779984951 * lc - 2.4285922050 * mc + 0.4505937099 * sc,
        0.0259040371 * lc + 0.7827717662 * mc - 0.8086757660 * sc,
    )


def _cbrt(value: float) -> float:
    return math.copysign(abs(value) ** (1 / 3), value)


def _lab_to_linear(lightness: float, a: float, b: float) -> tuple[float, float, float]:
    """CIE Lab (D50) to linear sRGB (D65, Bradford-adapted)."""
    fy = (lightness + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200

    def inverse(f: float) -> float:
        return f**3 if f**3 > 216 / 24389 else (116 * f - 16) / (24389 / 27)

    x, y, z = inverse(fx) * 0.96422, inverse(fy), inverse(fz) * 0.82521
    return (
        3.1341359569958707 * x - 1.6173863321612538 * y - 0.4906619460083532 * z,
        -0.978795502912089 * x + 1.916254567259524 * y + 0.03344273116131949 * z,
        0.07195537988411677 * x - 0.2289768264158322 * y + 1.405386058324125 * z,
    )


def _mix(args: str, current: Rgba | None, depth: int) -> Rgba | None:
    """``color-mix()``: premultiplied interpolation in Oklab for the Lab-family spaces, else in
    sRGB."""
    parts = split_args(args)
    if len(parts) != 3 or not parts[0].startswith("in "):
        return None
    space = parts[0].split()[1] if len(parts[0].split()) > 1 else ""
    colours: list[Rgba] = []
    shares: list[float | None] = []
    for part in parts[1:]:
        tokens = part.rsplit(" ", 1)
        share = parse_percent(tokens[-1]) if len(tokens) == 2 else None
        text = tokens[0] if share is not None else part
        colour = _parse(text, current, depth)
        if colour is None or (share is not None and not 0 <= share <= 100):
            return None
        colours.append(colour)
        shares.append(share)
    first, second = shares
    if first is None and second is None:
        first, second = 50.0, 50.0
    elif first is None:
        first = 100 - (second or 0.0)
    elif second is None:
        second = 100 - first
    total = first + (second or 0.0)
    if total <= 0:
        return None
    weight = first / total
    scale = min(total, 100.0) / 100
    (r1, g1, b1, a1), (r2, g2, b2, a2) = colours
    alpha = a1 * weight + a2 * (1 - weight)
    if alpha <= 0:
        return (0, 0, 0, 0)
    if space in ("oklab", "oklch", "lab", "lch"):
        one = _linear_to_oklab(*(linear(c / 255) for c in (r1, g1, b1)))
        two = _linear_to_oklab(*(linear(c / 255) for c in (r2, g2, b2)))
        mixed = [
            (x * a1 * weight + y * a2 * (1 - weight)) / alpha for x, y in zip(one, two, strict=True)
        ]
        lit = _oklab_to_linear(*mixed)
        rgb = [round(255 * _gamma(max(0.0, min(1.0, c)))) for c in lit]
    elif space in ("srgb", "srgb-linear", "hsl", "hwb", "xyz", "xyz-d50", "xyz-d65"):
        rgb = [
            round((x * a1 * weight + y * a2 * (1 - weight)) / alpha)
            for x, y in zip((r1, g1, b1), (r2, g2, b2), strict=True)
        ]
    else:
        return None
    return (rgb[0], rgb[1], rgb[2], alpha * scale)
