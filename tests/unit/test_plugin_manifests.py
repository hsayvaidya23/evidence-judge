"""The shipped Claude Code plugin, marketplace and Gemini CLI extension match the documented
manifest schemas and only call hook commands the CLI has.

Schemas: https://code.claude.com/docs/en/plugins/manifest-reference,
https://code.claude.com/docs/en/plugins/marketplace-reference,
https://code.claude.com/docs/en/hooks,
https://github.com/google-gemini/gemini-cli/blob/main/docs/extensions/reference.md and
https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import typer.main
import yaml
from typer.core import TyperGroup

from evidence_judge.hooks.cli import hook_app

ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
CLAUDE_PLUGIN = ROOT / "plugins" / "claude-code" / "evidence-judge"
GEMINI_EXTENSION = ROOT / "plugins" / "gemini-cli" / "evidence-judge"
SHIMS = [CLAUDE_PLUGIN / "hooks" / "run-hook.sh", GEMINI_EXTENSION / "hooks" / "run-hook.sh"]
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MARKETPLACE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CLAUDE_EVENTS = {
    "SessionStart", "Setup", "UserPromptSubmit", "UserPromptExpansion", "PreToolUse",
    "PermissionRequest", "PermissionDenied", "PostToolUse", "PostToolUseFailure",
    "PostToolBatch", "Notification", "MessageDisplay", "SubagentStart", "SubagentStop",
    "TaskCreated", "TaskCompleted", "Stop", "StopFailure", "TeammateIdle",
    "InstructionsLoaded", "ConfigChange", "CwdChanged", "DirectoryAdded", "FileChanged",
    "WorktreeCreate", "WorktreeRemove", "PreCompact", "PostCompact", "PreModelSwitch",
    "PostModelSwitch", "Elicitation", "ElicitationResult", "SessionEnd",
}  # fmt: skip
GEMINI_EVENTS = {
    "BeforeTool", "AfterTool", "BeforeAgent", "AfterAgent", "BeforeModel", "AfterModel",
    "BeforeToolSelection", "SessionStart", "SessionEnd", "Notification", "PreCompress",
}  # fmt: skip


def _json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _hook_commands() -> set[str]:
    group = typer.main.get_command(hook_app)
    assert isinstance(group, TyperGroup)
    return set(group.commands)


def _release() -> str:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    return re.sub(r"(\.dev\d+|a\d+|b\d+|rc\d+)$", "", version)


def _handlers(hooks: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    found: list[tuple[str, dict[str, object]]] = []
    for event, groups in hooks.items():
        assert isinstance(groups, list)
        for group in groups:
            assert isinstance(group["hooks"], list) and group["hooks"]
            for handler in group["hooks"]:
                found.append((event, handler))
    return found


def _adapter(command: str) -> str:
    match = re.search(r'run-hook\.sh" ([a-z-]+)', command)
    assert match, command
    return match.group(1)


def test_marketplace_manifest() -> None:
    market = _json(MARKETPLACE)
    assert MARKETPLACE_NAME.match(str(market["name"]))
    assert isinstance(market["owner"], dict) and market["owner"]["name"]
    assert market.get("description")
    plugins = market["plugins"]
    assert isinstance(plugins, list) and len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == _json(CLAUDE_PLUGIN / ".claude-plugin" / "plugin.json")["name"]
    source = entry["source"]
    assert isinstance(source, str) and source.startswith("./")
    assert (ROOT / source).resolve() == CLAUDE_PLUGIN.resolve()


def test_plugin_manifest() -> None:
    manifest = _json(CLAUDE_PLUGIN / ".claude-plugin" / "plugin.json")
    assert KEBAB.match(str(manifest["name"]))
    assert manifest["version"] == _release()
    assert manifest["description"]
    assert isinstance(manifest["author"], dict) and manifest["author"]["name"]
    assert manifest["license"] == "MIT"
    assert sorted(p.name for p in (CLAUDE_PLUGIN / ".claude-plugin").iterdir()) == ["plugin.json"]


def test_plugin_hooks_use_documented_events_and_real_commands() -> None:
    hooks = _json(CLAUDE_PLUGIN / "hooks" / "hooks.json")
    assert set(hooks) == {"hooks"}
    handlers = _handlers(hooks["hooks"])  # type: ignore[arg-type]
    assert {event for event, _ in handlers} <= CLAUDE_EVENTS
    commands = _hook_commands()
    for event, handler in handlers:
        assert handler["type"] == "command"
        command = str(handler["command"])
        assert command.startswith('sh "${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.sh" ')
        adapter = _adapter(command)
        assert adapter in commands
        if adapter != "claude-code-turn-start":
            assert '--log-dir "${CLAUDE_PLUGIN_DATA}/evidence"' in command
        if event in ("Stop", "SubagentStop"):
            assert adapter == "claude-code-stop"
            assert "async" not in handler
            assert 60 < int(str(handler["timeout"])) <= 600
            assert '--state-dir "${CLAUDE_PLUGIN_DATA}/state"' in command
        elif adapter == "claude-code-turn-start":
            assert event == "UserPromptSubmit"
            assert "async" not in handler
            assert '--state-dir "${CLAUDE_PLUGIN_DATA}/state"' in command
        else:
            assert adapter == "claude-code-record"
            assert handler["async"] is True


def test_plugin_mcp_server() -> None:
    servers = _json(CLAUDE_PLUGIN / ".mcp.json")["mcpServers"]
    assert servers == {"evidence-judge": {"command": "evidence-judge", "args": ["mcp"]}}


def test_skill_frontmatter() -> None:
    text = (CLAUDE_PLUGIN / "skills" / "evidence-judge" / "SKILL.md").read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md needs YAML frontmatter on line 1"
    front = yaml.safe_load(match.group(1))
    assert front["name"] == "evidence-judge"
    assert 50 < len(front["description"]) <= 1536
    for tool in ("judge_run", "inspect_run", "explain_verdict", "validate_spec", "find_recordings"):
        assert f"`{tool}`" in text
    assert "--caps=devtools" in text
    assert "browser_start_tracing" in text


def test_gemini_extension_manifest() -> None:
    manifest = _json(GEMINI_EXTENSION / "gemini-extension.json")
    assert manifest["name"] == "evidence-judge"
    assert manifest["version"] == _release()
    assert manifest["description"]
    assert manifest["mcpServers"] == {
        "evidence-judge": {"command": "evidence-judge", "args": ["mcp"]}
    }
    assert "hooks" not in manifest


def test_gemini_extension_hooks() -> None:
    hooks = _json(GEMINI_EXTENSION / "hooks" / "hooks.json")
    handlers = _handlers(hooks["hooks"])  # type: ignore[arg-type]
    assert {event for event, _ in handlers} == {"BeforeAgent", "AfterTool", "AfterAgent"}
    assert {event for event, _ in handlers} <= GEMINI_EVENTS
    commands = _hook_commands()
    for event, handler in handlers:
        assert handler["type"] == "command"
        assert handler["name"]
        command = str(handler["command"])
        assert command.startswith('sh "${extensionPath}/hooks/run-hook.sh" ')
        adapter = _adapter(command)
        assert adapter in commands
        expected = {
            "AfterAgent": {"gemini-after-agent"},
            "BeforeAgent": {"gemini-turn-start", "gemini-record"},
            "AfterTool": {"gemini-record"},
        }
        assert adapter in expected[event]
        assert 1000 <= int(str(handler["timeout"])) <= 600_000


def test_the_shims_are_identical() -> None:
    assert SHIMS[0].read_bytes() == SHIMS[1].read_bytes()


def _shim(args: list[str], path: str, stdin: str = "{}") -> subprocess.CompletedProcess[str]:
    sh = shutil.which("sh")
    assert sh is not None
    return subprocess.run(
        [sh, str(SHIMS[0]), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env={"PATH": path, "HOME": os.environ.get("HOME", "/")},
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("adapter", ["claude-code-stop", "codex-stop", "gemini-after-agent"])
def test_a_missing_cli_allows_the_stop_with_a_notice(tmp_path: Path, adapter: str) -> None:
    done = _shim([adapter], f"{tmp_path}:/usr/bin:/bin")
    assert done.returncode == 0
    message = json.loads(done.stdout)["systemMessage"]
    assert message.startswith("evidence-judge hook: the evidence-judge CLI is not on PATH")


@pytest.mark.parametrize(
    ("adapter", "stdout"),
    [("claude-code-record", ""), ("codex-record", ""), ("gemini-record", "{}\n")],
)
def test_a_missing_cli_records_nothing(tmp_path: Path, adapter: str, stdout: str) -> None:
    done = _shim([adapter, "--log-dir", "/x"], f"{tmp_path}:/usr/bin:/bin")
    assert (done.returncode, done.stdout) == (0, stdout)


def test_the_shim_hands_over_to_the_cli(tmp_path: Path) -> None:
    fake = tmp_path / "evidence-judge"
    fake.write_text('#!/bin/sh\nprintf "%s|" "$@"\ncat\n')
    fake.chmod(0o755)
    done = _shim(["claude-code-stop", "--log-dir", "/a b"], f"{tmp_path}:/usr/bin:/bin", "IN")
    assert done.stdout == "hook|claude-code-stop|--log-dir|/a b|IN"


CODEX_EVENTS = {
    "PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact",
    "UserPromptSubmit", "SubagentStop", "Stop", "Interrupt", "SessionStart", "SubagentStart",
    "SessionEnd",
}  # fmt: skip


@pytest.mark.parametrize(
    ("guide", "events", "prefix"),
    [("codex.md", CODEX_EVENTS, "codex-"), ("gemini-cli.md", GEMINI_EVENTS, "gemini-")],
)
def test_guide_hook_snippets_are_valid(guide: str, events: set[str], prefix: str) -> None:
    text = (ROOT / "docs" / "guides" / guide).read_text(encoding="utf-8")
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", text, re.S)]
    hooks = [b["hooks"] for b in blocks if "hooks" in b]
    assert len(hooks) == 1
    handlers = _handlers(hooks[0])
    assert {event for event, _ in handlers} <= events
    commands = _hook_commands()
    for _, handler in handlers:
        match = re.match(r"evidence-judge hook ([a-z-]+) ", str(handler["command"]))
        assert match, handler["command"]
        assert match.group(1) in commands
        assert match.group(1).startswith(prefix)
