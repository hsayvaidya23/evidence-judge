"""Push verdicts to Langfuse as scores on a trace or observation. Needs the ``langfuse`` extra."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from evidence_judge.core.verdict import RunVerdict
from evidence_judge.integrations._common import (
    DIMENSION_EVALUATION_PREFIX,
    OUTCOME_EVALUATION,
    MissingExtraError,
    evaluation_records,
)

SCORE_NAME = "evidence_judge.score"
OUTCOME_NAME = OUTCOME_EVALUATION
SCORE_ID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/hsayvaidya23/evidence-judge"
)
REQUIRED_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")

DataType = Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"]


class ScoreClient(Protocol):
    """The part of ``langfuse.Langfuse`` this integration uses."""

    def create_score(
        self,
        *,
        name: str,
        value: Any,
        trace_id: str | None = None,
        observation_id: str | None = None,
        score_id: str | None = None,
        data_type: Any = None,
        comment: str | None = None,
        metadata: Any = None,
    ) -> None: ...

    def flush(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LangfuseScore:
    id: str
    name: str
    value: float | str
    data_type: DataType
    comment: str | None


def score_id(*, run_id: str, name: str, trace_id: str, observation_id: str | None = None) -> str:
    """Deterministic score id, so pushing the same verdict again updates instead of duplicating.

    Includes the target trace/observation because run ids default to the evidence file stem and
    are not unique across runs.
    """
    key = "\x1f".join((run_id, name, trace_id, observation_id or ""))
    return str(uuid.uuid5(SCORE_ID_NAMESPACE, key))


def langfuse_scores(
    verdict: RunVerdict, *, trace_id: str, observation_id: str | None = None
) -> list[LangfuseScore]:
    """The scores a verdict becomes: overall score, outcome and every assessable dimension."""

    def make(
        name: str, value: float | str, data_type: DataType, comment: str | None
    ) -> LangfuseScore:
        sid = score_id(
            run_id=verdict.run_id, name=name, trace_id=trace_id, observation_id=observation_id
        )
        return LangfuseScore(sid, name, value, data_type, comment)

    scores = [
        make(SCORE_NAME, verdict.score, "NUMERIC", verdict.summary),
        make(OUTCOME_NAME, verdict.outcome.value, "CATEGORICAL", verdict.summary),
    ]
    scores.extend(
        make(record.name, record.score, "NUMERIC", record.explanation)
        for record in evaluation_records(verdict)
        if record.name.startswith(DIMENSION_EVALUATION_PREFIX) and record.score is not None
    )
    return scores


def ensure_configured() -> None:
    """Raise unless the Langfuse SDK is installed and its credentials are in the environment."""
    _client_class()
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise ValueError(f"Langfuse credentials missing: set {', '.join(missing)}")


def _client_class() -> type[Any]:
    try:
        from langfuse import Langfuse
    except ImportError as e:
        raise MissingExtraError("Langfuse", "langfuse", e) from e
    return Langfuse


def push_to_langfuse(
    verdict: RunVerdict,
    *,
    trace_id: str,
    observation_id: str | None = None,
    client: ScoreClient | None = None,
) -> list[LangfuseScore]:
    """Create the verdict's scores on a Langfuse trace (or one of its observations).

    Without ``client`` a ``Langfuse()`` client is built from ``LANGFUSE_*`` environment variables
    and flushed before returning; a client you pass is left for you to flush.
    """
    owned = client is None
    active = cast(ScoreClient, _client_class()()) if client is None else client
    metadata = {
        "run_id": verdict.run_id,
        "schema_id": verdict.schema_id,
        "judge_version": verdict.judge_version,
    }
    scores = langfuse_scores(verdict, trace_id=trace_id, observation_id=observation_id)
    for score in scores:
        active.create_score(
            name=score.name,
            value=score.value,
            trace_id=trace_id,
            observation_id=observation_id,
            score_id=score.id,
            data_type=score.data_type,
            comment=score.comment,
            metadata=metadata,
        )
    if owned:
        active.flush()
    return scores
