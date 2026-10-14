from __future__ import annotations

from dataclasses import dataclass, field

from evidence_judge.core.verdict import RunVerdict

OUTCOME_EVALUATION = "evidence_judge.outcome"
CLAIM_EVALUATION = "evidence_judge.claim"
DIMENSION_EVALUATION_PREFIX = "evidence_judge.dimension."
NOT_ASSESSABLE = "not_assessable"

AttributeValue = str | bool | int | float | list[str]


class MissingExtraError(ImportError):
    """An integration was used without its optional dependencies installed."""

    def __init__(self, integration: str, extra: str, cause: ImportError) -> None:
        super().__init__(
            f"the {integration} integration needs optional dependencies that are not installed "
            f"({cause}); install them with: pip install 'evidence-judge[{extra}]'"
        )
        self.extra = extra


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One evaluation result in a tool-neutral shape: the run outcome, a claim or a dimension."""

    name: str
    score: float | None
    label: str | None
    explanation: str | None
    identifier: str
    attributes: dict[str, AttributeValue] = field(default_factory=dict[str, AttributeValue])


def dimension_name(dimension: str) -> str:
    return f"{DIMENSION_EVALUATION_PREFIX}{dimension}"


def evaluation_records(verdict: RunVerdict) -> list[EvaluationRecord]:
    """The verdict as evaluation results: the outcome, then every claim, then every dimension.

    Attribute keys are unprefixed; each integration namespaces them (``evidence_judge.*``).
    ``identifier`` is unique within one verdict and the same when the verdict is exported again.
    """
    base: dict[str, AttributeValue] = {
        "run_id": verdict.run_id,
        "schema_id": verdict.schema_id,
        "judge_version": verdict.judge_version,
    }
    records = [
        EvaluationRecord(
            name=OUTCOME_EVALUATION,
            score=verdict.score,
            label=verdict.outcome.value,
            explanation=verdict.summary,
            identifier="outcome",
            attributes=dict(base),
        )
    ]
    records.extend(
        EvaluationRecord(
            name=CLAIM_EVALUATION,
            score=None,
            label=claim.status.value,
            explanation=claim.reason,
            identifier=f"claim:{claim.claim_id}",
            attributes={
                **base,
                "claim_id": claim.claim_id,
                "key": claim.key,
                "kind": claim.kind.value,
                "confidence": claim.confidence,
                "checker": claim.checker,
                "evidence_ids": [c.evidence_id for c in claim.citations],
            },
        )
        for claim in verdict.claims
    )
    records.extend(
        EvaluationRecord(
            name=dimension_name(dimension.value),
            score=result.score,
            label=None if result.score is not None else NOT_ASSESSABLE,
            explanation="; ".join(result.findings) or None,
            identifier=f"dimension:{dimension.value}",
            attributes={**base, "dimension": dimension.value},
        )
        for dimension, result in verdict.dimensions.items()
    )
    return records
