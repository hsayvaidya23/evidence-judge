from __future__ import annotations

import builtins
import sys
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from evidence_judge import __version__
from evidence_judge.integrations import MissingExtraError, otel
from tests.unit.test_report_fixtures import make_verdict

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer(exporter: InMemorySpanExporter) -> trace.Tracer:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test")


def only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def events(span: ReadableSpan) -> list[dict[str, Any]]:
    assert all(e.name == "gen_ai.evaluation.result" for e in span.events)
    return [dict(e.attributes or {}) for e in span.events]


def test_semconv_names_are_pinned() -> None:
    assert otel.EVALUATION_RESULT_EVENT == "gen_ai.evaluation.result"
    assert otel.EVALUATION_NAME == "gen_ai.evaluation.name"
    assert otel.EVALUATION_SCORE_VALUE == "gen_ai.evaluation.score.value"
    assert otel.EVALUATION_SCORE_LABEL == "gen_ai.evaluation.score.label"
    assert otel.EVALUATION_EXPLANATION == "gen_ai.evaluation.explanation"
    assert otel.ERROR_TYPE == "error.type"


def test_emits_outcome_claim_and_dimension_events(
    tracer: trace.Tracer, exporter: InMemorySpanExporter
) -> None:
    verdict = make_verdict()
    otel.emit_verdict(verdict, tracer=tracer)

    span = only_span(exporter)
    assert span.name == "evidence_judge.evaluate"
    assert dict(span.attributes or {}) == {
        "evidence_judge.run_id": "run-42",
        "evidence_judge.outcome": "fail",
        "evidence_judge.score": 0.42,
    }
    base = {
        "evidence_judge.run_id": "run-42",
        "evidence_judge.schema_id": "evidence-judge/run-verdict@1",
        "evidence_judge.judge_version": "0.1.0",
    }
    got = events(span)
    assert got[0] == {
        "gen_ai.evaluation.name": "evidence_judge.outcome",
        "gen_ai.evaluation.score.value": 0.42,
        "gen_ai.evaluation.score.label": "fail",
        "gen_ai.evaluation.explanation": verdict.summary,
        **base,
    }
    assert got[1] == {
        "gen_ai.evaluation.name": "evidence_judge.claim",
        "gen_ai.evaluation.score.label": "contradicted",
        "gen_ai.evaluation.explanation": "POST /api/orders returned 500",
        "evidence_judge.claim_id": "c1",
        "evidence_judge.key": True,
        "evidence_judge.kind": "outcome",
        "evidence_judge.confidence": 1.0,
        "evidence_judge.checker": "request",
        "evidence_judge.evidence_ids": ("net#2",),
        **base,
    }
    assert [e["gen_ai.evaluation.score.label"] for e in got[1:4]] == [
        "contradicted",
        "supported",
        "no_evidence",
    ]
    assert got[3]["evidence_judge.evidence_ids"] == ()
    dimensions = {e["evidence_judge.dimension"]: e for e in got[4:]}
    assert set(dimensions) == {"state", "success", "side_effects"}
    assert dimensions["state"] == {
        "gen_ai.evaluation.name": "evidence_judge.dimension.state",
        "gen_ai.evaluation.score.label": "not_assessable",
        "gen_ai.evaluation.explanation": "No snapshots captured",
        "evidence_judge.dimension": "state",
        **base,
    }
    assert dimensions["success"]["gen_ai.evaluation.score.value"] == 0.0
    assert "gen_ai.evaluation.score.label" not in dimensions["success"]
    assert "gen_ai.evaluation.explanation" not in dimensions["side_effects"]
    assert len(got) == 1 + 3 + 3


def test_trace_and_span_ids_parent_and_link_the_judged_span(
    tracer: trace.Tracer, exporter: InMemorySpanExporter
) -> None:
    returned = otel.emit_verdict(make_verdict(), tracer=tracer, trace_id=TRACE_ID, span_id=SPAN_ID)

    span = only_span(exporter)
    assert span.context is not None and span.parent is not None
    assert span.context.trace_id == int(TRACE_ID, 16)
    assert span.parent.span_id == int(SPAN_ID, 16)
    assert [(link.context.trace_id, link.context.span_id) for link in span.links] == [
        (int(TRACE_ID, 16), int(SPAN_ID, 16))
    ]
    assert returned.span_id == span.context.span_id


def test_parent_context_from_a_live_span(
    tracer: trace.Tracer, exporter: InMemorySpanExporter
) -> None:
    with tracer.start_as_current_span("agent.run") as judged:
        otel.emit_verdict(make_verdict(), tracer=tracer, parent_context=None)
        judged_ctx = judged.get_span_context()
    evaluation = next(
        s for s in exporter.get_finished_spans() if s.name == "evidence_judge.evaluate"
    )
    assert evaluation.parent is not None
    assert evaluation.parent.span_id == judged_ctx.span_id

    exporter.clear()
    explicit = trace.set_span_in_context(trace.NonRecordingSpan(judged_ctx))
    otel.emit_verdict(make_verdict(), tracer=tracer, parent_context=explicit)
    span = only_span(exporter)
    assert span.parent is not None and span.parent.span_id == judged_ctx.span_id
    assert [link.context.span_id for link in span.links] == [judged_ctx.span_id]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"trace_id": TRACE_ID}, "together"),
        ({"trace_id": "nothex", "span_id": SPAN_ID}, "hex"),
        ({"trace_id": "0" * 32, "span_id": SPAN_ID}, "non-zero"),
        (
            {"parent_context": trace.set_span_in_context(trace.INVALID_SPAN), "span_id": SPAN_ID},
            "not both",
        ),
    ],
)
def test_bad_judged_span_ids_are_rejected(
    tracer: trace.Tracer, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        otel.emit_verdict(make_verdict(), tracer=tracer, **kwargs)


def test_emit_error_sets_error_type(tracer: trace.Tracer, exporter: InMemorySpanExporter) -> None:
    otel.emit_error("LLMError", explanation="budget exceeded", run_id="run-7", tracer=tracer)
    span = only_span(exporter)
    assert events(span) == [
        {
            "gen_ai.evaluation.name": "evidence_judge.outcome",
            "error.type": "LLMError",
            "gen_ai.evaluation.explanation": "budget exceeded",
            "evidence_judge.run_id": "run-7",
        }
    ]
    assert dict(span.attributes or {})["error.type"] == "LLMError"


def test_from_env_uses_a_configured_global_provider(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setenv("TRACEPARENT", f"00-{TRACE_ID}-{SPAN_ID}-01")

    assert otel.emit_verdict_from_env(make_verdict()) == []

    span = only_span(exporter)
    assert span.instrumentation_scope is not None
    assert span.instrumentation_scope.name == "evidence_judge"
    assert span.instrumentation_scope.version == __version__
    assert span.parent is not None and span.parent.span_id == int(SPAN_ID, 16)


def test_from_env_builds_an_otlp_provider_when_none_is_configured(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda: exporter)

    assert otel.emit_verdict_from_env(make_verdict()) == []
    assert only_span(exporter).name == "evidence_judge.evaluate"


def test_from_env_warns_without_an_otlp_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)

    [warning] = otel.emit_verdict_from_env(make_verdict())
    assert "opentelemetry-exporter-otlp" in warning


def test_from_env_respects_sdk_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert otel.emit_verdict_from_env(make_verdict()) == [
        "OTEL_SDK_DISABLED is set; verdict not exported"
    ]


def test_missing_api_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_otel(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_otel)
    with pytest.raises(MissingExtraError, match=r"evidence-judge\[otel\]"):
        otel.ensure_available()


@pytest.mark.parametrize(
    ("protocol", "module", "package"),
    [
        ("", "opentelemetry.sdk.trace.export", "opentelemetry-sdk"),
        ("", "opentelemetry.exporter.otlp.proto.http.trace_exporter", "proto-http"),
        ("grpc", "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", "proto-grpc"),
    ],
)
def test_ensure_available_needs_what_the_cli_builds_a_provider_from(
    monkeypatch: pytest.MonkeyPatch, protocol: str, module: str, package: str
) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(
        MissingExtraError, match=rf"{package}.*evidence-judge\[anthropic,mcp,otel\]"
    ):
        otel.ensure_available()


def test_ensure_available_skips_the_sdk_when_it_is_not_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace.export", None)
    monkeypatch.setattr(trace, "get_tracer_provider", TracerProvider)
    otel.ensure_available()
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    otel.ensure_available()
