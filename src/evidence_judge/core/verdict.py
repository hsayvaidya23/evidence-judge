from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from evidence_judge.core.claims import ClaimKind

SCHEMA_ID = "evidence-judge/run-verdict@1"


class Support(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    NO_EVIDENCE = "no_evidence"


class Outcome(StrEnum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


class Dimension(StrEnum):
    SUCCESS = "success"
    SIDE_EFFECTS = "side_effects"
    STATE = "state"
    GUARDRAILS = "guardrails"
    RECOVERY = "recovery"
    LOOPS = "loops"


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(description="e.g. net#12, act#3, snap#4, shot#7, con#2, final")
    excerpt: str | None = None


class PredicateResult(BaseModel):
    """Outcome of evaluating one predicate against the evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Support
    reason: str
    citations: list[Citation] = Field(default_factory=list[Citation])
    checker: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ClaimVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    claim_text: str
    quote: str = Field(description="Verbatim span from the agent output the claim came from")
    kind: ClaimKind
    key: bool
    status: Support
    reason: str
    citations: list[Citation] = Field(default_factory=list[Citation])
    checker: str
    confidence: float = Field(ge=0.0, le=1.0)


class ExpectationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expectation_id: str
    kind: str = Field(description="expect | forbid | constraint")
    met: bool | None = Field(description="None when the needed evidence channel was not captured")
    reason: str
    citations: list[Citation] = Field(default_factory=list[Citation])


class DimensionScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    score: float | None = Field(ge=0.0, le=1.0, description="None when not assessable")
    findings: list[str] = Field(default_factory=list[str])


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    def add(self, other: Usage) -> None:
        self.llm_calls += other.llm_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        self.latency_ms += other.latency_ms


class RunVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_id: str = SCHEMA_ID
    judge_version: str
    run_id: str
    task: str | None
    outcome: Outcome
    score: float = Field(ge=0.0, le=1.0)
    summary: str
    claims: list[ClaimVerdict] = Field(default_factory=list[ClaimVerdict])
    expectations: list[ExpectationResult] = Field(default_factory=list[ExpectationResult])
    dimensions: dict[Dimension, DimensionScore] = Field(
        default_factory=dict[Dimension, DimensionScore]
    )
    claim_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    deterministic_share: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Share of claim verdicts decided without an LLM"
    )
    warnings: list[str] = Field(default_factory=list[str])
    usage: Usage = Field(default_factory=Usage)
