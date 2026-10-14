from __future__ import annotations

import pytest

from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.checks.url import UrlChecker
from evidence_judge.core.evidence import Channel
from evidence_judge.core.predicates import Scope, UrlPredicate
from evidence_judge.core.verdict import Support
from tests.unit.conftest import CART_TEXT, act, ctx, final, forged_run, honest_run, make_run, snap


async def test_final_url() -> None:
    pred = UrlPredicate(contains="/order/")
    assert (await UrlChecker().evaluate(pred, ctx(honest_run()))).status is Support.SUPPORTED
    res = await UrlChecker().evaluate(pred, ctx(forged_run()))
    assert res.status is Support.CONTRADICTED
    assert res.reason == "Final URL is /cart, which does not contain '/order/'"


async def test_final_url_without_final_state_is_no_evidence() -> None:
    res = await UrlChecker().evaluate(UrlPredicate(contains="/order/"), ctx(make_run()))
    assert res.status is Support.NO_EVIDENCE


async def test_final_url_regex_and_contains_must_both_hold() -> None:
    pred = UrlPredicate(contains="/order/", regex=r"/order/B\d+")
    res = await UrlChecker().evaluate(pred, ctx(honest_run()))
    assert res.status is Support.CONTRADICTED
    assert "contain '/order/' and match /" in res.reason


async def test_any_url_uses_actions_and_snapshots() -> None:
    run = make_run(
        actions=[act(1, "goto", url="https://shop.test/checkout")],
        snapshots=[snap(2, "/cart", CART_TEXT)],
        final_state=final("/cart", CART_TEXT),
    )
    pred = UrlPredicate(contains="/checkout", scope=Scope.ANY)
    res = await UrlChecker().evaluate(pred, ctx(run))
    assert res.status is Support.SUPPORTED
    assert res.citations[0].evidence_id == "act#1"
    miss = UrlPredicate(contains="/order", scope=Scope.ANY)
    res = await UrlChecker().evaluate(miss, ctx(run))
    assert res.status is Support.CONTRADICTED
    assert "None of 2 visited URLs" in res.reason


async def test_any_url_without_channels_is_no_evidence() -> None:
    run = make_run(captured=frozenset({Channel.NETWORK}))
    res = await UrlChecker().evaluate(UrlPredicate(contains="/x", scope=Scope.ANY), ctx(run))
    assert res.status is Support.NO_EVIDENCE


async def test_url_predicate_needs_a_criterion() -> None:
    with pytest.raises(InvalidPredicateError):
        await UrlChecker().evaluate(UrlPredicate(), ctx(honest_run()))
