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


def test_docker_image_pins_bases_by_digest_and_installs_from_the_lockfile() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bases = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert bases
    assert all("@sha256:" in base for base in bases)
    assert "uv export --frozen" in dockerfile
    assert "--require-hashes" in dockerfile


def test_release_runs_the_full_ci_before_publishing() -> None:
    import yaml

    workflows = ROOT / ".github" / "workflows"
    ci = yaml.safe_load((workflows / "ci.yml").read_text(encoding="utf-8"))
    release = yaml.safe_load((workflows / "release.yml").read_text(encoding="utf-8"))
    assert "workflow_call" in ci[True]
    jobs = release["jobs"]
    [gate] = [name for name, job in jobs.items() if job.get("uses") == "./.github/workflows/ci.yml"]
    assert gate in jobs["build"]["needs"]
    assert jobs["publish"]["needs"] == "build"


def _action() -> dict[str, Any]:
    import yaml

    return yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))


def test_action_documents_the_reasoning_file_format_the_loader_reads() -> None:
    description = _action()["inputs"]["reasoning-file"]["description"]
    assert "paragraph" in description
    assert "blank line" in description


def test_action_version_installs_from_the_repository_not_pypi() -> None:
    action = _action()
    install = next(s for s in action["runs"]["steps"] if s.get("name") == "Install evidence-judge")
    assert "git+https://github.com/hsayvaidya23/evidence-judge@" in install["run"]
    assert "==" not in install["run"]
    assert "tag" in action["inputs"]["version"]["description"]
