"""Judge one claim: any satisfied predicate proves it; all predicates contradicted refutes it.

A refuted claim is as firm as its firmest refutation: a confident network contradiction is not
weakened by a point-in-time snapshot that also failed to show the claim.

Support does not outvote a confident refutation from a different kind of evidence. When one
predicate is supported and a predicate of another kind (checker) is contradicted at or above
CONFIDENT_CONTRADICTION, the claim is CONTRADICTED with WEAK_CONTRADICTION_CONFIDENCE
("conflicting evidence"), which sends the run to review: it can neither pass on the weaker
evidence nor fail on this claim alone. Predicates of the same kind are alternatives (a POST to
/order or to /checkout), so a missing alternative does not conflict with a supported one.

Deterministic predicates are evaluated first. Vision (screenshot) predicates are consulted only
when the deterministic ones are not decisive, so an LLM never overrides recorded network or page
evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from evidence_judge.checks._common import (
    CONFIDENT_CONTRADICTION,
    WEAK_CONTRADICTION_CONFIDENCE,
    checker_names,
    join_reasons,
    merge_citations,
    result,
)
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.registry import CheckerRegistry
from evidence_judge.core.claims import Claim
from evidence_judge.core.predicates import vacuity_reason
from evidence_judge.core.verdict import ClaimVerdict, PredicateResult, Support

NO_PREDICATE_REASON = "claim has no checkable predicate"


def is_decisive(results: Sequence[PredicateResult]) -> bool:
    if any(r.status is Support.SUPPORTED for r in results):
        return True
    return bool(results) and all(r.status is Support.CONTRADICTED for r in results)


async def judge_claim(claim: Claim, ctx: CheckContext, registry: CheckerRegistry) -> ClaimVerdict:
    if not claim.predicates:
        return _verdict(claim, Support.NO_EVIDENCE, NO_PREDICATE_REASON, [], 0.0)
    weak = [(p, r) for p in claim.predicates if (r := vacuity_reason(p)) is not None]
    usable = [p for p in claim.predicates if vacuity_reason(p) is None]
    deterministic = [p for p in usable if registry.is_deterministic(p)]
    fallback = [p for p in usable if not registry.is_deterministic(p)]
    results = [
        result(Support.NO_EVIDENCE, f"Predicate cannot prove a claim: {reason}", p.kind)
        for p, reason in weak
    ]
    results += [await registry.evaluate(p, ctx) for p in deterministic]
    if not is_decisive(results):
        results += [await registry.evaluate(p, ctx) for p in fallback]
    return combine(claim, results)


def combine(claim: Claim, results: Sequence[PredicateResult]) -> ClaimVerdict:
    supported = [r for r in results if r.status is Support.SUPPORTED]
    contradicted = [r for r in results if r.status is Support.CONTRADICTED]
    supporting_kinds = {r.checker for r in supported}
    confident = [
        r
        for r in contradicted
        if r.confidence >= CONFIDENT_CONTRADICTION and r.checker not in supporting_kinds
    ]
    if supported and confident:
        return _verdict(
            claim,
            Support.CONTRADICTED,
            f"conflicting evidence: {join_reasons(supported)} supports the claim, but "
            f"{join_reasons(confident)} contradicts it",
            [*supported, *confident],
            WEAK_CONTRADICTION_CONFIDENCE,
        )
    if supported:
        return _verdict(
            claim,
            Support.SUPPORTED,
            join_reasons(supported),
            supported,
            max(r.confidence for r in supported),
        )
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
