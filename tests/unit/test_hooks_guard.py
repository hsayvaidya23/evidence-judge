from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from evidence_judge.hooks.cli import hook_app
from evidence_judge.hooks.guard import guard_reason

runner = CliRunner()
PATCH = "*** Begin Patch\n*** Update File: .codex/config.toml\n@@\n-a\n+b\n*** End Patch\n"


@pytest.mark.parametrize(
    ("tool", "tool_input", "fragment"),
    [
        (
            "Write",
            {"file_path": "/w/.claude/settings.local.json", "content": '{"disableAllHooks": true}'},
            "it sets disableAllHooks",
        ),
        ("Edit", {"file_path": "/home/u/.claude/settings.json", "new_string": "x"}, "settings"),
        ("MultiEdit", {"file_path": ".claude/hooks/stop.sh", "edits": []}, ".claude/hooks/stop.sh"),
        ("NotebookEdit", {"notebook_path": "/w/.mcp.json"}, ".mcp.json"),
        ("Write", {"file_path": "/w/.evidence-judge/spec.yaml"}, "evidence is kept"),
        ("Bash", {"command": "rm -f ~/.local/bin/evidence-judge"}, "evidence-judge install"),
        ("Bash", {"command": "uv tool uninstall evidence-judge"}, "evidence-judge install"),
        ("Bash", {"command": "echo '{}' > .claude/settings.local.json"}, "agent configuration"),
        ("Bash", {"command": "rm -rf ~/.evidence-judge/sessions"}, "evidence is kept"),
        ("shell", {"command": ["bash", "-lc", "rm ~/.codex/hooks.json"]}, ".codex/hooks.json"),
        ("apply_patch", {"command": PATCH}, ".codex/config.toml"),
        ("run_shell_command", {"command": "cp x ~/.gemini/settings.json"}, ".gemini"),
        ("write_file", {"file_path": "/w/.gemini/settings.json", "content": "{}"}, ".gemini"),
    ],
)
def test_denies_writes_that_switch_off_the_hooks(
    tool: str, tool_input: dict[str, object], fragment: str
) -> None:
    why = guard_reason(tool, tool_input, "/w")
    assert why is not None and fragment in why, why


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Write", {"file_path": "/w/calc.py", "content": "x = 1\n"}),
        ("Edit", {"file_path": "/w/docs/hooks.md", "new_string": "disableAllHooks"}),
        ("Bash", {"command": "pytest -q"}),
        ("Bash", {"command": "cat ~/.claude/settings.json"}),
        ("Bash", {"command": "cat ~/.claude/projects/x/s.jsonl"}),
        ("Bash", {"command": "evidence-judge session judge --claude-code s.jsonl"}),
        ("apply_patch", {"command": "*** Begin Patch\n*** Add File: calc.py\n+x\n*** End Patch"}),
        ("Read", {"file_path": "/w/.claude/settings.json"}),
        ("Bash", {}),
    ],
)
def test_allows_everything_else(tool: str, tool_input: dict[str, object]) -> None:
    assert guard_reason(tool, tool_input, "/w") is None


def _call(command: str, payload: object, **env: str) -> str:
    result = runner.invoke(hook_app, [command], input=json.dumps(payload), env=env)
    assert result.exit_code == 0
    return result.stdout


SETTINGS = {
    "tool_name": "Write",
    "tool_input": {"file_path": "/w/.claude/settings.local.json", "content": "{}"},
    "cwd": "/w",
}


@pytest.mark.parametrize("command", ["claude-code-guard", "codex-guard"])
def test_pre_tool_use_deny_output(command: str) -> None:
    out = json.loads(_call(command, SETTINGS))["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("Evidence Judge blocked this call (")
    allowed = {**SETTINGS, "tool_input": {"file_path": "/w/calc.py"}}
    assert _call(command, allowed) == ""


def test_gemini_before_tool_output() -> None:
    gemini = {**SETTINGS, "tool_name": "write_file"}
    assert json.loads(_call("gemini-guard", gemini))["decision"] == "deny"
    assert json.loads(_call("gemini-guard", {**gemini, "tool_input": {}})) == {}


def test_guard_fails_open() -> None:
    assert _call("claude-code-guard", SETTINGS, EVJ_HOOK_GUARD="false") == ""
    assert _call("claude-code-guard", SETTINGS, EVJ_HOOK_ENABLED="false") == ""
    assert _call("claude-code-guard", SETTINGS, EVJ_HOOK_TIMEOUT_S="nope") == ""
    assert _call("claude-code-guard", ["not", "an", "object"]) == ""
    assert _call("claude-code-guard", {"tool_name": "Write", "tool_input": "x"}) == ""
