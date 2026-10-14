from __future__ import annotations

import time

import pytest

from evidence_judge.checks.errors import DuplicateCheckerError, UnknownPredicateKindError
from evidence_judge.checks.registry import CheckerRegistry, default_registry
from evidence_judge.checks.request import RequestChecker
from evidence_judge.core.predicates import (
    PageTextPredicate,
    RequestPredicate,
    Scope,
    ScreenshotPredicate,
    UrlPredicate,
)
from evidence_judge.core.verdict import Support
from tests.unit.conftest import ctx, final, honest_run, make_run, snap


def test_default_registry_covers_every_predicate_kind() -> None:
    registry = default_registry()
    for kind in ("request", "page_text", "url", "action", "value", "screenshot"):
        assert registry.get(kind).kind == kind
    assert registry.is_deterministic(RequestPredicate())
    assert not registry.is_deterministic(ScreenshotPredicate(question="?"))


async def test_evaluate_dispatches_by_kind() -> None:
    res = await default_registry().evaluate(
        PageTextPredicate(contains="Thank you"), ctx(honest_run())
    )
    assert res.status is Support.SUPPORTED
    assert res.checker == "page_text"


async def test_invalid_predicate_becomes_no_evidence() -> None:
    res = await default_registry().evaluate(UrlPredicate(), ctx(honest_run()))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.startswith("Predicate cannot be checked: url predicate needs")


def test_unknown_kind_and_duplicates() -> None:
    registry = CheckerRegistry([RequestChecker()])
    with pytest.raises(UnknownPredicateKindError):
        registry.get("page_text")
    with pytest.raises(DuplicateCheckerError):
        registry.register(RequestChecker())


async def test_catastrophic_regex_is_no_evidence_without_hanging() -> None:
    start = time.perf_counter()
    long_page = "/" + "a" * 9000 + "!"
    run = make_run(snapshots=[snap(1, long_page, "x")], final_state=final(long_page, "x"))
    res = await default_registry().evaluate(UrlPredicate(regex="(a+)+$", scope=Scope.ANY), ctx(run))
    assert time.perf_counter() - start < 0.5
    assert res.status is Support.NO_EVIDENCE
    assert "regex timed out" in res.reason
