"""The task spec is fixed when the turn starts: an agent that weakens or deletes it mid-turn is
still judged against the original, and is sent back for changing it."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evidence_judge.cli import app
from evidence_judge.hooks.state import load_snapshot, resolve_state_dir, session_dir
from tests.unit.hook_fixtures import RUNS, claude_transcript, make_project, render

runner = CliRunner()
SESSION = "0b7e3c1a-1111-4222-8333-944455556666"
SIDE_EFFECT_ANSWER = (RUNS / "side_effect" / "answer.txt").read_text("utf-8")
HONEST_ANSWER = (RUNS / "honest_success" / "answer.txt").read_text("utf-8")


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for key in list(os.environ):
        if key.startswith("EVJ_HOOK_"):
            monkeypatch.delenv(key)


def _turn_start(project: Path, state: Path, command: str = "claude-code-turn-start") -> str:
    data = {"session_id": SESSION, "cwd": str(project), "hook_event_name": "UserPromptSubmit"}
    result = runner.invoke(
        app, ["hook", command, "--state-dir", str(state)], input=json.dumps(data)
    )
    assert result.exit_code == 0, result.output
    return result.stdout


def _stop(tmp_path: Path, project: Path, state: Path, answer: str) -> dict[str, object]:
    transcript = claude_transcript(tmp_path / "session.jsonl")
    data = json.loads(render("claude_code_stop.json", TRANSCRIPT=str(transcript), CWD=str(project)))
    data["last_assistant_message"] = answer
    result = runner.invoke(
        app, ["hook", "claude-code-stop", "--state-dir", str(state)], input=json.dumps(data)
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip())


def _weaken(project: Path) -> None:
    spec = project / ".evidence-judge" / "spec.yaml"
    spec.write_text(spec.read_text("utf-8").split("forbid:")[0], "utf-8")


def test_the_baseline_side_effect_run_fails(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    make_project(project, "side_effect")
    out = _stop(tmp_path, project, tmp_path / "state", SIDE_EFFECT_ANSWER)
    assert out["decision"] == "block"


def test_without_the_snapshot_a_weakened_spec_would_pass(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    make_project(project, "side_effect")
    _weaken(project)
    out = _stop(tmp_path, project, tmp_path / "state", SIDE_EFFECT_ANSWER)
    assert "decision" not in out


def test_a_spec_weakened_mid_turn_is_judged_as_it_was_and_blocks(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    state = tmp_path / "state"
    make_project(project, "side_effect")
    assert _turn_start(project, state) == ""
    _weaken(project)
    out = _stop(tmp_path, project, state, SIDE_EFFECT_ANSWER)
    assert out["decision"] == "block"
    reason = str(out["reason"])
    assert reason.startswith("Evidence Judge: the task spec changed during the turn")
    assert "Evidence Judge: FAIL" in reason
    assert "no_account_changes" in reason


def test_a_spec_change_can_be_a_notice_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "shop"
    state = tmp_path / "state"
    make_project(project, "honest_success", layout="zip")
    _turn_start(project, state)
    _weaken(project)
    monkeypatch.setenv("EVJ_HOOK_BLOCK_ON_SPEC_CHANGE", "false")
    out = _stop(tmp_path, project, state, HONEST_ANSWER)
    assert "decision" not in out
    message = str(out["systemMessage"])
    assert message.startswith("evidence-judge hook: the task spec changed during the turn")
    assert "Evidence Judge: PASS" in message


def test_a_spec_change_notice_keeps_a_failure_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "shop"
    state = tmp_path / "state"
    make_project(project, "side_effect")
    _turn_start(project, state)
    _weaken(project)
    monkeypatch.setenv("EVJ_HOOK_BLOCK_ON_SPEC_CHANGE", "false")
    out = _stop(tmp_path, project, state, SIDE_EFFECT_ANSWER)
    assert out["decision"] == "block"
    assert "the task spec changed during the turn" in str(out["reason"])


def test_a_deleted_spec_blocks(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    state = tmp_path / "state"
    make_project(project, "side_effect")
    _turn_start(project, state)
    (project / ".evidence-judge" / "spec.yaml").unlink()
    out = _stop(tmp_path, project, state, SIDE_EFFECT_ANSWER)
    assert out["decision"] == "block"
    assert "no spec now" in str(out["reason"])


def test_a_spec_created_mid_turn_blocks(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    state = tmp_path / "state"
    make_project(project, "honest_success", layout="zip", spec=False)
    _turn_start(project, state)
    (project / ".evidence-judge").mkdir()
    (project / ".evidence-judge" / "spec.yaml").write_text("task: anything\n")
    out = _stop(tmp_path, project, state, HONEST_ANSWER)
    assert out["decision"] == "block"
    assert "(no spec when it started" in str(out["reason"])


def test_an_unchanged_spec_is_quiet(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    state = tmp_path / "state"
    make_project(project, "honest_success", layout="zip")
    _turn_start(project, state)
    out = _stop(tmp_path, project, state, HONEST_ANSWER)
    assert "decision" not in out
    assert str(out["systemMessage"]).startswith("Evidence Judge: PASS")


def test_the_snapshot_is_private(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    state = tmp_path / "state"
    make_project(project, "side_effect")
    _turn_start(project, state)
    snapshot = load_snapshot(state, SESSION)
    assert snapshot is not None
    assert snapshot.copy is not None
    assert stat.S_IMODE(snapshot.copy.stat().st_mode) == 0o600
    assert stat.S_IMODE(session_dir(state, SESSION).stat().st_mode) == 0o700


def test_a_damaged_snapshot_matches_no_spec(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    state = tmp_path / "state"
    make_project(project, "side_effect")
    _turn_start(project, state)
    snapshot = load_snapshot(state, SESSION)
    assert snapshot is not None
    assert snapshot.copy is not None
    snapshot.copy.write_text("task: weakened\n")
    out = _stop(tmp_path, project, state, SIDE_EFFECT_ANSWER)
    assert out["decision"] == "block"
    assert "a damaged snapshot" in str(out["reason"])


def test_a_spec_outside_the_workspace_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "shop"
    make_project(project, "side_effect", spec=False)
    outside = tmp_path / "private" / "spec.yaml"
    outside.parent.mkdir()
    outside.write_bytes((RUNS / "side_effect" / "spec.yaml").read_bytes())
    monkeypatch.setenv("EVJ_HOOK_SPEC", str(outside))
    out = _stop(tmp_path, project, tmp_path / "state", SIDE_EFFECT_ANSWER)
    assert out["decision"] == "block"
    assert "no_account_changes" in str(out["reason"])


@pytest.mark.parametrize(
    ("command", "stdout"), [("codex-turn-start", ""), ("gemini-turn-start", "{}\n")]
)
def test_codex_and_gemini_turn_start(tmp_path: Path, command: str, stdout: str) -> None:
    project = tmp_path / "shop"
    make_project(project, "side_effect")
    assert _turn_start(project, tmp_path / "state", command) == stdout
    assert load_snapshot(tmp_path / "state", SESSION) is not None


@pytest.mark.parametrize(
    "stdin", ["not json", '{"session_id": "s"}', '{"session_id": "s", "cwd": "relative"}']
)
def test_turn_start_never_fails_the_agent(tmp_path: Path, stdin: str) -> None:
    result = runner.invoke(
        app, ["hook", "claude-code-turn-start", "--state-dir", str(tmp_path)], input=stdin
    )
    assert (result.exit_code, result.stdout) == (0, "")
    assert load_snapshot(tmp_path, "s") is None


def test_state_dir_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert resolve_state_dir(str(tmp_path / "a")) == tmp_path / "a"
    assert resolve_state_dir("${CLAUDE_PLUGIN_DATA}/state").name == "state"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin"))
    assert resolve_state_dir("${X}") == tmp_path / "plugin" / "state"
    monkeypatch.setenv("EVJ_HOOK_STATE_DIR", str(tmp_path / "env"))
    assert resolve_state_dir(None) == tmp_path / "env"
    assert session_dir(tmp_path, "../x").parent == tmp_path
