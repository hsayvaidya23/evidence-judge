"""Which traces a stop hook judges: every trace of the turn, only the ones browser_stop_tracing
reported when it reported any, and never a missing, junk or older trace in place of evidence."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evidence_judge.cli import app
from tests.unit.hook_fixtures import (
    RUNS,
    claude_transcript,
    make_project,
    raw_trace,
    render,
    stop_tracing_transcript,
    trace_zip,
)

runner = CliRunner()
HONEST_ANSWER = (RUNS / "honest_success" / "answer.txt").read_text("utf-8")


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for key in list(os.environ):
        if key.startswith("EVJ_HOOK_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("EVJ_HOOK_STATE_DIR", str(tmp_path / "hook-state"))


def _stop(project: Path, transcript: Path, **overrides: object) -> dict[str, object]:
    data = json.loads(render("claude_code_stop.json", TRANSCRIPT=str(transcript), CWD=str(project)))
    data.update(overrides)
    result = runner.invoke(app, ["hook", "claude-code-stop"], input=json.dumps(data))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip())


def _transcript(tmp_path: Path) -> Path:
    return claude_transcript(tmp_path / "session.jsonl")


def _junk(project: Path) -> Path:
    junk = project / ".playwright-mcp" / "traces" / "trace-1.trace"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_text("not a trace\n")
    return junk


def test_a_junk_trace_blocks_instead_of_failing_open(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    _junk(project)
    out = _stop(project, _transcript(tmp_path))
    assert out["decision"] == "block"
    assert ".playwright-mcp/traces/trace-1.trace: the judge exited 3" in str(out["reason"])


def test_a_junk_trace_next_to_an_honest_one_still_blocks(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    make_project(project, "honest_success", layout="zip")
    _junk(project)
    out = _stop(project, _transcript(tmp_path), last_assistant_message=HONEST_ANSWER)
    assert out["decision"] == "block"
    assert "trace-1.trace" in str(out["reason"])


def test_junk_without_playwright_is_only_a_notice(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    _junk(project)
    data = json.loads(render("claude_code_stop.json", TRANSCRIPT="", CWD=str(project)))
    result = runner.invoke(app, ["hook", "claude-code-stop"], input=json.dumps(data))
    out = json.loads(result.stdout.strip())
    assert "decision" not in out
    assert "the answer was not checked" in str(out["systemMessage"])


def test_a_planted_old_trace_does_not_hide_the_real_one(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    make_project(project)
    planted = trace_zip(
        "honest_success", project / "test-results" / "x" / "trace.zip", at=time.time() - 86400
    )
    later = time.time() + 5
    os.utime(planted, (later, later))
    out = _stop(project, _transcript(tmp_path))
    assert out["decision"] == "block"
    assert str(out["reason"]).startswith("Evidence Judge: FAIL")
    assert "order #1042" in str(out["reason"])


def test_a_planted_old_trace_alone_blocks(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    trace_zip(
        "honest_success", project / "test-results" / "x" / "trace.zip", at=time.time() - 86400
    )
    out = _stop(project, _transcript(tmp_path), last_assistant_message=HONEST_ANSWER)
    assert out["decision"] == "block"
    assert "test-results/x/trace.zip: its events were recorded before this turn started" in str(
        out["reason"]
    )


def test_every_trace_of_the_turn_is_judged(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    make_project(project, "honest_success", layout="zip")
    raw_trace("forged_success", project / ".playwright-mcp" / "traces")
    out = _stop(project, _transcript(tmp_path), last_assistant_message=HONEST_ANSWER)
    assert out["decision"] == "block"
    assert "Evidence Judge: FAIL" in str(out["reason"])
    assert ".playwright-mcp/traces/trace-1792007306000.trace" in str(out["reason"])


def test_a_reported_trace_that_is_missing_blocks(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    make_project(project, "honest_success", layout="zip")
    transcript = stop_tracing_transcript(tmp_path / "s.jsonl", "test-results/gone.trace")
    out = _stop(project, transcript, last_assistant_message=HONEST_ANSWER)
    assert out["decision"] == "block"
    assert "test-results/gone.trace: browser_stop_tracing reported it, but it is missing" in str(
        out["reason"]
    )


def test_reported_traces_are_the_only_ones_that_count(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    make_project(project)
    trace_zip("honest_success", project / "test-results" / "x" / "trace.zip")
    transcript = stop_tracing_transcript(
        tmp_path / "s.jsonl", ".playwright-mcp/traces/trace-1792007306000.trace"
    )
    out = _stop(project, transcript)
    assert out["decision"] == "block"
    assert str(out["reason"]).startswith("Evidence Judge: FAIL")
    assert "test-results" not in str(out["reason"])


def test_an_honest_reported_trace_passes(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    make_project(project, "honest_success")
    trace_zip("forged_success", project / "test-results" / "x" / "trace.zip")
    transcript = stop_tracing_transcript(
        tmp_path / "s.jsonl", ".playwright-mcp/traces/trace-1792007306000.trace"
    )
    out = _stop(project, transcript, last_assistant_message=HONEST_ANSWER)
    assert "decision" not in out
    assert str(out["systemMessage"]).startswith("Evidence Judge: PASS")
