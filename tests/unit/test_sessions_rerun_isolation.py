"""What the re-run can read from outside its copy: configuration above the copy, bytecode next to
the sources, the report directory, and an optional sandbox around the command."""

from __future__ import annotations

import os
import py_compile
import tempfile
from importlib.util import cache_from_source
from pathlib import Path

import pytest

from evidence_judge.sessions.rerun import RerunSpec, RerunStatus, configuration_above, execute
from tests.unit.test_sessions_rerun import PYTEST, TESTS, committed, needs_git, toy

ONLY_ZERO = "[pytest]\naddopts = -k zero\n"


def bare_toy(tmp_path: Path, *, buggy: bool) -> Path:
    """A project with no pytest configuration of its own, so pytest looks further up."""
    root = tmp_path / "work" / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(f"def add(a, b):\n    return a {'-' if buggy else '+'} b\n")
    (root / "tests" / "test_calc.py").write_text(TESTS)
    (root / "conftest.py").write_text(
        "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n"
    )
    return root


def test_configuration_above_the_project_never_reaches_the_run(tmp_path: Path) -> None:
    root = bare_toy(tmp_path, buggy=True)
    (tmp_path / "work" / "pytest.ini").write_text(ONLY_ZERO)
    (tmp_path / "work" / "conftest.py").write_text(
        "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n    yield\n"
    )
    result = execute(RerunSpec(command=PYTEST), root)
    assert result.status is RerunStatus.FAIL
    assert result.failed == 2
    assert result.problems == []


def test_configuration_above_the_copy_needs_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "pytest.ini").write_text(ONLY_ZERO)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    result = execute(RerunSpec(command=PYTEST), bare_toy(tmp_path, buggy=True))
    assert result.passed == 1
    assert any(f"{temp.resolve()}/pytest.ini sits above" in p for p in result.problems)


def test_configuration_above_depends_on_the_runner(tmp_path: Path) -> None:
    for name in ("pytest.ini", "go.work", "node_modules", "Cargo.toml"):
        (tmp_path / name).touch()
    copy = tmp_path / "scratch" / "tree"
    names = {
        runner: [Path(p).name for p in configuration_above(copy, runner, {})]
        for runner in ("pytest", "go", "node", "cargo", "unittest")
    }
    assert names == {
        "pytest": ["pytest.ini"],
        "go": ["go.work"],
        "node": ["node_modules"],
        "cargo": ["Cargo.toml"],
        "unittest": [],
    }
    cargo_home = tmp_path / ".cargo"
    cargo_home.mkdir()
    assert configuration_above(copy, "cargo", {"CARGO_HOME": str(cargo_home)}) == [
        str(tmp_path / "Cargo.toml")
    ]


@needs_git
def test_planted_bytecode_is_never_loaded(tmp_path: Path) -> None:
    root = committed(toy(tmp_path, buggy=True))
    source = root / "calc.py"
    py_compile.compile(str(source), cfile=cache_from_source(str(source)), doraise=True)
    stat = source.stat()
    source.write_text(source.read_text().replace("a - b", "a + b"))
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    result = execute(RerunSpec(command=PYTEST), root)
    assert result.status is RerunStatus.PASS, result.reason
