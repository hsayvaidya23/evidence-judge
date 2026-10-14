"""Output schema the LLM fills when extracting claims."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evidence_judge.core.claims import ClaimKind
from evidence_judge.core.predicates import Predicate


class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, description="The claim restated in plain words")
    quote: str = Field(
        min_length=1, description="Exact, verbatim span copied from the agent output"
    )
    kind: ClaimKind
    origin: Literal["answer", "reasoning"] = Field(
        default="answer", description="Which part of the agent output the quote comes from"
    )
    key: bool = Field(default=False, description="True if the task's success depends on it")
    predicates: list[Predicate] = Field(
        default_factory=list[Predicate],
        description="1-3 evidence checks; any one satisfied proves the claim",
    )


class ClaimExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claims: list[ExtractedClaim] = Field(default_factory=list[ExtractedClaim])
