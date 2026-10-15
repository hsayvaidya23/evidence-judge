from __future__ import annotations

from evidence_judge.checks.page_text import SNAPSHOT_ABSENCE_CONFIDENCE, PageTextChecker
from evidence_judge.core.evidence import Channel, PageState
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
    snap,
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
    assert res.confidence == 1.0
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
    assert "snapshots are point-in-time" in res.reason
    assert res.confidence == SNAPSHOT_ABSENCE_CONFIDENCE < 0.7
    uncaptured = make_run(captured=ALL_CHANNELS - {Channel.SNAPSHOTS})
    res = await PageTextChecker().evaluate(pred, ctx(uncaptured))
    assert res.status is Support.NO_EVIDENCE


STOCK = PageTextPredicate(contains="In stock")
OUT = PageTextPredicate(contains="Out of stock")


def _final(text: str, unsure: str = "", *, opaque: bool = False) -> PageState:
    return final("/p/kettle", text).model_copy(update={"unsure_text": unsure, "opaque": opaque})


async def test_text_the_page_may_hide_is_never_proof() -> None:
    run = make_run(final_state=_final("Red Kettle Out of stock", "In stock"))
    res = await PageTextChecker().evaluate(STOCK, ctx(run))

    assert res.status is Support.NO_EVIDENCE
    assert "holds 'In stock' only in text the page may hide" in res.reason
    assert res.citations[0].excerpt == "in stock"


async def test_text_the_page_may_hide_keeps_its_absence_from_contradicting() -> None:
    run = make_run(final_state=_final("Red Kettle", "Out of stock"))
    assert (await PageTextChecker().evaluate(OUT, ctx(run))).status is Support.NO_EVIDENCE
    shown = make_run(final_state=_final("Red Kettle Out of stock", "In stock"))
    assert (await PageTextChecker().evaluate(OUT, ctx(shown))).status is Support.SUPPORTED


async def test_a_page_with_text_the_snapshot_cannot_see_is_not_proven_without_it() -> None:
    run = make_run(final_state=_final("Red Kettle", opaque=True))
    res = await PageTextChecker().evaluate(OUT, ctx(run))

    assert res.status is Support.NO_EVIDENCE
    assert "closed shadow root" in res.reason


async def test_any_scope_text_the_page_may_hide() -> None:
    hidden = snap(1, "/p/kettle", "Red Kettle").model_copy(update={"unsure_text": "In stock"})
    pred = PageTextPredicate(contains="In stock", scope=Scope.ANY)
    res = await PageTextChecker().evaluate(pred, ctx(make_run(snapshots=[hidden])))
    assert res.status is Support.NO_EVIDENCE
    assert "appears only in text the page may hide on 1 page state" in res.reason
    assert res.citations[0].evidence_id == "snap#1"
    opaque = snap(1, "/p/kettle", "Red Kettle").model_copy(update={"opaque": True})
    res = await PageTextChecker().evaluate(pred, ctx(make_run(snapshots=[opaque])))
    assert res.status is Support.NO_EVIDENCE
    assert "closed shadow root" in res.reason
