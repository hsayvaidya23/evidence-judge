from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate
from evidence_judge.core.verdict import PredicateResult, Usage
from evidence_judge.llm.base import LLMClient


@dataclass(slots=True)
class CheckContext:
    index: EvidenceIndex
    llm: LLMClient | None = None
    usage: Usage = field(default_factory=Usage)


class PredicateChecker(Protocol):
    """Evaluates one predicate kind. `deterministic` checkers never call an LLM."""

    kind: str
    deterministic: bool

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult: ...
