"""The README media in docs/assets/ exists, stays within its size budget and has sane dimensions."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[2] / "docs" / "assets"
PNG_BUDGET = 400 * 1024
ANIMATION_BUDGET = 3 * 1024 * 1024

PNGS = {
    "report-fail-light.png": 1200,
    "report-fail-dark.png": 1200,
    "report-pass-light.png": 1200,
    "report-pass-dark.png": 1200,
    "report-mobile.png": 780,
    "social-card.png": 1280,
}
ANIMATIONS = ("demo-agent-run.webp", "demo-verdict.webp")


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def webp_animation(data: bytes) -> tuple[int, int, int, int]:
    """Canvas width and height, frame count and total duration in ms of an animated WebP."""
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert data[12:16] == b"VP8X"
    width = int.from_bytes(data[24:27], "little") + 1
    height = int.from_bytes(data[27:30], "little") + 1
    frames = total_ms = 0
    pos = 12
    while pos + 8 <= len(data):
        tag = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if tag == b"ANMF":
            frames += 1
            total_ms += int.from_bytes(data[pos + 20 : pos + 23], "little")
        pos += 8 + size + (size & 1)
    return width, height, frames, total_ms


@pytest.mark.parametrize(("name", "width"), PNGS.items())
def test_png_within_budget(name: str, width: int) -> None:
    data = (ASSETS / name).read_bytes()
    assert len(data) < PNG_BUDGET
    w, h = png_size(data)
    assert w == width
    assert 300 <= h <= 3200


def test_social_card_is_preview_sized() -> None:
    assert png_size((ASSETS / "social-card.png").read_bytes()) == (1280, 640)


@pytest.mark.parametrize("name", ANIMATIONS)
def test_animation_within_budget(name: str) -> None:
    data = (ASSETS / name).read_bytes()
    assert len(data) < ANIMATION_BUDGET
    width, height, frames, total_ms = webp_animation(data)
    assert 900 <= width <= 1200
    assert 400 <= height <= 900
    assert frames > 10
    assert 15_000 <= total_ms <= 25_000
