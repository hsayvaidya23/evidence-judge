"""The evidence recorder: log format version 1, one redacted, bounded, hash-chained JSON line
per tool call, prompt and stop."""

from __future__ import annotations

import hashlib
import json
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evidence_judge.cli import app
from evidence_judge.hooks import record as record_module
from evidence_judge.hooks.record import (
    DEFAULT_LOG_DIR,
    EXCERPT_CHARS,
    append,
    build_record,
    log_path,
    resolve_log_dir,
    stop_record,
)
from evidence_judge.redact import Redactor
from tests.unit.hook_fixtures import payload, render

runner = CliRunner()
REDACTOR = Redactor(b"k" * 32)
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
REQUIRED = {
    "v",
    "agent",
    "ts",
    "session_id",
    "tool",
    "input",
    "exit_code",
    "stdout_sha256",
    "stdout_excerpt",
    "stderr_excerpt",
    "cwd",
}


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _entry(agent: str, data: dict[str, object]) -> dict[str, object]:
    entry = build_record(agent, data, REDACTOR, NOW)  # type: ignore[arg-type]
    assert entry is not None
    return entry


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_claude_code_bash_success_is_exit_zero_and_redacted() -> None:
    data = payload("claude_code_post_tool_use_bash.json")
    entry = _entry("claude-code", data)
    assert set(entry) >= REQUIRED
    assert (entry["v"], entry["agent"], entry["event"]) == (1, "claude-code", "tool")
    assert entry["ts"] == "2026-10-15T09:00:00.000Z"
    assert (entry["tool"], entry["status"], entry["exit_code"]) == ("Bash", "ok", 0)
    assert entry["duration_ms"] == 412
    assert entry["tool_use_id"] == "toolu_bash_01"
    blob = json.dumps(entry)
    assert "sk-ant-api03" not in blob
    assert "jane.doe@example.com" not in blob
    assert "[REDACTED:" in blob
    stdout = data["tool_response"]["stdout"]  # type: ignore[index]
    assert entry["stdout_sha256"] == _sha(stdout)
    assert entry["stdout_bytes"] == len(stdout.encode())
    assert entry["output_truncated"] is True
    assert entry["stderr_excerpt"] == ""
    assert "agent_id" not in entry
    assert "result" not in entry


def test_an_untouched_excerpt_is_complete() -> None:
    data = payload("claude_code_post_tool_use_bash.json")
    data["tool_response"] = {"stdout": "3 passed in 0.02s", "stderr": "", "interrupted": False}
    entry = _entry("claude-code", data)
    assert entry["output_truncated"] is False
    assert entry["stdout_sha256"] == _sha(str(entry["stdout_excerpt"]))


def test_a_return_code_interpretation_means_no_exit_code() -> None:
    data = payload("claude_code_post_tool_use_bash.json")
    data["tool_response"] = {
        "stdout": "",
        "stderr": "",
        "interrupted": False,
        "returnCodeInterpretation": "No matches found",
    }
    assert _entry("claude-code", data)["exit_code"] is None


def test_claude_code_failure_reads_the_exit_code_line() -> None:
    entry = _entry("claude-code", payload("claude_code_post_tool_use_failure_bash.json"))
    assert (entry["status"], entry["exit_code"]) == ("error", 1)
    assert str(entry["stdout_excerpt"]).startswith("Exit code 1\nFAIL src/cart.test.ts")


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"error": "Permission to use Bash has been denied."}, "error", None),
        ({"is_interrupt": True}, "interrupted", None),
        ({"tool_name": "PowerShell"}, "error", None),
    ],
)
def test_claude_code_failures_never_guess(
    changes: dict[str, object], status: str, code: int | None
) -> None:
    data = payload("claude_code_post_tool_use_failure_bash.json") | changes
    entry = _entry("claude-code", data)
    assert (entry["status"], entry["exit_code"]) == (status, code)


def test_claude_code_permission_denied_did_not_run() -> None:
    data = {
        "hook_event_name": "PermissionDenied",
        "session_id": "s",
        "cwd": "/w",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"},
        "tool_use_id": "toolu_9",
        "reason": "denied by the auto-mode classifier",
    }
    entry = _entry("claude-code", data)
    assert (entry["status"], entry["exit_code"], entry["stdout_sha256"]) == ("denied", None, None)
    assert entry["stdout_excerpt"] == ""
    assert entry["stderr_excerpt"] == "denied by the auto-mode classifier"


def test_claude_code_interrupted_bash_has_no_exit_code() -> None:
    data = payload("claude_code_post_tool_use_bash.json")
    data["tool_response"] = {"stdout": "", "stderr": "", "interrupted": True}
    entry = _entry("claude-code", data)
    assert (entry["status"], entry["exit_code"]) == ("interrupted", None)


def test_claude_code_edits_keep_the_patch_not_the_file() -> None:
    patch = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-a", "+b"]}]
    data = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "session_id": "s",
        "agent_id": "agent-7",
        "cwd": "/w",
        "tool_input": {"file_path": "/w/test_a.py", "content": "x" * 5000},
        "tool_response": {
            "type": "update",
            "filePath": "/w/test_a.py",
            "content": "SECRET FILE BODY",
            "originalFile": "ORIGINAL BODY",
            "structuredPatch": patch,
            "userModified": False,
        },
    }
    entry = _entry("claude-code", data)
    assert entry["result"] == {"structuredPatch": patch, "type": "update", "userModified": False}
    assert (entry["exit_code"], entry["stdout_sha256"], entry["stdout_excerpt"]) == (None, None, "")
    assert entry["agent_id"] == "agent-7"
    assert "BODY" not in json.dumps(entry)
    content = entry["input"]["content"]  # type: ignore[index]
    assert len(content) < 1100
    assert "characters omitted" in content


def test_claude_code_other_tools_keep_their_response_text() -> None:
    data = {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__playwright__browser_navigate",
        "session_id": "s",
        "cwd": "/w",
        "tool_input": "not an object",
        "tool_response": [{"type": "text", "text": "Navigated"}],
    }
    entry = _entry("claude-code", data)
    assert entry["input"] == {}
    assert json.loads(str(entry["stdout_excerpt"])) == [{"type": "text", "text": "Navigated"}]


def test_codex_exit_code_from_the_payload_or_its_exit_line() -> None:
    entry = _entry("codex", payload("codex_post_tool_use.json"))
    assert (entry["exit_code"], entry["status"], entry["agent"]) == (2, "error", "codex")
    assert str(entry["stdout_excerpt"]).startswith("Process exited with code 2")
    data = payload("codex_post_tool_use.json")
    data["tool_response"] = "Chunk ID: 1\nProcess exited with code 0\nOutput:\n5 passed"
    assert (_entry("codex", data)["exit_code"], _entry("codex", data)["status"]) == (0, "ok")
    data["tool_response"] = "Process exited with code 1\nOutput:\nProcess exited with code 0"
    assert _entry("codex", data)["exit_code"] == 1
    data["tool_response"] = "output mentions: Process exited with code 0 inline"
    assert "status" not in _entry("codex", data)
    data["tool_response"] = {"stdout": "ok", "stderr": "warn", "exit_code": 0}
    entry = _entry("codex", data)
    assert (entry["exit_code"], entry["status"], entry["stderr_excerpt"]) == (0, "ok", "warn")
    data["tool_response"] = {"output": "boom", "exitCode": 7}
    assert (_entry("codex", data)["exit_code"], _entry("codex", data)["status"]) == (7, "error")


def test_codex_patches_keep_their_changes() -> None:
    changes = {"/w/a.py": {"type": "update", "unified_diff": "-a\n+b"}}
    data = payload("codex_post_tool_use.json")
    data["tool_name"] = "apply_patch"
    data["tool_input"] = {"changes": changes}
    data["tool_response"] = "Success. Updated the following files:\nM a.py"
    assert _entry("codex", data)["result"] == {"changes": changes}


def test_gemini_shell_exit_code_and_errors() -> None:
    entry = _entry("gemini", payload("gemini_after_tool_shell.json"))
    assert (entry["exit_code"], entry["status"], entry["agent"]) == (1, "error", "gemini")
    data = payload("gemini_after_tool_shell.json")
    data["tool_name"] = "read_file"
    data["tool_response"] = {"returnDisplay": "read", "error": {"message": "ENOENT"}}
    entry = _entry("gemini", data)
    assert (entry["exit_code"], entry["status"], entry["stderr_excerpt"]) == (
        None,
        "error",
        "ENOENT",
    )
    assert entry["stdout_excerpt"] == "read"
    data["tool_response"] = {"llmContent": "Exit Code: 0"}
    data["tool_name"] = "run_shell_command"
    assert (_entry("gemini", data)["exit_code"], _entry("gemini", data)["status"]) == (0, "ok")


@pytest.mark.parametrize(
    ("agent", "event", "prompt_key"),
    [
        ("claude-code", "UserPromptSubmit", "prompt"),
        ("codex", "UserPromptSubmit", "prompt"),
        ("gemini", "BeforeAgent", "prompt"),
    ],
)
def test_prompts_are_logged(agent: str, event: str, prompt_key: str) -> None:
    data: dict[str, object] = {
        "hook_event_name": event,
        "session_id": "s",
        prompt_key: "mail a@b.example now",
    }
    entry = _entry(agent, data)
    assert entry["event"] == "prompt"
    assert "a@b.example" not in str(entry["text"])


def test_stop_records_carry_the_redacted_answer() -> None:
    data = {"session_id": "s", "agent_id": "agent-1"}
    entry = stop_record("codex", data, "sent to jane.doe@example.com", REDACTOR, NOW)
    assert entry["event"] == "stop"
    assert entry["agent_id"] == "agent-1"
    assert "jane.doe" not in str(entry["last_assistant_message"])
    assert stop_record("gemini", {}, None, REDACTOR, NOW)["last_assistant_message"] == ""


@pytest.mark.parametrize(
    ("agent", "event"),
    [("claude-code", "Stop"), ("codex", "PostToolUseFailure"), ("gemini", "PostToolUse")],
)
def test_other_events_are_not_logged(agent: str, event: str) -> None:
    data = {"hook_event_name": event, "tool_name": "Bash"}
    assert build_record(agent, data, REDACTOR, NOW) is None  # type: ignore[arg-type]


def test_long_outputs_and_deep_inputs_are_bounded() -> None:
    nested: object = "leaf"
    for _ in range(10):
        nested = {"k": nested}
    data = payload("claude_code_post_tool_use_bash.json")
    data["tool_input"] = {"deep": nested, "many": list(range(200)), "obj": object()}
    data["tool_response"] = {"stdout": "a" * 10_000 + "TAIL", "stderr": None}
    entry = _entry("claude-code", data)
    excerpt = str(entry["stdout_excerpt"])
    assert len(excerpt) < EXCERPT_CHARS + 100
    assert excerpt.endswith("TAIL")
    assert entry["output_truncated"] is True
    assert entry["stdout_bytes"] == 10_004
    assert entry["stderr_excerpt"] == ""
    assert len(entry["input"]["many"]) == 50  # type: ignore[index]
    assert "…" in json.dumps(entry["input"], ensure_ascii=False)


def test_the_log_is_a_hash_chain(tmp_path: Path) -> None:
    for i in range(3):
        append(tmp_path, {"v": 1, "session_id": "s", "n": i})
    raw = (tmp_path / "s.jsonl").read_bytes().splitlines()
    lines = [json.loads(line) for line in raw]
    assert [line["seq"] for line in lines] == [0, 1, 2]
    assert lines[0]["prev"] is None
    assert lines[1]["prev"] == hashlib.sha256(raw[0]).hexdigest()
    assert lines[2]["prev"] == hashlib.sha256(raw[1]).hexdigest()


def test_the_chain_restarts_after_a_corrupt_line(tmp_path: Path) -> None:
    (tmp_path / "s.jsonl").write_bytes(b"garbage\n")
    append(tmp_path, {"session_id": "s"})
    raw = (tmp_path / "s.jsonl").read_bytes().splitlines()
    last = json.loads(raw[-1])
    assert (last["seq"], last["prev"]) == (0, hashlib.sha256(b"garbage").hexdigest())


def test_concurrent_appends_keep_one_chain(tmp_path: Path) -> None:
    threads = [
        threading.Thread(target=append, args=(tmp_path, {"session_id": "s", "n": i}))
        for i in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    raw = (tmp_path / "s.jsonl").read_bytes().splitlines()
    lines = [json.loads(line) for line in raw]
    assert [line["seq"] for line in lines] == list(range(20))
    for before, line in zip(raw, lines[1:], strict=False):
        assert line["prev"] == hashlib.sha256(before).hexdigest()


@pytest.mark.parametrize(
    ("session", "safe"),
    [("abc-123_X.y", True), ("../../etc/passwd", False), ("..", False), ("", False)],
)
def test_log_path_cannot_escape(tmp_path: Path, session: str, safe: bool) -> None:
    path = log_path(tmp_path, session)
    assert path.parent == tmp_path
    assert (path.name == f"{session}.jsonl") is safe


def test_resolve_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EVJ_HOOK_LOG_DIR", raising=False)
    assert resolve_log_dir(str(tmp_path)) == tmp_path
    assert resolve_log_dir("${CLAUDE_PLUGIN_DATA}/evidence") == DEFAULT_LOG_DIR
    assert resolve_log_dir("relative") == DEFAULT_LOG_DIR
    monkeypatch.setenv("EVJ_HOOK_LOG_DIR", str(tmp_path / "env"))
    assert resolve_log_dir("/sessions") == Path("/sessions")
    assert resolve_log_dir(None) == tmp_path / "env"


@pytest.mark.parametrize(
    ("command", "fixture", "stdout"),
    [
        ("claude-code-record", "claude_code_post_tool_use_bash.json", ""),
        ("claude-code-record", "claude_code_post_tool_use_failure_bash.json", ""),
        ("codex-record", "codex_post_tool_use.json", ""),
        ("gemini-record", "gemini_after_tool_shell.json", "{}\n"),
    ],
)
def test_record_commands_append_private_lines(
    tmp_path: Path, command: str, fixture: str, stdout: str
) -> None:
    logs = tmp_path / "logs"
    stdin = render(fixture)
    for _ in range(2):
        result = runner.invoke(app, ["hook", command, "--log-dir", str(logs)], input=stdin)
        assert result.exit_code == 0
        assert result.stdout == stdout
    files = list(logs.iterdir())
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    lines = _lines(files[0])
    assert [line["seq"] for line in lines] == [0, 1]
    assert all(set(line) >= REQUIRED for line in lines)


def test_record_fails_open(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["hook", "claude-code-record", "--log-dir", str(tmp_path)], input="{"
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "could not record the tool call (JSONDecodeError)" in result.stderr
    blocker = tmp_path / "file"
    blocker.write_text("")
    stdin = render("claude_code_post_tool_use_bash.json")
    result = runner.invoke(
        app, ["hook", "claude-code-record", "--log-dir", str(blocker)], input=stdin
    )
    assert result.exit_code == 0
    assert "could not record the tool call" in result.stderr


def test_record_skips_other_events(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["hook", "codex-record", "--log-dir", str(tmp_path / "l")],
        input='{"hook_event_name": "Stop"}',
    )
    assert result.exit_code == 0
    assert not (tmp_path / "l").exists()


def test_the_redactor_survives_bad_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVJ_MAX_BODY_CHARS", "lots")
    assert isinstance(record_module.redactor(), Redactor)
