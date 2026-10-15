from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import evidence_judge.engine
import evidence_judge.ingest
import evidence_judge.llm.factory
from evidence_judge import __version__
from evidence_judge.cli import ExitCode, app
from evidence_judge.config import Settings
from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.llm.base import LLMError
from evidence_judge.report.json_report import schema_to_json
from tests.unit.test_report_fixtures import make_run, make_verdict

runner = CliRunner()
EMAIL = "alice@example.com"


@dataclass
class JudgeOptions:
    offline: bool = False
    spec: TaskSpec | None = None
    claims: ClaimSet | None = None


@dataclass
class Calls:
    load_kwargs: dict[str, Any] | None = None
    run: Run | None = None
    options: JudgeOptions | None = None
    llm: object | None = None
    build_llm_called: bool = False


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    path = tmp_path / "run.har"
    path.write_text("{}", encoding="utf-8")
    return path


def install(
    monkeypatch: pytest.MonkeyPatch,
    calls: Calls,
    *,
    outcome: Outcome = Outcome.PASS,
    run: Run | None = None,
    load_error: Exception | None = None,
    judge_error: Exception | None = None,
    llm: object | None = "fake-llm",
) -> None:
    def load_run(**kwargs: Any) -> Run:
        calls.load_kwargs = kwargs
        if load_error is not None:
            raise load_error
        base = run or make_run()
        return base.model_copy(update={"agent_answer": f"Emailed {EMAIL}"})

    def build_llm(settings: Settings) -> object | None:
        calls.build_llm_called = True
        return llm

    class Judge:
        def __init__(self, settings: Settings, llm: object | None = None) -> None:
            calls.llm = llm

        async def judge(self, run: Run, options: JudgeOptions) -> RunVerdict:
            calls.run = run
            calls.options = options
            if judge_error is not None:
                raise judge_error
            return make_verdict(outcome)

    monkeypatch.setattr(evidence_judge.engine, "Judge", Judge)
    monkeypatch.setattr(evidence_judge.engine, "JudgeOptions", JudgeOptions)
    monkeypatch.setattr(evidence_judge.llm.factory, "build_llm", build_llm)
    monkeypatch.setattr(evidence_judge.ingest, "load_run", load_run)
    monkeypatch.delenv("EVJ_REDACT", raising=False)
    monkeypatch.setenv("EVJ_REDACTION_KEY", "test-key")


@pytest.mark.parametrize(
    ("outcome", "code"),
    [(Outcome.PASS, 0), (Outcome.FAIL, 1), (Outcome.NEEDS_REVIEW, 2)],
)
def test_exit_code_follows_outcome(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, outcome: Outcome, code: int
) -> None:
    install(monkeypatch, calls, outcome=outcome)
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == code, result.output
    assert "score 42/100" in result.output


def test_writes_json_and_html(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls, outcome=Outcome.FAIL)
    out = tmp_path / "o" / "verdict.json"
    html = tmp_path / "o" / "report.html"
    result = runner.invoke(
        app, ["judge", "--har", str(evidence), "--out", str(out), "--html", str(html), "-q"]
    )
    assert result.exit_code == ExitCode.FAIL
    assert result.output == ""
    verdict = RunVerdict.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert verdict.outcome is Outcome.FAIL
    assert html.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_passes_inputs_to_load_run(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    answer = tmp_path / "answer.txt"
    answer.write_text("done", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "judge",
            "--trace",
            str(evidence),
            "--answer-file",
            str(answer),
            "--task",
            "buy",
            "--run-id",
            "r9",
        ],
    )
    assert result.exit_code == 0, result.output
    kw = calls.load_kwargs
    assert kw is not None
    assert kw["trace"] == evidence.resolve()
    assert kw["answer_file"] == answer.resolve()
    assert kw["task"] == "buy" and kw["run_id"] == "r9"
    assert kw["har"] is None and kw["answer"] is None
    assert kw["max_body_chars"] == 20_000


def test_offline_skips_llm(monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(app, ["judge", "--har", str(evidence), "--offline"])
    assert result.exit_code == 0
    assert not calls.build_llm_called
    assert calls.llm is None
    assert calls.options is not None and calls.options.offline


NON_FINITE_HAR = (
    '{"log":{"entries":[{"startedDateTime":"2024-01-01T00:00:00Z",'
    '"request":{"method":"GET","url":"https://a.test/"},"response":{"status":STATUS}}]}}'
)


@pytest.mark.parametrize("status", ["1e400", "Infinity", "NaN"])
def test_a_har_with_a_non_finite_status_is_judged_not_an_internal_error(
    tmp_path: Path, status: str
) -> None:
    har = tmp_path / "h.har"
    har.write_text(NON_FINITE_HAR.replace("STATUS", status), encoding="utf-8")
    result = runner.invoke(app, ["judge", "--har", str(har), "--offline"])
    assert result.exit_code != ExitCode.INTERNAL, result.output
    assert "internal error" not in result.output


def test_online_uses_llm(monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path) -> None:
    install(monkeypatch, calls)
    runner.invoke(app, ["judge", "--har", str(evidence)])
    assert calls.build_llm_called
    assert calls.llm == "fake-llm"
    assert calls.options is not None and not calls.options.offline


def test_missing_llm_falls_back_to_offline(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls, llm=None)
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == 0
    assert calls.options is not None and calls.options.offline
    assert "judging offline" in result.output


def test_redacts_by_default(monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert calls.run is not None and calls.run.agent_answer is not None
    assert EMAIL not in calls.run.agent_answer
    assert "[REDACTED:email:" in calls.run.agent_answer
    assert "redacted 1 values (email 1)" in result.output


def test_no_redact(monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(app, ["judge", "--har", str(evidence), "--no-redact"])
    assert calls.run is not None and calls.run.agent_answer == f"Emailed {EMAIL}"
    assert "redaction disabled" in result.output


def test_spec_and_claims_are_loaded_and_redacted(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "id: s\ntask: Email the receipt\nexpect:\n  - id: sent\n    any:\n"
        f"      - {{kind: request, method: POST, body_contains: ['{EMAIL}']}}\n",
        encoding="utf-8",
    )
    claims = tmp_path / "claims.json"
    claims.write_text(
        json.dumps(
            {"claims": [{"id": "c1", "text": f"mailed {EMAIL}", "quote": "q", "kind": "action"}]}
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["judge", "--har", str(evidence), "--spec", str(spec), "--claims", str(claims)]
    )
    assert result.exit_code == 0, result.output
    opts = calls.options
    assert opts is not None and opts.spec is not None and opts.claims is not None
    assert calls.load_kwargs is not None and calls.load_kwargs["task"] == "Email the receipt"
    assert EMAIL not in opts.spec.model_dump_json()
    assert EMAIL not in opts.claims.model_dump_json()


@pytest.mark.parametrize(
    "args",
    [
        ["judge"],
        ["judge", "--har", "/definitely/missing.har"],
        ["judge", "--bogus"],
        ["nosuchcommand"],
    ],
)
def test_usage_errors_exit_3(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, args: list[str]
) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(app, args)
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "Traceback" not in result.output


def test_answer_and_answer_file_conflict(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(
        app, ["judge", "--har", str(evidence), "--answer", "x", "--answer-file", str(evidence)]
    )
    assert result.exit_code == ExitCode.USAGE
    assert "not both" in result.output


def test_invalid_spec_exit_3(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    spec = tmp_path / "bad.yaml"
    spec.write_text("id: [unclosed", encoding="utf-8")
    result = runner.invoke(app, ["judge", "--har", str(evidence), "--spec", str(spec)])
    assert result.exit_code == ExitCode.USAGE
    assert "invalid task spec" in result.output


def test_invalid_claims_exit_3(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    bad = tmp_path / "claims.json"
    bad.write_text('{"claims": [{"id": 1}]}', encoding="utf-8")
    result = runner.invoke(app, ["judge", "--har", str(evidence), "--claims", str(bad)])
    assert result.exit_code == ExitCode.USAGE
    assert "invalid claims file" in result.output


def test_unreadable_evidence_exit_3(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls, load_error=ValueError("not a HAR"))
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.USAGE
    assert "could not load evidence: not a HAR" in result.output
    assert "Traceback" not in result.output


def test_llm_error_exit_4(monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path) -> None:
    install(monkeypatch, calls, judge_error=LLMError("rate limited"))
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.INTERNAL
    assert "LLM error: rate limited" in result.output


def test_internal_error_exit_4(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls, judge_error=RuntimeError("boom"))
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.INTERNAL
    assert "internal error: RuntimeError: boom" in result.output
    assert "Traceback" not in result.output


def test_debug_reraises(monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path) -> None:
    install(monkeypatch, calls, judge_error=RuntimeError("boom"))
    result = runner.invoke(app, ["judge", "--har", str(evidence), "--debug"])
    assert isinstance(result.exception, RuntimeError)


def test_unexpected_engine_error_exit_4(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls, judge_error=RuntimeError("boom"))
    result = runner.invoke(app, ["judge", "--har", str(evidence), "--offline"])
    assert result.exit_code == ExitCode.INTERNAL
    assert "boom" in result.output


def test_inspect_table_and_json(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(app, ["inspect", "--har", str(evidence)])
    assert result.exit_code == 0, result.output
    assert "State-changing requests" in result.output
    result = runner.invoke(app, ["inspect", "--har", str(evidence), "--json"])
    data = json.loads(result.stdout)
    assert data["counts"]["mutations"] == 1
    assert data["final_url"] == "https://shop.test/cart"


def test_inspect_requires_evidence() -> None:
    assert runner.invoke(app, ["inspect"]).exit_code == ExitCode.USAGE


def test_schema_stdout_and_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    assert result.stdout == schema_to_json()
    out = tmp_path / "s.json"
    assert runner.invoke(app, ["schema", "--out", str(out)]).exit_code == 0
    assert out.read_text(encoding="utf-8") == schema_to_json()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"evidence-judge {__version__}"


def test_redaction_key_is_stable_across_invocations(tmp_path: Path) -> None:
    from evidence_judge.cli import _redactor
    from evidence_judge.config import Settings

    settings = Settings(anthropic_api_key=None, cache_dir=tmp_path / "cache")
    first = _redactor(settings).fingerprint("person@example.com")
    second = _redactor(settings).fingerprint("person@example.com")
    assert first == second
    assert (tmp_path / "cache" / "redaction.key").exists()


def test_passes_ingest_safety_options(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    result = runner.invoke(
        app,
        [
            "judge",
            "--har",
            str(evidence),
            "--offline",
            "--allow-mismatch",
            "--screenshot-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == ExitCode.PASS, result.output
    assert calls.load_kwargs is not None
    assert calls.load_kwargs["allow_mismatch"] is True
    assert calls.load_kwargs["screenshot_root"] == tmp_path


def test_rejected_api_key_exits_4_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    from evidence_judge.llm.base import LLMAuthError

    install(
        monkeypatch,
        calls,
        judge_error=LLMAuthError("ANTHROPIC_API_KEY rejected by the Anthropic API (401)"),
    )
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.INTERNAL
    assert "ANTHROPIC_API_KEY rejected" in result.output


def test_har_mismatch_points_to_the_cli_flag(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path
) -> None:
    from evidence_judge.ingest import MismatchedEvidenceError

    install(monkeypatch, calls, load_error=MismatchedEvidenceError("the HAR is from another run."))
    result = runner.invoke(app, ["judge", "--har", str(evidence)])
    assert result.exit_code == ExitCode.USAGE
    assert "--allow-mismatch" in result.output
    assert "allow_mismatch=True" not in result.output


def test_another_runs_har_is_refused_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVJ_REDACTION_KEY", "test-key")
    runs = Path(__file__).parents[1] / "fixtures" / "runs"
    spec = runs / "honest_success" / "spec.yaml"
    result = runner.invoke(
        app,
        [
            "judge",
            "--trace",
            str(runs / "null_agent" / "trace.zip"),
            "--har",
            str(runs / "honest_success" / "run.har"),
            "--answer",
            "I placed the order",
            "--spec",
            str(spec),
            "--offline",
        ],
        terminal_width=400,
    )
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "--allow-mismatch" in result.output


def test_invalid_claims_file_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    bad = tmp_path / "claims.json"
    bad.write_text(
        '{"claims": [{"id": "c1", "text": "card 4111111111111111 pw hunter2", "kind": 7}]}',
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["judge", "--har", str(evidence), "--claims", str(bad)], terminal_width=400
    )
    assert result.exit_code == ExitCode.USAGE
    assert "hunter2" not in result.output and "4111111111111111" not in result.output
    assert "claims.json" in result.output
    assert "claims.0.kind" in result.output


def test_unparseable_claims_file_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch, calls: Calls, evidence: Path, tmp_path: Path
) -> None:
    install(monkeypatch, calls)
    bad = tmp_path / "claims.json"
    bad.write_text('{"claims": [ "secret-sk-ant-abc', encoding="utf-8")
    result = runner.invoke(
        app, ["judge", "--har", str(evidence), "--claims", str(bad)], terminal_width=400
    )
    assert result.exit_code == ExitCode.USAGE
    assert "secret-sk-ant-abc" not in result.output
    assert "json_invalid" in result.output
