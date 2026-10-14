import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "docs" / "assets"
EXPECTED = {
    "demo-forged.svg",
    "demo-honest.svg",
    "mascot.svg",
    "how-it-works-light.svg",
    "how-it-works-dark.svg",
    "benchmark-light.svg",
    "benchmark-dark.svg",
}
SIZE_BUDGET = 150 * 1024
SVGS = sorted(ASSETS.glob("*.svg"))
HREF_ATTRS = {"href", "{http://www.w3.org/1999/xlink}href"}


def test_expected_assets_exist() -> None:
    assert EXPECTED.issubset(p.name for p in SVGS)


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_svg_parses(path: Path) -> None:
    root = ET.parse(path).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("viewBox")


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
    assert not re.search(r"url\(\s*['\"]?(?!#)", text)
    assert "<script" not in text.lower()


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_svg_respects_reduced_motion(path: Path) -> None:
    assert re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)", path.read_text("utf-8"))


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_svg_size_budget(path: Path) -> None:
    assert path.stat().st_size < SIZE_BUDGET
