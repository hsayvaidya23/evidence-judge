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
from typing import Final
from urllib.parse import unquote_plus

from evidence_judge.checks._common import (
    CONFIDENT_CONTRADICTION,
    WEAK_CONTRADICTION_CONFIDENCE,
    checker_names,
    join_reasons,
    merge_citations,
    quoted,
    result,
)
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.registry import CheckerRegistry
from evidence_judge.core.claims import Claim, ClaimKind
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    RequestPredicate,
    Scope,
    ScreenshotPredicate,
    UrlPredicate,
    ValuePredicate,
    vacuity_reason,
)
from evidence_judge.core.verdict import ClaimVerdict, PredicateResult, Support
from evidence_judge.textnorm import normalize

NO_PREDICATE_REASON = "claim has no checkable predicate"
MUTATION_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_SHOWN_INPUTS: Final = 3


def is_decisive(results: Sequence[PredicateResult]) -> bool:
    if any(r.status is Support.SUPPORTED for r in results):
        return True
    return bool(results) and all(r.status is Support.CONTRADICTED for r in results)


def is_strong_outcome_predicate(predicate: Predicate) -> bool:
    """A mutation request, or page text or a URL on the final page: what an outcome leaves."""
    match predicate:
        case RequestPredicate(method=str(method)):
            return method.upper() in MUTATION_METHODS
        case PageTextPredicate() | UrlPredicate() | ScreenshotPredicate():
            return predicate.scope is Scope.FINAL
        case _:
            return False


def _reads_page_or_url(predicate: Predicate) -> bool:
    match predicate:
        case PageTextPredicate() | UrlPredicate():
            return True
        case ValuePredicate(page_regex=str()):
            return True
        case _:
            return False


async def _echoed(
    predicate: Predicate, supported: PredicateResult, ctx: CheckContext, registry: CheckerRegistry
) -> str | None:
    """Why `predicate`, `supported` on the evidence, only holds on an echo of the agent's own
    input, or None.

    The predicate is evaluated again with every input (EvidenceIndex.agent_inputs) masked out of
    page text and URLs; if it no longer holds, what supported it was the agent's input.
    """
    if not _reads_page_or_url(predicate):
        return None
    masked = ctx.without_agent_input()
    if masked.index is ctx.index:
        return None
    if (await registry.evaluate(predicate, masked)).status is Support.SUPPORTED:
        return None
    inputs = _cited_inputs(ctx.index.agent_inputs(), supported)
    shown = ", ".join(quoted(i) for i in inputs[:MAX_SHOWN_INPUTS])
    more = f" and {len(inputs) - MAX_SHOWN_INPUTS} more" if len(inputs) > MAX_SHOWN_INPUTS else ""
    return (
        "it holds only where the page or URL echoes the agent's own input (text it typed or "
        f"put in a URL: {shown}{more}), so it proves nothing"
    )


def _cited_inputs(inputs: tuple[str, ...], supported: PredicateResult) -> tuple[str, ...]:
    """The inputs that appear in what `supported` cites, else all of them."""
    cited = " ".join(normalize(unquote_plus(c.excerpt or "")) for c in supported.citations)
    return tuple(i for i in inputs if i in cited) or inputs


async def _unproving(
    claim: Claim,
    predicate: Predicate,
    supported: PredicateResult,
    ctx: CheckContext,
    registry: CheckerRegistry,
) -> str | None:
    """Why a specific `predicate` still cannot prove `claim` in this run, or None."""
    if claim.key and claim.kind is ClaimKind.OUTCOME and not is_strong_outcome_predicate(predicate):
        return (
            "a key outcome claim needs a mutation request or the final page (final-scope page "
            "text or URL) to prove it"
        )
    checks_start = not isinstance(predicate, PageTextPredicate | ValuePredicate) or claim.kind in (
        ClaimKind.ACTION,
        ClaimKind.OUTCOME,
    )
    if checks_start and registry.is_deterministic(predicate):
        before = await registry.evaluate(predicate, ctx.at_start())
        if before.status is Support.SUPPORTED:
            return (
                "it was already true before the agent acted, so every run of the task meets it "
                f"({before.reason})"
            )
    return await _echoed(predicate, supported, ctx, registry)


async def judge_claim(claim: Claim, ctx: CheckContext, registry: CheckerRegistry) -> ClaimVerdict:
    """Judge `claim`. Besides vacuous predicates, a predicate cannot *support* it when the run met
    it before the agent's first action (the baseline), when it only holds on an echo of the
    agent's own input (text it typed or put in a URL), or, for a key outcome claim, when it is
    neither a mutation request nor the final page. Such a predicate still refutes the claim when
    it is contradicted."""
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
    results += [await _evaluate(claim, p, ctx, registry) for p in deterministic]
    if not is_decisive(results):
        results += [await _evaluate(claim, p, ctx, registry) for p in fallback]
    return combine(claim, results)


async def _evaluate(
    claim: Claim, predicate: Predicate, ctx: CheckContext, registry: CheckerRegistry
) -> PredicateResult:
    res = await registry.evaluate(predicate, ctx)
    if res.status is not Support.SUPPORTED:
        return res
    reason = await _unproving(claim, predicate, res, ctx, registry)
    if reason is None:
        return res
    return result(
        Support.NO_EVIDENCE,
        f"Predicate cannot prove a claim: {reason}",
        res.checker,
        res.citations,
    )


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
