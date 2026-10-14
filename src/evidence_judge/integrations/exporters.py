"""Verdict export for the CLI's ``--otel`` and ``--langfuse-trace-id`` flags."""

from __future__ import annotations

from evidence_judge.core.verdict import RunVerdict


def check(*, otel: bool, langfuse_trace_id: str | None) -> None:
    """Fail before judging when a requested exporter cannot work.

    Raises:
        MissingExtraError: the exporter's optional dependencies are not installed.
        ValueError: Langfuse credentials are missing from the environment.
    """
    if otel:
        from evidence_judge.integrations.otel import ensure_available

        ensure_available()
    if langfuse_trace_id is not None:
        from evidence_judge.integrations.langfuse import ensure_configured

        ensure_configured()


def export(verdict: RunVerdict, *, otel: bool, langfuse_trace_id: str | None) -> list[str]:
    """Send the verdict to each requested exporter. Failures become warnings, so an export
    problem never changes the verdict's exit code."""
    warnings: list[str] = []
    if otel:
        from evidence_judge.integrations.otel import emit_verdict_from_env

        try:
            warnings.extend(emit_verdict_from_env(verdict))
        except Exception as e:
            warnings.append(f"OpenTelemetry export failed: {type(e).__name__}: {e}")
    if langfuse_trace_id is not None:
        from evidence_judge.integrations.langfuse import push_to_langfuse

        try:
            push_to_langfuse(verdict, trace_id=langfuse_trace_id)
        except Exception as e:
            warnings.append(f"Langfuse export failed: {type(e).__name__}: {e}")
    return warnings
