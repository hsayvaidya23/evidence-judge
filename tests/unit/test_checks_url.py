from __future__ import annotations

import pytest

from evidence_judge.checks._common import CONFIDENT_CONTRADICTION, WEAK_CONTRADICTION_CONFIDENCE
from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.checks.url import UrlChecker
from evidence_judge.core.evidence import Channel, NetworkEvent
from evidence_judge.core.predicates import Scope, UrlPredicate
from evidence_judge.core.verdict import Support
from tests.unit.conftest import (
    CART_TEXT,
    act,
    ctx,
    final,
    forged_run,
    honest_run,
    make_run,
    net,
    snap,
)


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


async def test_any_url_with_only_actions_is_no_evidence() -> None:
    run = make_run(
        actions=[act(1, "click", "text=Checkout", url="https://shop.test/cart")],
        captured=frozenset({Channel.ACTIONS}),
    )
    pred = UrlPredicate(contains="/checkout", scope=Scope.ANY)
    res = await UrlChecker().evaluate(pred, ctx(run))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.startswith("Neither page snapshots nor page navigations were captured")


def _document(n: int, path: str, *, resource_type: str | None, mime: str | None) -> NetworkEvent:
    return net(n, "GET", path).model_copy(
        update={"resource_type": resource_type, "mime_type": mime}
    )


@pytest.mark.parametrize(
    ("resource_type", "mime"), [("document", None), (None, "text/html; charset=utf-8")]
)
async def test_any_url_counts_document_navigations(
    resource_type: str | None, mime: str | None
) -> None:
    run = make_run(
        actions=[act(1, "click", "text=Checkout", url="https://shop.test/cart")],
        network=[
            _document(2, "/checkout", resource_type=resource_type, mime=mime),
            net(3, "GET", "/api/checkout-status"),
        ],
        captured=frozenset({Channel.ACTIONS, Channel.NETWORK}),
    )
    pred = UrlPredicate(contains="/checkout", scope=Scope.ANY)
    res = await UrlChecker().evaluate(pred, ctx(run))
    assert res.status is Support.SUPPORTED
    assert [c.evidence_id for c in res.citations] == ["net#2"]
    miss = await UrlChecker().evaluate(UrlPredicate(contains="/order", scope=Scope.ANY), ctx(run))
    assert miss.status is Support.CONTRADICTED


async def test_any_url_ignores_api_calls_as_navigations() -> None:
    run = make_run(
        network=[net(1, "GET", "/checkout/api"), net(2, "POST", "/checkout")],
        captured=frozenset({Channel.ACTIONS, Channel.NETWORK}),
    )
    res = await UrlChecker().evaluate(UrlPredicate(contains="/checkout", scope=Scope.ANY), ctx(run))
    assert res.status is Support.NO_EVIDENCE


async def test_any_url_absent_from_snapshots_only_is_a_weak_contradiction() -> None:
    run = make_run(
        network=[net(1, "POST", "/api/orders", 201)],
        snapshots=[snap(2, "/cart", CART_TEXT)],
        final_state=final("/cart", CART_TEXT),
    )
    res = await UrlChecker().evaluate(UrlPredicate(contains="/checkout", scope=Scope.ANY), ctx(run))
    assert res.status is Support.CONTRADICTED
    assert res.confidence == WEAK_CONTRADICTION_CONFIDENCE < CONFIDENT_CONTRADICTION
    assert "point-in-time" in res.reason


async def test_any_url_absent_with_captured_navigations_stays_confident() -> None:
    run = make_run(
        network=[_document(1, "/cart", resource_type="document", mime=None)],
        snapshots=[snap(2, "/cart", CART_TEXT)],
        final_state=final("/cart", CART_TEXT),
    )
    res = await UrlChecker().evaluate(UrlPredicate(contains="/checkout", scope=Scope.ANY), ctx(run))
    assert res.status is Support.CONTRADICTED
    assert res.confidence == 1.0


async def test_url_predicate_needs_a_criterion() -> None:
    with pytest.raises(InvalidPredicateError):
        await UrlChecker().evaluate(UrlPredicate(), ctx(honest_run()))


async def test_contains_ignores_the_query_string() -> None:
    run = make_run(final_state=final("/login?next=/order/confirmation", "Please sign in"))
    res = await UrlChecker().evaluate(UrlPredicate(contains="/order/confirmation"), ctx(run))
    assert res.status is Support.CONTRADICTED


async def test_any_scope_contains_ignores_the_query_string() -> None:
    run = make_run(
        snapshots=[snap(1, "/login?next=/order/confirmation", "Please sign in")],
        final_state=final("/login?next=/order/confirmation", "Please sign in"),
    )
    pred = UrlPredicate(contains="/order/confirmation", scope=Scope.ANY)
    assert (await UrlChecker().evaluate(pred, ctx(run))).status is Support.CONTRADICTED


async def test_query_contains_matches_the_decoded_query() -> None:
    run = make_run(final_state=final("/search?q=red+kettle&page=2", "Results"))
    hit = UrlPredicate(contains="/search", query_contains="q=red kettle")
    res = await UrlChecker().evaluate(hit, ctx(run))
    assert res.status is Support.SUPPORTED
    assert "query containing 'q=red kettle'" in res.reason
    miss = UrlPredicate(query_contains="q=blue")
    assert (await UrlChecker().evaluate(miss, ctx(run))).status is Support.CONTRADICTED


async def test_regex_still_sees_the_full_url() -> None:
    run = make_run(final_state=final("/login?next=/order/confirmation", "Please sign in"))
    pred = UrlPredicate(regex=r"next=/order/")
    assert (await UrlChecker().evaluate(pred, ctx(run))).status is Support.SUPPORTED
