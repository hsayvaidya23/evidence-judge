"""Claims from a coding agent's final answer: a heuristic extractor and an LLM one.

Both produce `SessionClaim`s whose quotes occur verbatim in the answer. Neither decides
anything: the predicates they write are checked against the session's tool calls.
"""

from __future__ import annotations

from evidence_judge.sessions.extract.heuristic import (
    MAX_CLAIMS,
    MAX_PREDICATES,
    asserted_fix,
    changed_paths,
    claim_coverage,
    heuristic_claims,
    reports_failure,
    side_effects,
    unchecked_actions,
)
from evidence_judge.sessions.extract.llm import (
    SYSTEM_PROMPT,
    LLMSessionExtractor,
    SessionClaimExtraction,
    ground,
)

__all__ = [
    "MAX_CLAIMS",
    "MAX_PREDICATES",
    "SYSTEM_PROMPT",
    "LLMSessionExtractor",
    "SessionClaimExtraction",
    "asserted_fix",
    "changed_paths",
    "claim_coverage",
    "ground",
    "heuristic_claims",
    "reports_failure",
    "side_effects",
    "unchecked_actions",
]
