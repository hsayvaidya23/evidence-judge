from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "not released yet"
STALE = re.compile(r"on PyPI yet|tagged yet|untagged|until v0\.1\.0 is", re.IGNORECASE)


def _released() -> bool:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return (
        re.search(r"^## \[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2}$", changelog, re.MULTILINE)
        is not None
    )


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
    if _released():
        assert not _notes(), f"a release is out; update these notes: {_notes()}"


def test_pre_release_notes_use_the_shared_marker() -> None:
    stale = [
        f"{page.relative_to(ROOT)}:{number}"
        for page in _pages()
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        if STALE.search(line)
    ]
    assert not stale, f"say {MARKER!r} so the release check finds these: {stale}"


PYPI_REQUIREMENT = re.compile(
    r"""evidence-judge\[[a-z,]*\]["']|(?:pip|pipx|uv tool|uv pip) install +["']?evidence-judge\b(?![\[-])|uvx +evidence-judge\b"""
)


def test_pre_release_snippets_install_from_the_repository() -> None:
    if _released():
        return
    pages = [
        *_pages(),
        ROOT / "CONTRIBUTING.md",
        *sorted(path for path in (ROOT / "plugins").rglob("*") if path.is_file()),
    ]
    bare = [
        f"{page.relative_to(ROOT)}:{number}"
        for page in pages
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        if PYPI_REQUIREMENT.search(line)
    ]
    assert not bare, f"evidence-judge is not on PyPI; install it from git+https://... in {bare}"
