from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_otel_extra_ships_an_otlp_exporter() -> None:
    otel = _pyproject()["project"]["optional-dependencies"]["otel"]
    assert any(d.startswith("opentelemetry-exporter-otlp-proto-http") for d in otel)


def test_sdist_ships_what_the_test_suite_imports() -> None:
    include = set(_pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])
    assert {"src/evidence_judge", "tests", "scripts", "demo"} <= include
