"""Every `EVJ_HOOK_*` setting is in the configuration reference and the Claude Code guide."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evidence_judge.hooks.settings import HookSettings

ROOT = Path(__file__).resolve().parents[2]
PAGES = ("docs/reference/configuration.md", "docs/guides/claude-code.md")
SCRIPT_ONLY = ("EVJ_HOOK_ALLOW_MISSING_JUDGE",)
DIRECTORIES = ("EVJ_HOOK_LOG_DIR", "EVJ_HOOK_STATE_DIR")


def _rows(page: str) -> dict[str, str]:
    text = (ROOT / page).read_text(encoding="utf-8")
    return {
        m.group(1): line
        for line in text.splitlines()
        if (m := re.match(r"\| `(EVJ_HOOK_\w+)`", line))
    }


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("field", sorted(HookSettings.model_fields))
def test_every_hook_setting_is_documented(page: str, field: str) -> None:
    rows = _rows(page)
    env = f"EVJ_HOOK_{field.upper()}"
    assert env in rows, f"{env} missing from {page}"
    default = HookSettings.model_fields[field].default
    if isinstance(default, bool):
        assert f"`{str(default).lower()}`" in rows[env], (env, default)
    elif isinstance(default, int | float | str):
        assert re.search(rf"`{re.escape(str(default)).removesuffix(r'\.0')}", rows[env]), env


@pytest.mark.parametrize("page", PAGES)
def test_hook_variables_read_outside_the_settings_are_documented(page: str) -> None:
    rows = _rows(page)
    assert [env for env in (*SCRIPT_ONLY, *DIRECTORIES) if env not in rows] == []
    script = (ROOT / "plugins/claude-code/evidence-judge/hooks/run-hook.sh").read_text("utf-8")
    assert all(env in script for env in SCRIPT_ONLY)
