"""The README configuration table and docs/cli.md must match the code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer.main
from typer.core import TyperGroup

from evidence_judge.cli import app
from evidence_judge.config import Settings

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
CLI_DOC = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
DOCUMENTED_COMMANDS = ("judge", "inspect", "bench")


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##+ (?:[^\w\s`]+\s*)?{re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S
    )
    assert match is not None, f"missing section {heading!r}"
    return match.group(1)


def _env_name(name: str) -> str:
    alias = Settings.model_fields[name].validation_alias
    return alias if isinstance(alias, str) else f"EVJ_{name.upper()}"


def _config_rows() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in _section(README, "Configuration").splitlines():
        for env in re.findall(
            r"`((?:EVJ_|ANTHROPIC_)[A-Z_]+)`", line.split("|")[1] if "|" in line else ""
        ):
            rows[env] = line
    return rows


@pytest.mark.parametrize("field", sorted(Settings.model_fields))
def test_every_setting_is_in_the_readme_config_table(field: str) -> None:
    rows = _config_rows()
    env = _env_name(field)
    assert env in rows, f"{env} missing from the README configuration table"
    default = Settings.model_fields[field].default
    if isinstance(default, bool):
        assert f"`{str(default).lower()}`" in rows[env]
    elif isinstance(default, int | float | str):
        assert re.search(rf"`{re.escape(str(default))}`", rows[env]), (env, default)


def _options(command: str) -> list[str]:
    group = typer.main.get_command(app)
    assert isinstance(group, TyperGroup)
    params = group.commands[command].params
    return sorted({opt for p in params for opt in p.opts if opt.startswith("--")})


@pytest.mark.parametrize("command", DOCUMENTED_COMMANDS)
def test_every_cli_option_is_in_docs_cli(command: str) -> None:
    section = _section(CLI_DOC, f"`evidence-judge {command}`")
    missing = [o for o in _options(command) if not re.search(rf"`{re.escape(o)}[` ]", section)]
    assert missing == [], f"docs/cli.md is missing {missing} for {command}"


def test_readme_inspect_usage_matches_the_cli() -> None:
    for usage in re.findall(r"evidence-judge inspect[^`\n]*", README):
        assert re.search(r"inspect --(trace|har|browser-use) ", usage), usage
