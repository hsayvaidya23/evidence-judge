import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "docs" / "assets"
EXPECTED = {
    "hero.svg",
    "mascot.svg",
    "demo-forged.svg",
    "demo-honest.svg",
    "how-it-works.svg",
    "benchmark.svg",
}
TRANSPARENT = {"mascot.svg"}
SIZE_BUDGET = 150 * 1024
SVGS = sorted(ASSETS.glob("*.svg"))
CARDS = [p for p in SVGS if p.name not in TRANSPARENT]
SVG_NS = "{http://www.w3.org/2000/svg}"
HREF_ATTRS = {"href", "{http://www.w3.org/1999/xlink}href"}
NON_RENDERED = {"defs", "clipPath", "pattern", "mask", "marker", "symbol", "filter"}


def view_box(root: ET.Element) -> tuple[float, float, float, float]:
    x, y, w, h = (float(v) for v in (root.get("viewBox") or "").split())
    return x, y, w, h


def rendered_rects(root: ET.Element) -> list[ET.Element]:
    found: list[ET.Element] = []

    def walk(el: ET.Element) -> None:
        for child in el:
            tag = child.tag.removeprefix(SVG_NS)
            if tag in NON_RENDERED:
                continue
            if tag == "rect":
                found.append(child)
            walk(child)

    walk(root)
    return found


def test_asset_set_matches_the_generator() -> None:
    assert {p.name for p in SVGS} == EXPECTED


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_svg_parses(path: Path) -> None:
    root = ET.parse(path).getroot()
    assert root.tag == f"{SVG_NS}svg"
    _, _, w, h = view_box(root)
    assert w > 0 and h > 0
    assert root.get("role") == "img"
    assert root.find(f"{SVG_NS}title") is not None


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_svg_is_self_contained(path: Path) -> None:
    root = ET.parse(path).getroot()
    for el in root.iter():
        assert el.tag.rsplit("}", 1)[-1] not in {"script", "foreignObject", "image"}
        for name, value in el.attrib.items():
            assert not name.startswith("on"), name
            if name in HREF_ATTRS:
                assert value.startswith("#"), value
    text = path.read_text(encoding="utf-8")
    assert "@import" not in text
    assert "@font-face" not in text
    assert not re.search(r"url\(\s*['\"]?(?!#)", text)
    assert "<script" not in text.lower()


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_svg_respects_reduced_motion(path: Path) -> None:
    text = path.read_text("utf-8")
    assert "@keyframes" in text
    assert re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{\s*\*\s*\{\s*animation:\s*none\s*!important",
        text,
    )


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_svg_size_budget(path: Path) -> None:
    assert path.stat().st_size < SIZE_BUDGET


@pytest.mark.parametrize("path", CARDS, ids=lambda p: p.name)
def test_card_fills_its_view_box(path: Path) -> None:
    root = ET.parse(path).getroot()
    vx, vy, vw, vh = view_box(root)
    rects = rendered_rects(root)
    assert rects, "no background rect"
    bg = rects[0]
    x, y = float(bg.get("x", "0")), float(bg.get("y", "0"))
    w, h = float(bg.get("width", "0")), float(bg.get("height", "0"))
    assert (x, y) == (vx, vy)
    assert w * h >= 0.95 * vw * vh
    assert (bg.get("fill") or "").lower() not in {"#fff", "#ffffff", "white"}


def test_mascot_has_no_backdrop() -> None:
    root = ET.parse(ASSETS / "mascot.svg").getroot()
    _, _, vw, vh = view_box(root)
    for rect in rendered_rects(root):
        area = float(rect.get("width", "0")) * float(rect.get("height", "0"))
        assert area < 0.5 * vw * vh
