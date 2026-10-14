from __future__ import annotations

from evidence_judge.checks.page_text import PageTextChecker
from evidence_judge.core.evidence import Channel
from evidence_judge.core.predicates import PageTextPredicate, Scope
from evidence_judge.core.verdict import Support
from tests.unit.conftest import (
    ALL_CHANNELS,
    CART_TEXT,
    ctx,
    final,
    forged_run,
    honest_run,
    make_run,
)

THANKS = PageTextPredicate(contains="Thank you for your order")


async def test_final_text_supported_with_excerpt() -> None:
    res = await PageTextChecker().evaluate(THANKS, ctx(honest_run()))
    assert res.status is Support.SUPPORTED
    assert res.citations[0].evidence_id == "final"
    assert "thank you for your order" in (res.citations[0].excerpt or "")


async def test_final_text_contradicted_names_final_url() -> None:
    res = await PageTextChecker().evaluate(THANKS, ctx(forged_run()))
    assert res.status is Support.CONTRADICTED
    assert res.reason == (
        "Final page text does not contain 'Thank you for your order'; final URL is /cart"
    )


async def test_final_text_missing_state_is_no_evidence() -> None:
    res = await PageTextChecker().evaluate(THANKS, ctx(make_run()))
    assert res.status is Support.NO_EVIDENCE


async def test_final_text_not_flagged_captured_is_no_evidence() -> None:
    run = make_run(
        final_state=final("/cart", CART_TEXT), captured=ALL_CHANNELS - {Channel.FINAL_STATE}
    )
    res = await PageTextChecker().evaluate(THANKS, ctx(run))
    assert res.status is Support.NO_EVIDENCE


async def test_any_scope_finds_intermediate_snapshot() -> None:
    pred = PageTextPredicate(contains="proceed to checkout", scope=Scope.ANY)
    res = await PageTextChecker().evaluate(pred, ctx(honest_run()))
    assert res.status is Support.SUPPORTED
    assert [c.evidence_id for c in res.citations] == ["snap#4"]


async def test_any_scope_contradicted_and_uncaptured() -> None:
    pred = PageTextPredicate(contains="Order A1", scope=Scope.ANY)
    res = await PageTextChecker().evaluate(pred, ctx(forged_run()))
    assert res.status is Support.CONTRADICTED
    assert "None of 3 recorded page states contains 'Order A1'" in res.reason
    uncaptured = make_run(captured=ALL_CHANNELS - {Channel.SNAPSHOTS})
    res = await PageTextChecker().evaluate(pred, ctx(uncaptured))
    assert res.status is Support.NO_EVIDENCE
