"""Emit verdicts as OpenTelemetry GenAI ``gen_ai.evaluation.result`` events.

Needs the ``otel`` extra. Only the OpenTelemetry API is used to emit; the SDK (and an OTLP
exporter) are needed only by :func:`emit_verdict_from_env`, which the CLI's ``--otel`` uses.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from evidence_judge import __version__
from evidence_judge.core.verdict import RunVerdict
from evidence_judge.integrations._common import (
    OUTCOME_EVALUATION,
    EvaluationRecord,
    MissingExtraError,
    evaluation_records,
)

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import SpanContext, Tracer
    from opentelemetry.util.types import AttributeValue

# OpenTelemetry GenAI semantic conventions for evaluation results. Status: Development. They now
# live in open-telemetry/semantic-conventions-genai and may still be renamed; pin them only here.
EVALUATION_RESULT_EVENT = "gen_ai.evaluation.result"
EVALUATION_NAME = "gen_ai.evaluation.name"
EVALUATION_SCORE_VALUE = "gen_ai.evaluation.score.value"
EVALUATION_SCORE_LABEL = "gen_ai.evaluation.score.label"
EVALUATION_EXPLANATION = "gen_ai.evaluation.explanation"
ERROR_TYPE = "error.type"

ATTRIBUTE_PREFIX = "evidence_judge."
EVALUATION_SPAN = "evidence_judge.evaluate"
INSTRUMENTATION_SCOPE = "evidence_judge"
TRACEPARENT_ENV = "TRACEPARENT"


def _trace_api():
    try:
        from opentelemetry import trace
    except ImportError as e:
        raise MissingExtraError("OpenTelemetry", "otel", e) from e
    return trace


def ensure_available() -> None:
    """Raise :class:`MissingExtraError` unless the OpenTelemetry API is installed."""
    _trace_api()


def event_attributes(record: EvaluationRecord) -> dict[str, AttributeValue]:
    """The attributes of one ``gen_ai.evaluation.result`` event for an evaluation record."""
    attributes: dict[str, AttributeValue] = {EVALUATION_NAME: record.name}
    if record.score is not None:
        attributes[EVALUATION_SCORE_VALUE] = record.score
    if record.label is not None:
        attributes[EVALUATION_SCORE_LABEL] = record.label
    if record.explanation is not None:
        attributes[EVALUATION_EXPLANATION] = record.explanation
    for key, value in record.attributes.items():
        attributes[ATTRIBUTE_PREFIX + key] = value
    return attributes


def _judged_context(
    parent_context: Context | None, trace_id: str | None, span_id: str | None
) -> Context | None:
    if parent_context is not None and (trace_id or span_id):
        raise ValueError("pass either parent_context or trace_id/span_id, not both")
    if parent_context is not None:
        return parent_context
    if not (trace_id or span_id):
        return None
    if not (trace_id and span_id):
        raise ValueError("trace_id and span_id must be given together")
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        set_span_in_context,
    )

    try:
        judged = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except ValueError as e:
        raise ValueError(f"trace_id and span_id must be hex strings: {e}") from e
    if not judged.is_valid:
        raise ValueError("trace_id and span_id must be non-zero 32- and 16-digit hex ids")
    return set_span_in_context(NonRecordingSpan(judged))


def _emit(
    events: list[dict[str, AttributeValue]],
    span_attributes: dict[str, AttributeValue],
    *,
    tracer: Tracer | None,
    parent_context: Context | None,
    trace_id: str | None,
    span_id: str | None,
) -> SpanContext:
    trace = _trace_api()
    context = _judged_context(parent_context, trace_id, span_id)
    judged: SpanContext | None = (
        trace.get_current_span(context).get_span_context() if context is not None else None
    )
    links = [trace.Link(judged)] if judged is not None and judged.is_valid else []
    active: Tracer = tracer or trace.get_tracer(INSTRUMENTATION_SCOPE, __version__)
    span = active.start_span(
        EVALUATION_SPAN, context=context, links=links, attributes=span_attributes
    )
    try:
        for attributes in events:
            span.add_event(EVALUATION_RESULT_EVENT, attributes=attributes)
        return span.get_span_context()
    finally:
        span.end()


def emit_verdict(
    verdict: RunVerdict,
    *,
    tracer: Tracer | None = None,
    parent_context: Context | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> SpanContext:
    """Emit a verdict as ``gen_ai.evaluation.result`` events on a short evaluation span.

    One event for the run (``evidence_judge.outcome``: score and pass/fail/needs_review), one
    per claim (``evidence_judge.claim``: label is the claim status) and one per dimension
    (``evidence_judge.dimension.<name>``). Extra fields are under ``evidence_judge.*``.

    The judged span is given by ``parent_context`` or by hex ``trace_id`` + ``span_id``. The
    evaluation span becomes its child and also links to it, so the events land in the judged
    trace. With neither, the span starts from the current context.

    Returns the evaluation span's context.
    """
    events = [event_attributes(record) for record in evaluation_records(verdict)]
    span_attributes: dict[str, AttributeValue] = {
        ATTRIBUTE_PREFIX + "run_id": verdict.run_id,
        ATTRIBUTE_PREFIX + "outcome": verdict.outcome.value,
        ATTRIBUTE_PREFIX + "score": verdict.score,
    }
    return _emit(
        events,
        span_attributes,
        tracer=tracer,
        parent_context=parent_context,
        trace_id=trace_id,
        span_id=span_id,
    )


def emit_error(
    error_type: str,
    *,
    explanation: str | None = None,
    run_id: str | None = None,
    tracer: Tracer | None = None,
    parent_context: Context | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> SpanContext:
    """Emit a ``gen_ai.evaluation.result`` event with ``error.type`` for a run that could not be
    judged (e.g. bad evidence or an LLM failure)."""
    attributes: dict[str, AttributeValue] = {
        EVALUATION_NAME: OUTCOME_EVALUATION,
        ERROR_TYPE: error_type,
    }
    if explanation is not None:
        attributes[EVALUATION_EXPLANATION] = explanation
    span_attributes: dict[str, AttributeValue] = {ERROR_TYPE: error_type}
    if run_id is not None:
        attributes[ATTRIBUTE_PREFIX + "run_id"] = run_id
        span_attributes[ATTRIBUTE_PREFIX + "run_id"] = run_id
    return _emit(
        [attributes],
        span_attributes,
        tracer=tracer,
        parent_context=parent_context,
        trace_id=trace_id,
        span_id=span_id,
    )


def _traceparent_context() -> Context | None:
    traceparent = os.environ.get(TRACEPARENT_ENV)
    if not traceparent:
        return None
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    return TraceContextTextMapPropagator().extract({"traceparent": traceparent})


def _provider_from_env() -> tuple[TracerProvider | None, str | None]:
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None, "OTEL_SDK_DISABLED is set; verdict not exported"
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return None, (
            "no OpenTelemetry tracer provider is configured and opentelemetry-sdk is not "
            "installed; verdict not exported (pip install 'evidence-judge[otel]')"
        )
    protocol = os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "")
    )
    try:
        if protocol.strip().lower() == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        return None, (
            "no OpenTelemetry tracer provider is configured and opentelemetry-exporter-otlp is "
            "not installed; verdict not exported (pip install opentelemetry-exporter-otlp)"
        )
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider, None


def emit_verdict_from_env(verdict: RunVerdict) -> list[str]:
    """Emit a verdict through the globally configured tracer provider and flush it.

    If no provider is configured (no auto-instrumentation), one is built from ``OTEL_*``
    environment variables with an OTLP exporter when ``opentelemetry-sdk`` and
    ``opentelemetry-exporter-otlp`` are installed. A W3C ``TRACEPARENT`` environment variable,
    when set, names the judged span. Returns warnings for the caller to show.
    """
    trace = _trace_api()
    provider = trace.get_tracer_provider()
    owned: TracerProvider | None = None
    warnings: list[str] = []
    if isinstance(provider, trace.ProxyTracerProvider):
        owned, warning = _provider_from_env()
        if owned is None:
            return [warning] if warning else []
    tracer: Tracer = (owned or provider).get_tracer(INSTRUMENTATION_SCOPE, __version__)
    emit_verdict(verdict, tracer=tracer, parent_context=_traceparent_context())
    if owned is not None:
        owned.shutdown()
    else:
        flush = getattr(provider, "force_flush", None)
        if callable(flush) and not flush():
            warnings.append("OpenTelemetry provider did not flush the verdict in time")
    return warnings
