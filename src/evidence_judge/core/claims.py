from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evidence_judge.core.predicates import Predicate, vacuity_reason


class ClaimKind(StrEnum):
    ACTION = "action"
    STATE = "state"
    VALUE = "value"
    OUTCOME = "outcome"


class Claim(BaseModel):
    """One atomic, checkable statement the agent made about what it did or saw."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str = Field(description="The claim in plain words")
    quote: str = Field(description="Verbatim span from the agent output this claim came from")
    kind: ClaimKind
    origin: Literal["answer", "reasoning"] = "answer"
    key: bool = Field(default=False, description="True if the task's success depends on this claim")
    predicates: list[Predicate] = Field(
        default_factory=list[Predicate],
        description="Evidence that would prove the claim; any one satisfied is enough",
    )

    @field_validator("predicates")
    @classmethod
    def _specific(cls, predicates: list[Predicate]) -> list[Predicate]:
        for index, predicate in enumerate(predicates):
            if (reason := vacuity_reason(predicate)) is not None:
                raise ValueError(f"predicate {index} ({predicate.kind}) is too weak: {reason}")
        return predicates


class ClaimSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[Claim] = Field(default_factory=list[Claim])
    extractor: str = "unknown"
    coverage_note: str | None = None
