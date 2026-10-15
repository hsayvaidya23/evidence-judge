from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import typer.main
from typer.core import TyperGroup
from typer.testing import CliRunner

import evidence_judge.llm.factory
from evidence_judge.cli import app
from evidence_judge.config import Settings
from evidence_judge.hooks.record import append
from evidence_judge.hooks.state import log_head_path
from evidence_judge.llm.base import LLMConfigError, LLMError
from evidence_judge.llm.fake import FakeLLM
from evidence_judge.sessions.extract import SessionClaimExtraction
from evidence_judge.sessions.ingest.claude_code import project_slug

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "sessions"
SUCCESS = FIXTURES / "claude_code" / "honest_success.jsonl"
FAILURE = FIXTURES / "claude_code" / "honest_failure.jsonl"
FILTERED = FIXTURES / "claude_code" / "filtered_tests.jsonl"
CODEX = FIXTURES / "codex" / "honest_success.exec.jsonl"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EVJ_REDACT", raising=False)
    monkeypatch.setenv("EVJ_REDACTION_KEY", "test-key")


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["--claude-code", str(SUCCESS)], 0),
        (["--claude-code", str(FILTERED)], 2),
        (["--codex", str(CODEX)], 0),
        (["--claude-code", str(FAILURE), "--answer", "All tests pass."], 1),
    ],
)
def test_judge_exit_codes(args: list[str], code: int) -> None:
    result = runner.invoke(app, ["session", "judge", "--offline", *args])
    assert result.exit_code == code, result.output


def test_judge_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "verdict.json"
    answer = tmp_path / "answer.txt"
    answer.write_text("I fixed the bug. All tests pass.", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "session",
            "judge",
            "--offline",
            "--claude-code",
            str(FAILURE),
            "--answer-file",
            str(answer),
            "--out",
            str(out),
            "--run-id",
            "forged",
        ],
    )
    assert result.exit_code == 1, result.output
    verdict = json.loads(out.read_text())
    assert verdict["schema_id"] == "evidence-judge/run-verdict@1"
    assert verdict["run_id"] == "forged"
    assert verdict["outcome"] == "fail"
    assert verdict["tamper_detection"]["level"] == "partial"
    assert "wrote json" in result.output


def test_judge_with_spec_and_claims(tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_text("expect:\n  - id: green\n    kind: test_result\n")
    claims = tmp_path / "claims.json"
    claims.write_text(
        json.dumps(
            {
                "claims": [
                    {
                        "id": "x",
                        "text": "lint ran",
                        "quote": "lint ran",
                        "kind": "action",
                        "predicates": [{"kind": "command", "category": "lint"}],
                    }
                ]
            }
        )
    )
    result = runner.invoke(
        app,
        [
            "session",
            "judge",
            "--offline",
            "--claude-code",
            str(SUCCESS),
            "--spec",
            str(spec),
            "--claims",
            str(claims),
            "-q",
        ],
    )
    assert result.exit_code == 1
    assert result.output == ""


def test_require_tamper_evidence_and_evidence_log(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "session",
            "judge",
            "--offline",
            "--claude-code",
            str(SUCCESS),
            "--require-tamper-evidence",
        ],
    )
    assert result.exit_code == 2
    entry: dict[str, object] = {
        "v": 1,
        "agent": "claude-code",
        "ts": "2026-10-15T10:00:00Z",
        "session_id": "s",
        "tool": "Bash",
        "input": {"command": "pytest -q"},
        "exit_code": 0,
        "stdout_sha256": None,
        "stdout_excerpt": "3 passed in 0.01s",
        "stderr_excerpt": "",
        "cwd": "/work/toy",
        "output_truncated": False,
    }
    fix: dict[str, object] = {
        **entry,
        "tool": "Edit",
        "input": {"file_path": "/work/toy/calc.py", "old_string": "a - b", "new_string": "a + b"},
        "exit_code": None,
        "stdout_excerpt": "",
    }
    append(tmp_path / "plain", fix)
    plain = append(tmp_path / "plain", entry)
    state = tmp_path / "state"
    append(tmp_path / "keyed", fix, state)
    keyed = append(tmp_path / "keyed", entry, state)
    base = ["session", "judge", "--offline", "--claude-code", str(SUCCESS)]
    required = [*base, "--require-tamper-evidence"]
    verified = ["--evidence-log", str(keyed), "--log-key", str(state / "log-key")]
    verified += ["--log-anchor", str(log_head_path(state, "s"))]
    assert runner.invoke(app, [*required, "--evidence-log", str(plain)]).exit_code == 2
    assert runner.invoke(app, [*required, *verified]).exit_code == 2
    result = runner.invoke(app, [*required, *verified, "--trusted-log"])
    assert result.exit_code == 0, result.output
    unanchored = ["--evidence-log", str(keyed), "--log-anchor", str(tmp_path / "missing")]
    unanchored += ["--log-key", str(state / "log-key"), "--trusted-log"]
    assert runner.invoke(app, [*required, *unanchored]).exit_code == 3


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "provide a session"),
        (["--claude-code", str(SUCCESS), "--codex", str(CODEX)], "not both"),
        (["--claude-code", "no-such-session-id"], "could not load the session"),
        (
            ["--claude-code", str(SUCCESS), "--answer", "x", "--answer-file", str(SUCCESS)],
            "not both",
        ),
    ],
)
def test_judge_usage_errors(
    args: list[str], message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    result = runner.invoke(app, ["session", "judge", "--offline", *args])
    assert result.exit_code == 3
    assert message in result.output


def test_bad_spec_and_claims(tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_text("expect: [{id: a, kind: nope}]\n")
    result = runner.invoke(
        app, ["session", "judge", "--offline", "--claude-code", str(SUCCESS), "--spec", str(spec)]
    )
    assert result.exit_code == 3 and "invalid session spec" in result.output
    claims = tmp_path / "claims.json"
    claims.write_text('{"claims": [{"id": 1}]}')
    result = runner.invoke(
        app,
        ["session", "judge", "--offline", "--claude-code", str(SUCCESS), "--claims", str(claims)],
    )
    assert result.exit_code == 3 and "invalid claims file" in result.output
    out = tmp_path / "missing" / "dir"
    out.mkdir(parents=True)
    result = runner.invoke(
        app, ["session", "judge", "--offline", "--claude-code", str(SUCCESS), "--out", str(out)]
    )
    assert result.exit_code == 3


def test_latest_session_in_a_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    folder = tmp_path / "projects" / project_slug(project.resolve())
    folder.mkdir(parents=True)
    (folder / "abc.jsonl").write_bytes(SUCCESS.read_bytes())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    result = runner.invoke(
        app, ["session", "judge", "--offline", "--claude-code", "latest", "--project", str(project)]
    )
    assert result.exit_code == 0, result.output


def test_no_redact_and_no_llm_warnings() -> None:
    result = runner.invoke(app, ["session", "judge", "--claude-code", str(SUCCESS), "--no-redact"])
    assert result.exit_code == 0
    assert "redaction disabled" in result.output
    assert "no LLM configured" in result.output


def test_judge_with_an_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeLLM()
    llm.queue(
        SessionClaimExtraction,
        {
            "claims": [
                {
                    "text": "suite passes",
                    "quote": "3 passed",
                    "kind": "outcome",
                    "key": True,
                    "predicates": [{"kind": "test_result"}],
                }
            ]
        },
    )
    monkeypatch.setattr(evidence_judge.llm.factory, "build_llm", lambda settings: llm)
    result = runner.invoke(app, ["session", "judge", "--claude-code", str(SUCCESS)])
    assert result.exit_code == 0, result.output
    assert "llm calls 1" in result.output


@pytest.mark.parametrize(
    ("error", "fragment"),
    [(LLMConfigError("bad model"), "use --offline"), (LLMError("boom"), "LLM error")],
)
def test_llm_errors(monkeypatch: pytest.MonkeyPatch, error: Exception, fragment: str) -> None:
    llm = FakeLLM()
    llm.queue(SessionClaimExtraction, error)
    monkeypatch.setattr(evidence_judge.llm.factory, "build_llm", lambda settings: llm)
    monkeypatch.setattr("evidence_judge.sessions.judge.SessionJudge._claims", _raise(error))
    result = runner.invoke(app, ["session", "judge", "--claude-code", str(SUCCESS)])
    assert result.exit_code == 4
    assert fragment in result.output


def _raise(error: Exception):  # type: ignore[no-untyped-def]
    async def claims(*args: object, **kwargs: object) -> None:
        raise error

    return claims


def test_llm_build_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(settings: Settings) -> None:
        raise LLMError("no SDK")

    monkeypatch.setattr(evidence_judge.llm.factory, "build_llm", broken)
    result = runner.invoke(app, ["session", "judge", "--claude-code", str(SUCCESS)])
    assert result.exit_code == 4
    assert "could not initialise" in result.output


def test_internal_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr("evidence_judge.sessions.cli._run", boom)
    result = runner.invoke(app, ["session", "judge", "--offline", "--claude-code", str(SUCCESS)])
    assert result.exit_code == 4 and "kaput" in result.output
    with pytest.raises(RuntimeError):
        runner.invoke(
            app,
            ["session", "judge", "--offline", "--claude-code", str(SUCCESS), "--debug"],
            catch_exceptions=False,
        )
    monkeypatch.setattr("evidence_judge.sessions.cli.summarize", boom)
    monkeypatch.setattr("evidence_judge.sessions.cli.redact_session", boom)
    result = runner.invoke(app, ["session", "inspect", "--claude-code", str(SUCCESS)])
    assert result.exit_code == 4


def test_invalid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVJ_MAX_COST_USD", "-1")
    result = runner.invoke(app, ["session", "judge", "--offline", "--claude-code", str(SUCCESS)])
    assert result.exit_code == 3 and "invalid settings" in result.output
    result = runner.invoke(app, ["session", "inspect", "--claude-code", str(SUCCESS)])
    assert result.exit_code == 3


def test_inspect() -> None:
    result = runner.invoke(app, ["session", "inspect", "--claude-code", str(FILTERED)])
    assert result.exit_code == 0, result.output
    assert "filter -k" in result.output
    assert "last change" in result.output
    data = json.loads(
        runner.invoke(
            app, ["session", "inspect", "--codex", str(CODEX), "--json", "--no-redact"]
        ).output
    )
    assert data["source"] == "codex"
    assert data["evidence"] == "harness_stream"
    assert [e["id"] for e in data["events"]] == ["edit#1", "cmd#1"]
    assert runner.invoke(app, ["session", "inspect"]).exit_code == 3


def test_cli_doc_lists_session_options() -> None:
    doc = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    group = typer.main.get_command(app)
    assert isinstance(group, TyperGroup)
    sessions = group.commands["session"]
    assert isinstance(sessions, TyperGroup)
    for name in ("judge", "inspect", "rerun"):
        assert re.search(rf"evidence-judge session {name}", doc)
        for param in sessions.commands[name].params:
            for opt in param.opts:
                if opt.startswith("--"):
                    assert f"`{opt}" in doc, f"{opt} of session {name} missing from docs/cli.md"
