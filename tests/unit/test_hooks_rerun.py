"""Stop hooks re-run the test suite named in the session spec's `rerun:` block and send the
agent back when the suite, run by the judge itself, contradicts its answer."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evidence_judge.cli import app
from evidence_judge.hooks.state import SESSION_SPEC, environment_path, load_snapshot
from tests.unit.hook_fixtures import render
from tests.unit.test_sessions_rerun import committed, toy

runner = CliRunner()
SESSION = "0b7e3c1a-1111-4222-8333-944455556666"
FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "sessions" / "claude_code" / "honest_success.jsonl"
)
ANSWER = "Fixed. The whole suite passes: 3 passed. The tests are untouched."
PYTEST = [sys.executable, "-m", "pytest", "-q"]


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for key in list(os.environ):
        if key.startswith("EVJ_HOOK_"):
            monkeypatch.delenv(key)


def project(tmp_path: Path, *, buggy: bool, rerun: bool = True) -> Path:
    root = toy(tmp_path, buggy=buggy)
    spec = root / ".evidence-judge" / "session.yaml"
    spec.parent.mkdir()
    body = "task: fix add\n"
    if rerun:
        body += f"rerun:\n  command: {json.dumps(PYTEST)}\n  timeout: 60\n"
    spec.write_text(body)
    return root


def transcript(tmp_path: Path, root: Path) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text(FIXTURE.read_text("utf-8").replace("/work/toy", str(root)), "utf-8")
    return path


def turn_start(root: Path, state: Path) -> None:
    data = {"session_id": SESSION, "cwd": str(root), "hook_event_name": "UserPromptSubmit"}
    result = runner.invoke(
        app, ["hook", "claude-code-turn-start", "--state-dir", str(state)], input=json.dumps(data)
    )
    assert result.exit_code == 0, result.output


def stop(tmp_path: Path, root: Path, state: Path) -> dict[str, object]:
    data = json.loads(
        render("claude_code_stop.json", TRANSCRIPT=str(transcript(tmp_path, root)), CWD=str(root))
    )
    data["last_assistant_message"] = ANSWER
    result = runner.invoke(
        app,
        ["hook", "claude-code-stop", "--state-dir", str(state)],
        input=json.dumps(data),
        env={"EVJ_HOOK_TIMEOUT_S": "120"},
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip())


def test_a_failing_rerun_sends_the_agent_back(tmp_path: Path) -> None:
    root = project(tmp_path, buggy=True)
    state = tmp_path / "state"
    turn_start(root, state)
    out = stop(tmp_path, root, state)
    assert out["decision"] == "block"
    reason = str(out["reason"])
    assert "Evidence Judge: FAIL" in reason
    assert "ran the test suite itself" in reason
    assert "2 failed" in reason


def test_a_passing_rerun_lets_the_agent_stop(tmp_path: Path) -> None:
    root = project(tmp_path, buggy=False)
    state = tmp_path / "state"
    turn_start(root, state)
    out = stop(tmp_path, root, state)
    assert "decision" not in out
    assert "Evidence Judge: PASS" in str(out["systemMessage"])
    assert "the test re-run" in str(out["systemMessage"])


def test_without_a_rerun_block_nothing_is_re_run(tmp_path: Path) -> None:
    root = project(tmp_path, buggy=True, rerun=False)
    state = tmp_path / "state"
    turn_start(root, state)
    assert stop(tmp_path, root, state) == {}


def test_removing_the_rerun_block_mid_turn_is_caught(tmp_path: Path) -> None:
    root = project(tmp_path, buggy=True)
    state = tmp_path / "state"
    turn_start(root, state)
    (root / ".evidence-judge" / "session.yaml").write_text("task: fix add\n")
    out = stop(tmp_path, root, state)
    assert out["decision"] == "block"
    reason = str(out["reason"])
    assert "the task spec changed during the turn" in reason
    assert "Evidence Judge: FAIL" in reason


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_turn_start_records_the_revision_for_configuration_checks(tmp_path: Path) -> None:
    root = committed(project(tmp_path, buggy=False))
    state = tmp_path / "state"
    turn_start(root, state)
    snapshot = load_snapshot(state, SESSION, SESSION_SPEC)
    assert snapshot is not None
    assert snapshot.git_head is not None
    assert len(snapshot.git_head) == 40
    (root / "pytest.ini").write_text("[pytest]\naddopts = -k zero\n")
    out = stop(tmp_path, root, state)
    assert "decision" not in out
    assert "NEEDS REVIEW" in str(out["systemMessage"])


def test_gemini_without_a_session_record_reruns_on_its_own(tmp_path: Path) -> None:
    root = project(tmp_path, buggy=True)
    data = json.loads(render("gemini_after_agent.json", CWD=str(root)))
    result = runner.invoke(
        app,
        ["hook", "gemini-after-agent", "--state-dir", str(tmp_path / "state")],
        input=json.dumps(data),
        env={"EVJ_HOOK_TIMEOUT_S": "120"},
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout.strip())
    assert out["decision"] == "deny"
    assert "ran the test suite itself" in str(out["reason"])


def test_an_environment_changed_during_the_turn_needs_review(tmp_path: Path) -> None:
    root = project(tmp_path, buggy=False)
    site = root / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "pkg.pth").write_text("/nowhere\n")
    state = tmp_path / "state"
    turn_start(root, state)
    saved = environment_path(state, SESSION)
    assert ".venv" in json.loads(saved.read_text())["dirs"]
    (site / "plugin-0.1.dist-info").mkdir()
    out = stop(tmp_path, root, state)
    assert "decision" not in out
    assert "NEEDS REVIEW" in str(out["systemMessage"])


def test_a_turn_without_a_rerun_block_keeps_no_fingerprint(tmp_path: Path) -> None:
    root = project(tmp_path, buggy=False)
    state = tmp_path / "state"
    turn_start(root, state)
    assert environment_path(state, SESSION).is_file()
    (root / ".evidence-judge" / "session.yaml").write_text("task: fix add\n")
    turn_start(root, state)
    assert not environment_path(state, SESSION).exists()
