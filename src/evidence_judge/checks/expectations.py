"""Evaluate a TaskSpec against the evidence with three-valued (Kleene) logic.

An expectation holds when every `all` predicate is supported and, if `any` is non-empty, at
least one `any` predicate is supported. It is refuted when an `all` predicate is contradicted or
every `any` predicate is. Otherwise it is unknown (None). A `forbid` entry is met when the
forbidden behaviour is refuted, i.e. its expectation does NOT hold.

For `forbid`, a request predicate whose status is `ok` (the default), `2xx` or null also
catches failed attempts: a matching request with any other status, or no response, makes the
result unknown, so the run goes to review instead of passing as if nothing was tried. A
forbidden request answered 2xx or 3xx happened (a POST answered 303 took effect).

An `expect` entry is proven like a claim (`checks.proof`): a predicate the run met before the
agent acted, or one that only holds on an echo of the agent's own input, leaves it unknown.
`forbid` entries and constraints read the evidence as recorded.

An `expect` entry with an explicit `max_count` is broken when more mutations that took effect
match its request predicates than that: "exactly once" is part of the expectation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from evidence_judge.checks._common import (
    MAX_CITATIONS,
    cite_request,
    join_reasons,
    merge_citations,
    plural,
    took_effect,
)
from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.errors import RegexTimeoutError
from evidence_judge.checks.matching import as_text
from evidence_judge.checks.proof import guarded
from evidence_judge.checks.registry import CheckerRegistry
from evidence_judge.checks.request import RequestMatcher
from evidence_judge.core.evidence import NetworkEvent
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import (
    STATUS_ANY,
    STATUS_OK,
    Predicate,
    RequestPredicate,
    ValuePredicate,
)
from evidence_judge.core.taskspec import Expectation, TaskSpec
from evidence_judge.core.verdict import ExpectationResult, PredicateResult, Support

EXPECT: Final = "expect"
FORBID: Final = "forbid"
CONSTRAINT: Final = "constraint"

_WIDENED_STATUSES: Final = frozenset({STATUS_OK, "2xx", None})
_TRUTH: Final = {Support.SUPPORTED: True, Support.CONTRADICTED: False, Support.NO_EVIDENCE: None}


def truth(result: PredicateResult) -> bool | None:
    return _TRUTH[result.status]


def all_of(values: Sequence[bool | None]) -> bool | None:
    if any(v is False for v in values):
        return False
    return True if all(v is True for v in values) else None


def any_of(values: Sequence[bool | None]) -> bool | None:
    if any(v is True for v in values):
        return True
    return False if all(v is False for v in values) else None


def holds(
    all_results: Sequence[PredicateResult], any_results: Sequence[PredicateResult]
) -> bool | None:
    parts = [all_of([truth(r) for r in all_results])]
    if any_results:
        parts.append(any_of([truth(r) for r in any_results]))
    return all_of(parts)


def _explaining(
    outcome: bool | None,
    all_results: Sequence[PredicateResult],
    any_results: Sequence[PredicateResult],
) -> list[PredicateResult]:
    """The results that decided `outcome`, used for its reason and citations."""
    results = [*all_results, *any_results]
    if outcome is True:
        return [r for r in results if r.status is Support.SUPPORTED]
    if outcome is None:
        return [r for r in results if r.status is Support.NO_EVIDENCE]
    refuting_all = [r for r in all_results if r.status is Support.CONTRADICTED]
    return refuting_all or list(any_results)


async def _evaluate(
    predicates: Sequence[Predicate], ctx: CheckContext, registry: CheckerRegistry, proving: bool
) -> list[PredicateResult]:
    if proving:
        return [await guarded(p, ctx, registry, "an expectation") for p in predicates]
    return [await registry.evaluate(p, ctx) for p in predicates]


async def _happened(
    expectation: Expectation, ctx: CheckContext, registry: CheckerRegistry, *, proving: bool
) -> tuple[bool | None, list[PredicateResult]]:
    all_results = await _evaluate(expectation.all, ctx, registry, proving)
    any_results = await _evaluate(expectation.any, ctx, registry, proving)
    happened = holds(all_results, any_results)
    return happened, _explaining(happened, all_results, any_results)


def _any_status(predicate: Predicate) -> Predicate:
    if isinstance(predicate, RequestPredicate) and predicate.status in _WIDENED_STATUSES:
        return predicate.model_copy(update={"status": STATUS_ANY})
    return predicate


def _attempt_of(expectation: Expectation) -> Expectation | None:
    """The same expectation with `ok`, `2xx` and null request statuses widened to any outcome."""
    widened = expectation.model_copy(
        update={
            "all": [_any_status(p) for p in expectation.all],
            "any": [_any_status(p) for p in expectation.any],
        }
    )
    return None if widened == expectation else widened


def matches_any(event: NetworkEvent, matchers: Sequence[RequestMatcher]) -> bool:
    """True if a matcher covers `event`; a regex that times out covers nothing."""
    for matcher in matchers:
        try:
            if matcher.matches(event):
                return True
        except RegexTimeoutError:
            continue
    return False


def matching_mutations(
    predicates: Sequence[RequestPredicate],
    index: EvidenceIndex,
    scope: EvidenceScope | None = None,
) -> list[NetworkEvent]:
    """Mutations that took effect and match one of `predicates`, in recorded order."""
    matchers = [RequestMatcher(p, scope) for p in predicates]
    if not matchers:
        return []
    return [e for e in index.mutations() if took_effect(e) and matches_any(e, matchers)]


def _over_count(expectation: Expectation, ctx: CheckContext) -> ExpectationResult | None:
    limit = expectation.max_count
    if limit is None:
        return None
    hits = matching_mutations(expectation.request_predicates(), ctx.index, ctx.scope)
    if len(hits) <= limit:
        return None
    ids = ", ".join(e.id for e in hits[:MAX_CITATIONS])
    more = ", …" if len(hits) > MAX_CITATIONS else ""
    return ExpectationResult(
        expectation_id=expectation.id,
        kind=EXPECT,
        met=False,
        reason=(
            f"{plural(len(hits), 'matching mutation')} took effect ({ids}{more}), "
            f"at most {limit} expected"
        ),
        citations=[cite_request(e) for e in hits[:MAX_CITATIONS]],
    )


async def evaluate_expectation(
    expectation: Expectation, kind: str, ctx: CheckContext, registry: CheckerRegistry
) -> ExpectationResult:
    if kind == EXPECT and (exceeded := _over_count(expectation, ctx)) is not None:
        return exceeded
    happened, deciders = await _happened(expectation, ctx, registry, proving=kind == EXPECT)
    met = happened
    reason = join_reasons(deciders)
    if kind == FORBID:
        met = None if happened is None else not happened
        attempt = _attempt_of(expectation) if happened is not True else None
        if attempt is not None:
            attempted, attempts = await _happened(attempt, ctx, registry, proving=False)
            if attempted is True:
                met, deciders = None, attempts
                reason = f"attempted forbidden action: {join_reasons(attempts)}"
    return ExpectationResult(
        expectation_id=expectation.id,
        kind=kind,
        met=met,
        reason=reason,
        citations=merge_citations(deciders),
    )


def spec_scope(
    spec: TaskSpec | None, index: EvidenceIndex, site: str | None = None
) -> EvidenceScope:
    """The hosts that may prove claims and `expect` entries for a run graded with `spec`, with
    the spec's page texts and `failure_markers` that decide what a redirect proves; `site`
    stands in for the spec's own `site` (a --site given without a spec)."""
    if spec is None:
        return EvidenceScope.of(index, [], site)
    return EvidenceScope.of(
        index,
        spec.allowed_hosts,
        site or spec.site,
        success_texts=spec.success_texts(),
        failure_markers=spec.failure_markers,
    )


def constraint_id(constraint: ValuePredicate) -> str:
    return f"{constraint.field} {constraint.op} {as_text(constraint.value)}"


async def evaluate_constraint(
    constraint: ValuePredicate, ctx: CheckContext, registry: CheckerRegistry
) -> ExpectationResult:
    res = await registry.evaluate(constraint, ctx)
    return ExpectationResult(
        expectation_id=constraint_id(constraint),
        kind=CONSTRAINT,
        met=truth(res),
        reason=res.reason,
        citations=res.citations,
    )


async def evaluate_spec(
    spec: TaskSpec, ctx: CheckContext, registry: CheckerRegistry
) -> list[ExpectationResult]:
    """`expect` entries must be proven on the site (`spec_scope`); `forbid` and constraints see
    every host, so a forbidden or violating request anywhere still counts."""
    proving = ctx.scoped(spec_scope(spec, ctx.index))
    everything = ctx.scoped(None)
    results = [await evaluate_expectation(e, EXPECT, proving, registry) for e in spec.expect]
    results += [await evaluate_expectation(f, FORBID, everything, registry) for f in spec.forbid]
    results += [await evaluate_constraint(c, everything, registry) for c in spec.constraints]
    return results
