from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "evidence_judge"
    / "report"
    / "templates"
    / "report.html.j2"
)

TEXT_TOKENS = ("text", "text-2", "text-3", "muted")
TINTED_TOKENS = ("muted", "violet", "cyan", "ok", "bad", "warn")
MAX_TINT = 0.13
AA = 4.5

Rgba = tuple[float, float, float, float]


def _block(css: str, mode: str) -> str:
    if mode == "dark":
        css = css.split("@media (prefers-color-scheme: dark)", 1)[1]
    return css.split(":root {", 1)[1].split("}", 1)[0]


def _tokens(mode: str) -> dict[str, Rgba]:
    found = re.findall(
        r"--([\w-]+):\s*([^;]+);", _block(TEMPLATE.read_text(encoding="utf-8"), mode)
    )
    return {name: c for name, value in found if (c := _parse(value)) is not None}


def _parse(value: str) -> Rgba | None:
    value = value.strip()
    if m := re.fullmatch(r"#([0-9a-fA-F]{6})", value):
        h = m[1]
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    if m := re.fullmatch(r"rgba\(([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\)", value):
        return (float(m[1]), float(m[2]), float(m[3]), float(m[4]))
    return None


def _over(fg: Rgba, bg: Rgba, alpha: float | None = None) -> Rgba:
    a = fg[3] if alpha is None else alpha
    r, g, b = (fg[i] * a + bg[i] * (1 - a) for i in range(3))
    return (r, g, b, 1.0)


def _luminance(c: Rgba) -> float:
    def channel(x: float) -> float:
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2])


def _contrast(a: Rgba, b: Rgba) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _surfaces(t: dict[str, Rgba]) -> list[Rgba]:
    out: list[Rgba] = []
    for base in (t["bg"], t["bg-2"]):
        panel = _over(t["panel"], base)
        out += [base, panel, _over(t["panel-2"], panel)]
    return out


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("token", sorted({*TEXT_TOKENS, *TINTED_TOKENS}))
def test_text_tokens_meet_wcag_aa_on_every_surface(mode: str, token: str) -> None:
    t = _tokens(mode)
    worst = min(_contrast(t[token], s) for s in _surfaces(t))
    assert worst >= AA, f"--{token} ({mode}) is {worst:.2f}:1"


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("token", TINTED_TOKENS)
def test_tone_tokens_meet_wcag_aa_on_their_own_tint(mode: str, token: str) -> None:
    t = _tokens(mode)
    fg = t[token]
    worst = min(_contrast(fg, _over(fg, s, MAX_TINT)) for s in _surfaces(t))
    assert worst >= AA, f"--{token} ({mode}) on its pill tint is {worst:.2f}:1"


def test_text_tints_stay_within_the_checked_strength() -> None:
    css = TEMPLATE.read_text(encoding="utf-8")
    tone = r"var\(--(?:t|warn|cyan|violet)\)"
    backgrounds = [
        int(p)
        for p in re.findall(rf"background: color-mix\(in srgb, {tone} (\d+)%, transparent\)", css)
    ]
    assert backgrounds
    assert max(backgrounds) <= MAX_TINT * 100
