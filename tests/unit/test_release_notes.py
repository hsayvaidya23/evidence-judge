from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "not released yet"
STALE = re.compile(r"on PyPI yet|tagged yet|untagged|until v0\.1\.0 is", re.IGNORECASE)


def _version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def _pages() -> list[Path]:
    return [
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        *sorted((ROOT / "docs").rglob("*.md")),
        ROOT / "src" / "evidence_judge" / "_extras.py",
    ]


def _notes() -> list[str]:
    return [
        f"{page.relative_to(ROOT)}:{number}"
        for page in _pages()
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        if MARKER in line.lower()
    ]


def test_a_release_leaves_no_pre_release_note() -> None:
    version = _version()
    if ".dev" not in version:
        assert not _notes(), f"version {version} is a release; update these notes: {_notes()}"


def test_pre_release_notes_use_the_shared_marker() -> None:
    stale = [
        f"{page.relative_to(ROOT)}:{number}"
        for page in _pages()
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        if STALE.search(line)
    ]
    assert not stale, f"say {MARKER!r} so the release check finds these: {stale}"
