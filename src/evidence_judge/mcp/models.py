"""Typed tool results. Each is a tool's output schema and its structured content."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evidence_judge.core.verdict import Citation, Outcome, RunVerdict, TamperDetection
from evidence_judge.mcp.specs import SpecCheck, SpecProblem

__all__ = [
    "EvidenceSource",
    "ExplainedItem",
    "Explanation",
    "FoundRecording",
    "Injection",
    "InspectResult",
    "JudgeResult",
    "Mutation",
    "RecordingSearch",
    "SpecCheck",
    "SpecProblem",
    "WrittenReport",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class WrittenReport(BaseModel):
    model_config = _FROZEN

    kind: Literal["json", "html"]
    path: str


class JudgeResult(BaseModel):
    model_config = _FROZEN

    verdict: RunVerdict
    reports: list[WrittenReport] = Field(
        default_factory=list[WrittenReport], description="Report files written by this call"
    )
    redaction: str = Field(description="What redaction did to the inputs before judging")
    used_llm: bool = Field(description="Whether a model was called; false when judged offline")


class EvidenceSource(BaseModel):
    model_config = _FROZEN

    name: str = Field(description="playwright-trace, har or browser-use")
    details: dict[str, Any] = Field(description="Source metadata: versions, browser, counts")


class Mutation(BaseModel):
    model_config = _FROZEN

    id: str
    method: str
    status: int | None
    url: str


class Injection(BaseModel):
    """A call that made the run's evidence the agent's own (a script it ran, a mocked route)."""

    model_config = _FROZEN

    id: str
    call: str
    sha256: str | None = Field(
        description="Script hash; list it in a spec's trusted_scripts to vouch for the script"
    )


class InspectResult(BaseModel):
    model_config = _FROZEN

    run_id: str
    source: str
    sources: list[EvidenceSource]
    captured: list[str] = Field(description="Evidence channels the recording holds")
    counts: dict[str, int] = Field(description="Items per channel, plus state-changing requests")
    mutations: list[Mutation]
    injections: list[Injection]
    final_url: str | None
    final_title: str | None
    warnings: list[str]
    redacted: bool


class ExplainedItem(BaseModel):
    model_config = _FROZEN

    kind: Literal["claim", "expect", "forbid", "constraint", "dimension", "tamper_detection"]
    id: str
    status: str = Field(
        description=(
            "contradicted / no_evidence for claims, not_met / unknown for spec entries, "
            "the score for a dimension, the level for tamper detection"
        )
    )
    key: bool = Field(default=False, description="A key claim decides the outcome")
    statement: str | None = Field(default=None, description="The claim, or the quoted answer")
    why: str
    evidence: list[Citation] = Field(default_factory=list[Citation])


class Explanation(BaseModel):
    model_config = _FROZEN

    run_id: str
    outcome: Outcome
    score: float
    summary: str
    supported_claims: int
    items: list[ExplainedItem] = Field(
        description="Everything that kept the run from a clean PASS, most decisive first"
    )
    tamper_detection: TamperDetection | None
    warnings: list[str]
    text: str = Field(description="The same explanation as readable Markdown")


class FoundRecording(BaseModel):
    model_config = _FROZEN

    path: str
    kind: Literal["playwright_trace", "playwright_trace_dir", "har", "browser_use"]
    size_bytes: int
    modified: str = Field(description="Last modification time, ISO 8601 UTC")
    note: str | None = None


class RecordingSearch(BaseModel):
    model_config = _FROZEN

    directory: str
    recordings: list[FoundRecording] = Field(description="Newest first")
    truncated: bool = Field(description="True when the scan stopped at its file or result limit")
