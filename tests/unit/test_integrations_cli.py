from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from typer.testing import CliRunner

from evidence_judge.cli import ExitCode, app
from evidence_judge.integrations import MissingExtraError, otel
from evidence_judge.integrations import langfuse as lf
from tests.unit.test_integrations_langfuse import FakeLangfuse

RUN = Path(__file__).parents[1] / "fixtures" / "runs" / "forged_success"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVJ_REDACTION_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")


def judge(*extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "judge",
            "--trace",
            str(RUN / "trace.zip"),
            "--answer-file",
            str(RUN / "answer.txt"),
            "--offline",
            *extra,
        ],
    )


def test_otel_flag_emits_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)

    result = judge("--otel", "-q")

    assert result.exit_code == ExitCode.FAIL, result.output
    [span] = exporter.get_finished_spans()
    outcome = dict(span.events[0].attributes or {})
    assert outcome["gen_ai.evaluation.name"] == "evidence_judge.outcome"
    assert outcome["gen_ai.evaluation.score.label"] == "fail"
    assert outcome["evidence_judge.run_id"] == "trace"


def test_langfuse_flag_pushes_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeLangfuse()
    monkeypatch.setattr(lf, "_client_class", lambda: lambda: client)

    result = judge("--langfuse-trace-id", TRACE_ID, "-q")

    assert result.exit_code == ExitCode.FAIL, result.output
    assert client.flushed == 1
    by_name = {c["name"]: c for c in client.calls}
    assert by_name["evidence_judge.outcome"]["value"] == "fail"
    assert all(c["trace_id"] == TRACE_ID for c in client.calls)


def test_missing_langfuse_credentials_fail_before_judging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    result = judge("--langfuse-trace-id", TRACE_ID)
    assert result.exit_code == ExitCode.USAGE
    assert "LANGFUSE_SECRET_KEY" in result.output
    assert "score" not in result.output


def test_missing_otel_extra_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> None:
        raise MissingExtraError("OpenTelemetry", "otel", ImportError("No module 'opentelemetry'"))

    monkeypatch.setattr(otel, "ensure_available", missing)
    result = judge("--otel")
    assert result.exit_code == ExitCode.USAGE
    assert "evidence-judge[otel]" in result.output


def test_a_missing_otlp_exporter_is_a_usage_error_before_judging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)
    result = judge("--otel")
    assert result.exit_code == ExitCode.USAGE
    text = " ".join(result.output.split())
    assert "opentelemetry-exporter-otlp-proto-http is not installed" in text
    assert "evidence-judge[anthropic,mcp,otel]" in text
    assert "score" not in result.output


def test_missing_langfuse_extra_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "langfuse", None)
    result = judge("--langfuse-trace-id", TRACE_ID)
    assert result.exit_code == ExitCode.USAGE
    assert "evidence-judge[anthropic,mcp,langfuse]" in " ".join(result.output.split())
    assert "score" not in result.output


def test_export_failure_is_a_warning_not_a_new_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeLangfuse):
        def create_score(self, **kwargs: Any) -> None:
            raise ConnectionError("langfuse unreachable")

    monkeypatch.setattr(lf, "_client_class", lambda: Broken)

    result = judge("--langfuse-trace-id", TRACE_ID)

    assert result.exit_code == ExitCode.FAIL
    assert "Langfuse export failed: ConnectionError: langfuse unreachable" in result.output
