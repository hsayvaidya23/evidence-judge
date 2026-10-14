"""Log verdicts to Arize Phoenix as span annotations.

Building the annotations needs nothing extra. Pushing them needs the ``phoenix`` extra
(``arize-phoenix-client``); pandas is never required.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, Protocol, TypedDict

from evidence_judge.core.verdict import RunVerdict
from evidence_judge.integrations._common import (
    CLAIM_EVALUATION,
    MissingExtraError,
    evaluation_records,
)

AnnotatorKind = Literal["LLM", "CODE", "HUMAN"]


class AnnotationResult(TypedDict):
    label: NotRequired[str]
    score: NotRequired[float]
    explanation: NotRequired[str]


class SpanAnnotation(TypedDict):
    """Same shape as ``phoenix.client`` ``SpanAnnotationData``."""

    name: str
    span_id: str
    annotator_kind: AnnotatorKind
    result: AnnotationResult
    metadata: dict[str, Any]
    identifier: str


class SpansResource(Protocol):
    def log_span_annotations(self, *, span_annotations: Any, sync: bool) -> Any: ...


class PhoenixClient(Protocol):
    """The part of ``phoenix.client.Client`` this integration uses."""

    @property
    def spans(self) -> SpansResource: ...


def annotator_kind(verdict: RunVerdict) -> AnnotatorKind:
    """``CODE`` when no model call shaped the verdict, otherwise ``LLM``."""
    deterministic = verdict.deterministic_share in (None, 1.0)
    return "CODE" if deterministic and verdict.usage.llm_calls == 0 else "LLM"


def span_annotations(
    verdict: RunVerdict, *, span_id: str, include_claims: bool = True
) -> list[SpanAnnotation]:
    """Phoenix span annotations for a verdict: the outcome, each claim and each dimension.

    ``identifier`` keys each annotation within the span, so logging a verdict for the same span
    again replaces the earlier annotations instead of adding duplicates.
    """
    kind = annotator_kind(verdict)
    annotations: list[SpanAnnotation] = []
    for record in evaluation_records(verdict):
        if record.name == CLAIM_EVALUATION and not include_claims:
            continue
        result: AnnotationResult = {}
        if record.label is not None:
            result["label"] = record.label
        if record.score is not None:
            result["score"] = record.score
        if record.explanation is not None:
            result["explanation"] = record.explanation
        annotations.append(
            {
                "name": record.name,
                "span_id": span_id,
                "annotator_kind": kind,
                "result": result,
                "metadata": dict(record.attributes),
                "identifier": record.identifier,
            }
        )
    return annotations


def span_annotation_rows(
    verdict: RunVerdict, *, span_id: str, include_claims: bool = True
) -> list[dict[str, Any]]:
    """The same annotations as flat rows for ``pandas.DataFrame(rows)`` and
    ``client.spans.log_span_annotations_dataframe``."""
    return [
        {
            "span_id": a["span_id"],
            "name": a["name"],
            "annotator_kind": a["annotator_kind"],
            "label": a["result"].get("label"),
            "score": a["result"].get("score"),
            "explanation": a["result"].get("explanation"),
            "metadata": a["metadata"],
            "identifier": a["identifier"],
        }
        for a in span_annotations(verdict, span_id=span_id, include_claims=include_claims)
    ]


def _default_client() -> PhoenixClient:
    try:
        from phoenix.client import Client
    except ImportError as e:
        raise MissingExtraError("Phoenix", "phoenix", e) from e
    return Client()


def push_to_phoenix(
    verdict: RunVerdict,
    *,
    span_id: str,
    client: PhoenixClient | None = None,
    include_claims: bool = True,
    sync: bool = False,
) -> list[SpanAnnotation]:
    """Log a verdict's annotations on a Phoenix span.

    Without ``client`` a ``phoenix.client.Client()`` is built from ``PHOENIX_*`` environment
    variables. Returns the annotations that were sent.
    """
    annotations = span_annotations(verdict, span_id=span_id, include_claims=include_claims)
    active = client if client is not None else _default_client()
    active.spans.log_span_annotations(span_annotations=annotations, sync=sync)
    return annotations
