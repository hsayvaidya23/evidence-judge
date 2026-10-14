"""Judge one claim: any satisfied predicate proves it; all predicates contradicted refutes it.

A refuted claim is as firm as its firmest refutation: a confident network contradiction is not
weakened by a point-in-time snapshot that also failed to show the claim.

Deterministic predicates are evaluated first. Vision (screenshot) predicates are consulted only
when the deterministic ones are not decisive, so an LLM never overrides recorded network or page
evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from evidence_judge.checks._common import checker_names, join_reasons, merge_citations
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.registry import CheckerRegistry
from evidence_judge.core.claims import Claim
from evidence_judge.core.verdict import ClaimVerdict, PredicateResult, Support

NO_PREDICATE_REASON = "claim has no checkable predicate"


def is_decisive(results: Sequence[PredicateResult]) -> bool:
    if any(r.status is Support.SUPPORTED for r in results):
        return True
    return bool(results) and all(r.status is Support.CONTRADICTED for r in results)


async def judge_claim(claim: Claim, ctx: CheckContext, registry: CheckerRegistry) -> ClaimVerdict:
    if not claim.predicates:
        return _verdict(claim, Support.NO_EVIDENCE, NO_PREDICATE_REASON, [], 0.0)
    deterministic = [p for p in claim.predicates if registry.is_deterministic(p)]
    fallback = [p for p in claim.predicates if not registry.is_deterministic(p)]
    results = [await registry.evaluate(p, ctx) for p in deterministic]
    if not is_decisive(results):
        results += [await registry.evaluate(p, ctx) for p in fallback]
    return combine(claim, results)


def combine(claim: Claim, results: Sequence[PredicateResult]) -> ClaimVerdict:
    supported = [r for r in results if r.status is Support.SUPPORTED]
    if supported:
        return _verdict(
            claim,
            Support.SUPPORTED,
            join_reasons(supported),
            supported,
            max(r.confidence for r in supported),
        )
    contradicted = [r for r in results if r.status is Support.CONTRADICTED]
    if len(contradicted) == len(results):
        return _verdict(
            claim,
            Support.CONTRADICTED,
            join_reasons(contradicted),
            contradicted,
            max(r.confidence for r in contradicted),
        )
    undecided = [r for r in results if r.status is Support.NO_EVIDENCE]
    reason = join_reasons(undecided)
    if contradicted:
        reason = f"{reason}; also {join_reasons(contradicted)}"
    return _verdict(
        claim,
        Support.NO_EVIDENCE,
        reason,
        undecided,
        min(r.confidence for r in undecided),
        cited=[*undecided, *contradicted],
    )


def _verdict(
    claim: Claim,
    status: Support,
    reason: str,
    deciders: Sequence[PredicateResult],
    confidence: float,
    cited: Sequence[PredicateResult] | None = None,
) -> ClaimVerdict:
    return ClaimVerdict(
        claim_id=claim.id,
        claim_text=claim.text,
        quote=claim.quote,
        kind=claim.kind,
        key=claim.key,
        status=status,
        reason=reason,
        citations=merge_citations(deciders if cited is None else cited),
        checker=checker_names(deciders) or "none",
        confidence=confidence,
    )
