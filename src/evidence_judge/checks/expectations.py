"""Evaluate a TaskSpec against the evidence with three-valued (Kleene) logic.

An expectation holds when every `all` predicate is supported and, if `any` is non-empty, at
least one `any` predicate is supported. It is refuted when an `all` predicate is contradicted or
every `any` predicate is. Otherwise it is unknown (None). A `forbid` entry is met when the
forbidden behaviour is refuted, i.e. its expectation does NOT hold.

For `forbid`, a request predicate left at the default status (2xx) also catches failed
attempts: a matching request with any status (or none) that did not succeed makes the result
unknown, so the run goes to review instead of passing as if nothing was tried.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from evidence_judge.checks._common import join_reasons, merge_citations
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.matching import as_text
from evidence_judge.checks.registry import CheckerRegistry
from evidence_judge.core.predicates import Predicate, RequestPredicate, ValuePredicate
from evidence_judge.core.taskspec import Expectation, TaskSpec
from evidence_judge.core.verdict import ExpectationResult, PredicateResult, Support

EXPECT: Final = "expect"
FORBID: Final = "forbid"
CONSTRAINT: Final = "constraint"

_DEFAULT_STATUS: Final = RequestPredicate().status
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
    predicates: Sequence[Predicate], ctx: CheckContext, registry: CheckerRegistry
) -> list[PredicateResult]:
    return [await registry.evaluate(p, ctx) for p in predicates]


async def _happened(
    expectation: Expectation, ctx: CheckContext, registry: CheckerRegistry
) -> tuple[bool | None, list[PredicateResult]]:
    all_results = await _evaluate(expectation.all, ctx, registry)
    any_results = await _evaluate(expectation.any, ctx, registry)
    happened = holds(all_results, any_results)
    return happened, _explaining(happened, all_results, any_results)


def _any_status(predicate: Predicate) -> Predicate:
    if isinstance(predicate, RequestPredicate) and predicate.status == _DEFAULT_STATUS:
        return predicate.model_copy(update={"status": None})
    return predicate


def _attempt_of(expectation: Expectation) -> Expectation | None:
    """The same expectation with default-status request predicates widened to any status."""
    widened = expectation.model_copy(
        update={
            "all": [_any_status(p) for p in expectation.all],
            "any": [_any_status(p) for p in expectation.any],
        }
    )
    return None if widened == expectation else widened


async def evaluate_expectation(
    expectation: Expectation, kind: str, ctx: CheckContext, registry: CheckerRegistry
) -> ExpectationResult:
    happened, deciders = await _happened(expectation, ctx, registry)
    met = happened
    reason = join_reasons(deciders)
    if kind == FORBID:
        met = None if happened is None else not happened
        attempt = _attempt_of(expectation) if happened is not True else None
        if attempt is not None:
            attempted, attempts = await _happened(attempt, ctx, registry)
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
    results = [await evaluate_expectation(e, EXPECT, ctx, registry) for e in spec.expect]
    results += [await evaluate_expectation(f, FORBID, ctx, registry) for f in spec.forbid]
    results += [await evaluate_constraint(c, ctx, registry) for c in spec.constraints]
    return results
