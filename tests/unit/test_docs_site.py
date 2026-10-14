"""The MkDocs site: nav, page content that must match the code, and a strict build."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from evidence_judge.config import Settings
from evidence_judge.core.taskspec import TaskSpec

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
NOT_PAGES = ("includes", "overrides")


class _Loader(yaml.SafeLoader):
    """Reads mkdocs.yml, ignoring its `!!python/...` tags."""


_Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda *_: None)


def _config() -> dict[str, Any]:
    return yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_Loader)


def _nav_files(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _nav_files(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _nav_files(value)


def _pages() -> list[Path]:
    return [
        p
        for p in DOCS.rglob("*.md")
        if p.relative_to(DOCS).parts[0] not in NOT_PAGES and "assets" not in p.parts
    ]


def _outside_fences(text: str) -> Iterator[str]:
    fence: str | None = None
    for line in text.splitlines():
        marker = re.match(r"\s*(```|~~~)", line)
        if marker:
            fence = None if fence == marker.group(1) else fence or marker.group(1)
            continue
        if fence is None:
            yield line


def _hooks() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docs_hooks", DOCS / "overrides" / "hooks.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nav_entries_point_to_existing_files() -> None:
    missing = [f for f in _nav_files(_config()["nav"]) if not (DOCS / f).is_file()]
    assert missing == []


def test_every_page_is_in_the_nav() -> None:
    nav = set(_nav_files(_config()["nav"]))
    orphans = [str(p.relative_to(DOCS)) for p in _pages() if str(p.relative_to(DOCS)) not in nav]
    assert orphans == []


def test_extra_css_and_js_exist() -> None:
    config = _config()
    for asset in [*config["extra_css"], *config["extra_javascript"]]:
        assert (DOCS / asset).is_file(), asset


@pytest.mark.parametrize("page", _pages(), ids=lambda p: str(p.relative_to(DOCS)))
def test_no_accidental_headings(page: Path) -> None:
    bad = [
        line for line in _outside_fences(page.read_text(encoding="utf-8")) if re.match(r"#\w", line)
    ]
    assert bad == [], f"a wrapped line starts with '#', which renders as a heading: {bad}"


def _env_name(name: str) -> str:
    alias = Settings.model_fields[name].validation_alias
    return alias if isinstance(alias, str) else f"EVJ_{name.upper()}"


@pytest.mark.parametrize("field", sorted(Settings.model_fields))
def test_configuration_page_lists_every_setting(field: str) -> None:
    text = (DOCS / "reference" / "configuration.md").read_text(encoding="utf-8")
    rows = {m.group(1): line for line in text.splitlines() if (m := re.match(r"\| `(\w+)`", line))}
    env = _env_name(field)
    assert env in rows, f"{env} missing from docs/reference/configuration.md"
    default = Settings.model_fields[field].default
    if isinstance(default, bool):
        assert f"`{str(default).lower()}`" in rows[env]
    elif isinstance(default, int | float | str):
        assert re.search(rf"`{re.escape(str(default))}`", rows[env]), (env, default)


def _cookbook_specs() -> list[str]:
    text = (DOCS / "guides" / "spec-cookbook.md").read_text(encoding="utf-8")
    return re.findall(r'```yaml title="specs/[^"]+"\n(.*?)```', text, re.S)


def test_cookbook_has_at_least_eight_recipes() -> None:
    assert len(_cookbook_specs()) >= 8


@pytest.mark.parametrize("source", _cookbook_specs(), ids=lambda s: s.split("\n", 1)[0])
def test_cookbook_specs_are_valid(source: str) -> None:
    TaskSpec.model_validate(yaml.safe_load(source))


def test_hooks_rewrite_links_that_leave_docs() -> None:
    hooks = _hooks()
    assert hooks._rewrite("../SECURITY.md", "docs", "") == "security.md"
    assert hooks._rewrite("../README.md#configuration", "docs", "") == (
        "reference/configuration.md"
    )
    assert hooks._rewrite("../../bench/results/x.md", "docs/guides", "guides") == (
        "https://github.com/hsayvaidya23/evidence-judge/blob/main/bench/results/x.md"
    )
    assert hooks._rewrite("SECURITY.md", "", "") == "security.md"
    assert hooks._rewrite("docs/cli.md", "", "reference") == "../cli.md"
    assert hooks._rewrite("https://example.com/a.md", "docs", "") == "https://example.com/a.md"
    assert hooks._rewrite("#anchor", "docs", "") == "#anchor"


def test_hooks_leave_code_fences_alone() -> None:
    text = "[a](../SECURITY.md)\n```\n[b](../SECURITY.md)\n```\n"
    out = _hooks()._links(text, "docs", "")
    assert out == "[a](security.md)\n```\n[b](../SECURITY.md)\n```\n"


@pytest.mark.skipif(
    importlib.util.find_spec("mkdocs") is None or importlib.util.find_spec("material") is None,
    reason="docs dependency group not installed (uv sync --group docs)",
)
def test_strict_build_succeeds(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "playground" / "index.html").is_file()
