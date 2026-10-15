"""The fingerprint of the dependency directories the re-run links instead of copying: a change
made to them during the session shows, the bytecode Python writes as it runs does not."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from evidence_judge.sessions.fingerprint import changes, fingerprint, load
from evidence_judge.sessions.rerun import RerunSpec, RerunStatus, execute
from tests.unit.test_sessions_rerun import PYTEST, toy


def venv(root: Path) -> Path:
    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    (site / "pkg").mkdir(parents=True)
    (site / "pkg" / "__init__.py").write_text("X = 1\n")
    (site / "pkg-1.0.dist-info").mkdir()
    (site / "pkg-1.0.dist-info" / "METADATA").write_text("Name: pkg\n")
    (site / "_editable.pth").write_text(str(root / "src") + "\n")
    (root / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return site


def node_modules(root: Path, version: str = "1.0.0") -> Path:
    modules = root / "node_modules"
    for name in ("left-pad", "@scope/util"):
        (modules / name).mkdir(parents=True, exist_ok=True)
        (modules / name / "package.json").write_text(json.dumps({"name": name, "version": version}))
    (modules / ".bin").mkdir(exist_ok=True)
    return modules


def test_an_unchanged_environment_has_no_changes(tmp_path: Path) -> None:
    site = venv(tmp_path)
    node_modules(tmp_path)
    before = fingerprint(tmp_path)
    (site / "pkg" / "__pycache__").mkdir()
    (site / "pkg" / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"x")
    (tmp_path / "node_modules" / ".cache").mkdir()
    assert changes(before, fingerprint(tmp_path)) == []
    assert set(before["dirs"]) == {".venv", "node_modules"}  # type: ignore[arg-type]


def test_an_installed_package_shows(tmp_path: Path) -> None:
    site = venv(tmp_path)
    before = fingerprint(tmp_path)
    (site / "evil-0.1.dist-info").mkdir()
    (site / "evil-0.1.dist-info" / "entry_points.txt").write_text("[pytest11]\nevil = evil\n")
    assert changes(before, fingerprint(tmp_path)) == [".venv: added evil-0.1.dist-info"]


def test_a_changed_pth_file_shows(tmp_path: Path) -> None:
    site = venv(tmp_path)
    before = fingerprint(tmp_path)
    (site / "_editable.pth").write_text("import evil\n")
    assert changes(before, fingerprint(tmp_path)) == [
        ".venv: changed lib/python3.12/site-packages/_editable.pth"
    ]


def test_an_edit_inside_a_package_shows_even_with_its_times_put_back(tmp_path: Path) -> None:
    site = venv(tmp_path)
    module = site / "pkg" / "__init__.py"
    stat = module.stat()
    before = fingerprint(tmp_path)
    time.sleep(0.01)
    module.write_text("X = 2\n")
    os.utime(module, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert changes(before, fingerprint(tmp_path)) == [".venv: files changed in place"]


def test_node_modules_versions_show(tmp_path: Path) -> None:
    node_modules(tmp_path)
    before = fingerprint(tmp_path)
    time.sleep(0.01)
    node_modules(tmp_path, "2.0.0")
    [line] = changes(before, fingerprint(tmp_path))
    assert line.startswith("node_modules: added @scope/util@2.0.0, left-pad@2.0.0; removed")


def test_created_and_removed_directories_show(tmp_path: Path) -> None:
    before = fingerprint(tmp_path)
    venv(tmp_path)
    assert changes(before, fingerprint(tmp_path)) == [".venv was created"]
    assert changes(fingerprint(tmp_path), before) == [".venv was removed"]


def test_an_unreadable_baseline_is_a_change(tmp_path: Path) -> None:
    assert changes({"format": 99}, fingerprint(tmp_path)) != []
    (tmp_path / "bad.json").write_text("[1]")
    assert load(tmp_path / "bad.json") is None
    assert load(tmp_path / "missing.json") is None


def test_a_rerun_against_a_changed_environment_needs_review(tmp_path: Path) -> None:
    root = toy(tmp_path)
    site = venv(root)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(fingerprint(root)))
    same = execute(RerunSpec(command=PYTEST), root, baseline=baseline)
    assert same.status is RerunStatus.PASS
    assert same.problems == []
    (site / "zz_plugin.pth").write_text("import os\n")
    changed = execute(RerunSpec(command=PYTEST), root, baseline=baseline)
    assert changed.status is RerunStatus.PASS
    assert changed.problems == [
        "the environment changed during the session (.venv: changed "
        "lib/python3.12/site-packages/zz_plugin.pth); the re-run used the changed environment"
    ]
    missing = execute(RerunSpec(command=PYTEST), root, baseline=tmp_path / "missing.json")
    assert "missing or unreadable" in missing.problems[0]
