from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import evidence_judge.llm.factory
from evidence_judge.cli import ExitCode, app
from evidence_judge.config import Settings
from evidence_judge.llm.base import LLMRequest
from evidence_judge.llm.budget import Budget
from evidence_judge.llm.fake import FakeLLM

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"
SITES = Path(__file__).parents[1] / "fixtures" / "sites"
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_bench_writes_json_and_markdown(tmp_path: Path) -> None:
    out, md = tmp_path / "res" / "results.json", tmp_path / "res" / "results.md"
    result = runner.invoke(
        app, ["bench", str(RUNS), "--seeds", "1", "--out", str(out), "--markdown", str(md)]
    )

    assert result.exit_code == ExitCode.PASS, result.output
    data = json.loads(out.read_text())
    assert data["schema_id"] == "evidence-judge/bench-result@1"
    assert data["seeds"] == 1
    assert data["offline"] is True
    pooled = next(
        m
        for m in data["forgery_metrics"]
        if m["judge"] == "evidence-judge+spec" and m["kind"] == "all manipulations"
    )
    assert pooled["false_pass"]["k"] == 0
    assert md.read_text() in result.output
    assert "# Forged-run benchmark" in result.output


def test_quiet_prints_nothing() -> None:
    result = runner.invoke(app, ["bench", str(RUNS), "--seeds", "1", "-q"])
    assert result.exit_code == ExitCode.PASS
    assert result.output == ""


def test_baseline_without_an_llm_exits_internal() -> None:
    result = runner.invoke(app, ["bench", str(RUNS), "--with-llm-baseline", "-q"])
    assert result.exit_code == ExitCode.INTERNAL
    assert "ANTHROPIC_API_KEY" in result.output


def test_bad_inputs_exit_with_usage_errors(tmp_path: Path) -> None:
    assert runner.invoke(app, ["bench", str(tmp_path / "nope")]).exit_code == ExitCode.USAGE
    empty = runner.invoke(app, ["bench", str(tmp_path)])
    assert empty.exit_code == ExitCode.USAGE
    assert "no scenario directories" in empty.output
    assert runner.invoke(app, ["bench", str(RUNS), "--seeds", "0"]).exit_code == ExitCode.USAGE


def test_baseline_runs_with_a_configured_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    budgets: list[Budget | None] = []

    def always_pass(request: LLMRequest, schema: type[BaseModel]) -> dict[str, Any]:
        return {"reasoning": "the agent says it is done", "verdict": "PASS"}

    def build_llm(settings: Settings, *, budget: Budget | None = None) -> FakeLLM:
        budgets.append(budget)
        return FakeLLM(handler=always_pass)

    monkeypatch.setattr(evidence_judge.llm.factory, "build_llm", build_llm)
    result = runner.invoke(
        app, ["bench", str(RUNS), str(SITES), "--seeds", "1", "--with-llm-baseline"]
    )

    assert result.exit_code == ExitCode.PASS, result.output
    assert "| `llm-transcript` | **all manipulations** | 482 | 482 | 100% |" in result.output
    assert budgets[0] is not None
    forged = 8 * 47 + 3 * 39
    assert budgets[0].max_cost_usd == pytest.approx(Settings().max_cost_usd * (17 + 6 * 7 + forged))


@pytest.mark.parametrize("flag", ["--out", "--markdown"])
def test_bench_unwritable_output_is_a_usage_error(tmp_path: Path, flag: str) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    result = runner.invoke(app, ["bench", str(RUNS), "--seeds", "1", flag, str(blocker / "r")])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "cannot write" in result.output


def test_bench_internal_error_exits_4_with_a_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from evidence_judge.bench import BenchmarkRunner

    async def boom(self: BenchmarkRunner, scenarios: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(BenchmarkRunner, "run", boom)
    result = runner.invoke(app, ["bench", str(RUNS), "--seeds", "1"])
    assert result.exit_code == ExitCode.INTERNAL, result.output
    assert "internal error: RuntimeError: boom" in result.output
    assert "--debug" in result.output


@pytest.mark.parametrize(("flags", "redacted"), [([], True), (["--no-redact"], False)])
def test_bench_redaction_follows_no_redact(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], redacted: bool
) -> None:
    import evidence_judge.bench

    seen: list[Any] = []
    real = evidence_judge.bench.BenchmarkRunner

    def capture(*args: Any, **kwargs: Any) -> Any:
        built = real(*args, **kwargs)
        seen.append(built)
        return built

    monkeypatch.setattr(evidence_judge.bench, "BenchmarkRunner", capture)
    result = runner.invoke(app, ["bench", str(RUNS), "--seeds", "1", "-q", *flags])
    assert result.exit_code == ExitCode.PASS, result.output
    assert (seen[0].redactor is not None) is redacted
