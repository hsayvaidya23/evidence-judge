from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.sessions.checks import SessionIndex, check, place
from evidence_judge.sessions.ingest import load_codex
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.models import CallStatus, SessionRun
from evidence_judge.sessions.normalize import ToolCall, build_events
from evidence_judge.sessions.predicates import EditPredicate, SessionSpec
from tests.unit.sessions_helpers import GREEN, bash, session

APP = EditPredicate(path="src/app.py")


def _patch(path: str) -> ToolCall:
    return ToolCall(
        name="apply_patch",
        input={},
        status=CallStatus.OK,
        edits=[(path, "modify", ["a"], ["b"])],
    )


def _rootless(*calls: ToolCall) -> SessionRun:
    events, _ = build_events(list(calls), None)
    return session().model_copy(update={"cwd": None, "events": events})


def _status(predicate: EditPredicate, run: SessionRun) -> Support:
    return check(predicate, SessionIndex(run)).status


def _stream(tmp_path: Path, *items: dict[str, Any]) -> Path:
    records = [
        {"type": "thread.started", "thread_id": "t1"},
        *({"type": "item.completed", "item": {"id": f"i{n}", **item}} for n, item in
          enumerate(items)),
        {"type": "turn.completed"},
    ]  # fmt: skip
    path = tmp_path / "s.exec.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _command(command: str, output: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "type": "command_execution",
        "command": f"/bin/zsh -lc '{command}'",
        "aggregated_output": output,
        "exit_code": 0,
        "status": "completed",
        **extra,
    }


def _change(path: str) -> dict[str, Any]:
    return {"type": "file_change", "changes": [{"path": path, "kind": "update"}], "status": "ok"}


@pytest.mark.parametrize("forged", ["/tmp/other/src/app.py", "/home/u/other/src/app.py"])
def test_a_same_named_file_outside_the_project_proves_no_edit(forged: str) -> None:
    run = _rootless(_patch("/work/toy/calc.py"), _patch("/work/toy/README.md"), _patch(forged))
    assert _status(APP, run) is not Support.SUPPORTED
    unrelated = Support.SUPPORTED if forged.startswith("/tmp/") else Support.NO_EVIDENCE
    assert _status(EditPredicate(path="calc.py"), run) is unrelated


def test_two_same_named_files_in_unrelated_trees_prove_no_edit() -> None:
    run = _rootless(_patch("/work/toy/src/app.py"), _patch("/home/u/other/src/app.py"))
    assert _status(APP, run) is Support.NO_EVIDENCE
    assert _status(EditPredicate(path="app.py"), run) is Support.NO_EVIDENCE
    assert _status(EditPredicate(path="src/app.py", touched=False), run) is Support.CONTRADICTED


def test_a_nested_copy_is_not_the_project_file() -> None:
    run = _rootless(_patch("/work/toy/calc.py"), _patch("/work/toy/vendor/x/src/app.py"))
    assert _status(APP, run) is Support.NO_EVIDENCE
    honest = _rootless(_patch("/work/toy/calc.py"), _patch("/work/toy/src/app.py"))
    assert _status(APP, honest) is Support.SUPPORTED
    nested = _rootless(_patch("/work/toy/src/app.py"), _patch("/work/toy/src/util.py"))
    assert _status(APP, nested) is Support.SUPPORTED
    shell = _rootless(_patch("/work/toy/calc.py"), bash("touch /home/u/other/src/app.py"))
    assert _status(APP, shell) is Support.NO_EVIDENCE


def test_a_lone_absolute_edit_keeps_its_suffix_match() -> None:
    assert _status(APP, _rootless(_patch("/work/toy/src/app.py"))) is Support.SUPPORTED


def test_the_spec_names_the_session_directory() -> None:
    with pytest.raises(ValidationError, match="absolute path"):
        SessionSpec(cwd="work/toy")
    spec = SessionSpec(cwd="/work/toy/")
    run = _rootless(_patch("/home/u/other/src/app.py"))
    placed = place(run, spec.cwd)
    assert placed.cwd == "/work/toy"
    assert _status(APP, placed) is Support.CONTRADICTED
    assert _status(EditPredicate(path="src/app.py", touched=False), placed) is Support.SUPPORTED
    recorded = session()
    assert place(recorded, spec.cwd) is recorded


async def test_the_judge_applies_the_spec_directory() -> None:
    run = _rootless(_patch("/home/u/other/src/app.py"), bash("pytest -q", GREEN))
    spec = SessionSpec.model_validate(
        {"cwd": "/work/toy", "expect": [{"id": "app", "kind": "edit", "path": "src/app.py"}]}
    )
    verdict = await SessionJudge().judge(run, SessionJudgeOptions(offline=True, spec=spec))
    assert verdict.outcome is not Outcome.PASS
    assert [e.met for e in verdict.expectations] == [False]


def test_the_stream_records_where_its_commands_ran(tmp_path: Path) -> None:
    path = _stream(
        tmp_path,
        _command("pytest -q", GREEN, cwd="/work/toy"),
        _change("/home/u/other/src/app.py"),
    )
    run = place(load_codex(path), None)
    assert run.cwd == "/work/toy"
    assert any("the directory its commands ran in" in n for n in run.notes)
    assert _status(APP, run) is Support.CONTRADICTED


@pytest.mark.parametrize("printer", ["pwd", "git rev-parse --show-toplevel"])
def test_a_bare_pwd_names_the_session_directory(tmp_path: Path, printer: str) -> None:
    path = _stream(tmp_path, _command(printer, "/work/toy\n"), _change("/home/u/other/src/app.py"))
    run = place(load_codex(path), None)
    assert run.cwd == "/work/toy"
    assert any(f"`{printer}`" in n for n in run.notes)
    assert _status(APP, run) is Support.CONTRADICTED
    honest = place(
        load_codex(
            _stream(tmp_path, _command(printer, "/work/toy\n"), _change("/work/toy/src/app.py")),
        ),
        None,
    )
    assert _status(APP, honest) is Support.SUPPORTED


def test_conflicting_or_compound_pwd_gives_no_directory(tmp_path: Path) -> None:
    two = _stream(tmp_path, _command("pwd", "/work/toy\n"), _command("pwd", "/work/other\n"))
    assert place(load_codex(two), None).cwd is None
    compound = _stream(tmp_path, _command("cd /work/other && pwd", "/work/other\n"))
    assert place(load_codex(compound), None).cwd is None
    shown = _stream(tmp_path, _command("pwd", "/work/other\n"))
    assert place(load_codex(shown), "/work/toy").cwd == "/work/toy"
    relative = _stream(tmp_path, _command("pwd", "toy\n"))
    assert place(load_codex(relative), None).cwd is None
