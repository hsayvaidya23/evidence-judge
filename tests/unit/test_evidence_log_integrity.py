"""Evidence-log forgery: an HMAC chain under the installation's key plus a head anchor kept
apart from the log. Re-chaining, truncating or reordering a log never reads as untouched."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from evidence_judge.hooks.record import append
from evidence_judge.hooks.state import LOG_KEY_FILE, key_id, line_mac, log_head_path
from evidence_judge.sessions.ingest.evidence_log import load_evidence_log
from evidence_judge.sessions.models import SessionRun

SESSION = "s-1"


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVJ_HOOK_STATE_DIR", str(tmp_path / "state"))


def _tool(command: str, exit_code: int, stdout: str) -> dict[str, object]:
    return {
        "v": 1,
        "agent": "claude-code",
        "ts": "2026-10-15T09:00:00.000Z",
        "session_id": SESSION,
        "event": "tool",
        "tool": "Bash",
        "input": {"command": command},
        "exit_code": exit_code,
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stdout_excerpt": stdout,
        "stderr_excerpt": "",
        "cwd": "/work",
        "output_truncated": False,
    }


def _log(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    append(tmp_path / "logs", _tool("pytest -q", 1, "1 failed"), state)
    append(tmp_path / "logs", _tool("git status", 0, "clean"), state)
    return append(tmp_path / "logs", _tool("pytest -q", 0, "5 passed"), state)


def integrity(run: SessionRun) -> tuple[str, str]:
    note = next(n for n in run.notes if n.startswith("log integrity: "))
    level, reason = note.removeprefix("log integrity: ").split(": ", 1)
    return level, reason


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines()


def _write(path: Path, lines: list[bytes]) -> None:
    path.write_bytes(b"".join(line + b"\n" for line in lines))


def test_an_untouched_keyed_log_verifies(tmp_path: Path) -> None:
    run = load_evidence_log(_log(tmp_path))
    assert run.complete
    assert integrity(run)[0] == "full"
    first = json.loads(_lines(_log(tmp_path))[0])
    assert (first["chain"], first["seq"], first["prev"]) == ("hmac-sha256", 0, None)


def test_the_key_and_anchor_are_private(tmp_path: Path) -> None:
    _log(tmp_path)
    key = tmp_path / "state" / LOG_KEY_FILE
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    anchor = log_head_path(tmp_path / "state", SESSION)
    assert json.loads(anchor.read_text())["count"] == 3


def test_rechaining_with_plain_sha256_is_partial(tmp_path: Path) -> None:
    path = _log(tmp_path)
    forged: list[bytes] = []
    for seq, line in enumerate(_lines(path)):
        record = json.loads(line)
        for key in ("chain", "key_id"):
            record.pop(key)
        if seq == 0:
            record["exit_code"], record["stdout_excerpt"] = 0, "all passed"
        record["prev"] = hashlib.sha256(forged[-1]).hexdigest() if forged else None
        forged.append(json.dumps(record).encode())
    _write(path, forged)
    run = load_evidence_log(path)
    level, reason = integrity(run)
    assert level == "partial"
    assert "plain SHA-256" in reason


def test_rechaining_with_another_key_breaks_the_chain(tmp_path: Path) -> None:
    path = _log(tmp_path)
    attacker = b"k" * 32
    forged: list[bytes] = []
    for line in _lines(path):
        record = json.loads(line)
        record["key_id"] = key_id(attacker)
        record["prev"] = line_mac(attacker, forged[-1]) if forged else None
        forged.append(json.dumps(record).encode())
    _write(path, forged)
    run = load_evidence_log(path)
    assert not run.complete
    assert any("hash chain broken" in note for note in run.notes)
    assert integrity(run)[0] == "partial"


def test_truncation_shows_against_the_anchor(tmp_path: Path) -> None:
    path = _log(tmp_path)
    _write(path, _lines(path)[:-1])
    run = load_evidence_log(path)
    assert not run.complete
    assert any("does not end where its head anchor says" in note for note in run.notes)
    assert integrity(run)[0] == "partial"


def test_reordering_breaks_the_chain(tmp_path: Path) -> None:
    path = _log(tmp_path)
    lines = _lines(path)
    _write(path, [lines[0], lines[2], lines[1]])
    run = load_evidence_log(path)
    assert not run.complete
    assert integrity(run)[0] == "partial"


def test_without_the_anchor_truncation_could_hide(tmp_path: Path) -> None:
    path = _log(tmp_path)
    log_head_path(tmp_path / "state", SESSION).unlink()
    level, reason = integrity(load_evidence_log(path))
    assert level == "partial"
    assert "no head anchor" in reason


def test_without_the_key_the_chain_cannot_be_checked(tmp_path: Path) -> None:
    path = _log(tmp_path)
    level, reason = integrity(load_evidence_log(path, key_file=tmp_path / "missing"))
    assert level == "partial"
    assert "log key was not found" in reason


def test_an_explicit_key_and_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _log(tmp_path)
    monkeypatch.setenv("EVJ_HOOK_STATE_DIR", str(tmp_path / "elsewhere"))
    assert integrity(load_evidence_log(path))[0] == "partial"
    run = load_evidence_log(
        path,
        key_file=tmp_path / "state" / LOG_KEY_FILE,
        anchor_file=log_head_path(tmp_path / "state", SESSION),
    )
    assert integrity(run)[0] == "full"


def test_an_unkeyed_log_is_partial(tmp_path: Path) -> None:
    path = append(tmp_path / "logs", _tool("pytest -q", 0, "5 passed"))
    level, reason = integrity(load_evidence_log(path))
    assert level == "partial"
    assert "plain SHA-256" in reason


def test_a_damaged_key_file_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / LOG_KEY_FILE).write_text("not hex")
    with pytest.raises(ValueError, match="not a usable evidence-log key"):
        append(tmp_path / "logs", _tool("ls", 0, ""), state)


def test_the_record_and_stop_hooks_write_a_verifiable_log(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from evidence_judge.cli import app
    from tests.unit.hook_fixtures import render

    runner = CliRunner()
    dirs = ["--log-dir", str(tmp_path / "logs"), "--state-dir", str(tmp_path / "state")]
    stdin = render("claude_code_post_tool_use_bash.json")
    assert runner.invoke(app, ["hook", "claude-code-record", *dirs], input=stdin).exit_code == 0
    stop = json.loads(render("claude_code_stop.json", TRANSCRIPT="", CWD=str(tmp_path)))
    result = runner.invoke(app, ["hook", "claude-code-stop", *dirs], input=json.dumps(stop))
    assert result.exit_code == 0
    (path,) = (tmp_path / "logs").iterdir()
    run = load_evidence_log(path)
    assert run.complete
    assert run.final_answer
    assert integrity(run)[0] == "full"
