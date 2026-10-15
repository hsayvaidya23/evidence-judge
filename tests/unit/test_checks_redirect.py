"""What a mutation answered with a redirect proves: the landing page decides."""

from __future__ import annotations

import pytest

from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.request import RequestChecker, text_failure, url_failure
from evidence_judge.config import Settings
from evidence_judge.core.evidence import NetworkEvent, PageSnapshot, Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import RequestPredicate
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import HOST, act, final, make_run, net, snap

SAVE = RequestPredicate(method="POST", url_contains=["/settings"])
ORDER = RequestPredicate(method="POST", url_contains=["/api/orders"])


@pytest.mark.parametrize(
    ("url", "mark"),
    [
        ("/cart?error=card_declined", "error"),
        ("/cart?errors=2", "errors"),
        ("/checkout/failed", "failed"),
        ("/checkout?reason=payment_failure", "failure"),
        ("/order/pending?payment=card_refused", "refused"),
        ("/cart?status=DECLINED", "DECLINED"),
        ("/checkout/rejected", "rejected"),
        ("/order/1?state=cancelled", "cancelled"),
        ("/order/1?state=canceled", "canceled"),
        ("/checkout?msg=invalid_card", "invalid"),
        ("/login?access=denied", "denied"),
        ("/login?reason=unauthorized", "unauthorized"),
        ("/login?reason=unauthorised", "unauthorised"),
        ("/checkout?session=expired", "expired"),
        ("/checkout?reason=insufficient_funds", "insufficient"),
        ("/checkout?msg=try-again", "try-again"),
        ("/checkout?msg=problem", "problem"),
        ("/checkout?msg=unable_to_charge", "unable"),
        ("/order/not-processed", "not-processed"),
        ("/order?msg=could_not_be_completed", "not_be_completed"),
        ("/order?msg=order%20not%20placed", "not placed"),
        ("/order?state=paymentRefused", "Refused"),
    ],
)
def test_url_failure_markers(url: str, mark: str) -> None:
    assert url_failure(f"{HOST}{url}") == mark


@pytest.mark.parametrize(
    "url",
    [
        "/orders/1001",
        "/order/A1?status=confirmed",
        "/settings",
        "/errorless",
        "/terror-movies",
        "/profile",
        "/cart?error=",
        "/cart?errors=0",
        "/checkout?failed=false",
        "/checkout?declined=none",
    ],
)
def test_url_without_a_failure(url: str) -> None:
    assert url_failure(f"{HOST}{url}") is None


@pytest.mark.parametrize(
    ("text", "mark"),
    [
        ("Your cart. Payment failed: your card was declined.", "failed"),
        ("Thank you for your order. Payment refused, please retry.", "refused"),
        ("Your order was rejected by the bank", "rejected"),
        ("Your order was cancelled", "was cancelled"),
        ("Error: card number is invalid", "Error"),
        ("Invalid display name", "Invalid"),
        ("Access denied", "denied"),
        ("401 Unauthorized", "Unauthorized"),
        ("Your session expired, sign in again", "expired"),
        ("Insufficient funds on this card", "Insufficient"),
        ("Something went wrong. Please try again.", "went wrong"),
        ("We were unable to process your payment", "unable to"),
        ("There was a problem with your payment", "There was a problem"),
        ("Your order could not be placed", "not be placed"),
        ("No order placed: payment declined", "declined"),
    ],
)
def test_text_failure_messages(text: str, mark: str) -> None:
    assert text_failure(text) == mark


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for your order! Order A1 for Red Kettle, total $24.00",
        "Settings saved. Display name: Bob",
        "Saved. No errors found.",
        "0 errors, 3 warnings",
        "Import finished without errors",
        "Error-free checkout, guaranteed",
        "Your card was not declined",
        "We use cookies. Accept all Reject all Decline",
        "Orders can be cancelled within 24 hours. Cancel order",
        "Having a problem? Contact us",
        "Errorless delivery since 1999",
    ],
)
def test_text_without_a_failure(text: str) -> None:
    assert text_failure(text) is None


def _scoped(run: Run) -> CheckContext:
    index = EvidenceIndex(run)
    return CheckContext(index=index, scope=EvidenceScope.of(index))


def _settings_run(*, after: str | None, before: str = "Display name: Alice") -> Run:
    """POST /settings answered 302 back to /settings, as a settings form saves in place."""
    save = NetworkEvent(
        id="net#4",
        ts=400.0,
        method="POST",
        url=f"{HOST}/settings",
        status=302,
        response_headers={"Location": "/settings"},
    )
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/settings"),
            act(2, "fill", "#name", value="Bob", url=f"{HOST}/settings"),
            act(3, "click", "#save", url=f"{HOST}/settings"),
        ],
        network=[net(1, "GET", "/settings"), save, net(5, "GET", "/settings")],
        snapshots=[snap(1, "/settings", before)],
        final_state=final("/settings", after) if after is not None else None,
    )


async def test_redirect_back_to_a_saved_settings_page_is_supported() -> None:
    run = _settings_run(after="Settings saved. Display name: Bob")
    res = await RequestChecker().evaluate(SAVE, _scoped(run))
    assert res.status is Support.SUPPORTED, res.reason


async def test_redirect_back_to_a_page_reporting_a_failure_is_no_evidence() -> None:
    run = _settings_run(after="Invalid display name. Display name: Alice")
    res = await RequestChecker().evaluate(SAVE, _scoped(run))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.endswith(
        "it was answered 302 and led to GET /settings → 200, whose page reports a failure "
        "('Invalid')"
    )


async def test_redirect_back_to_an_unchanged_page_is_no_evidence() -> None:
    run = _settings_run(after="Display name: Alice")
    res = await RequestChecker().evaluate(SAVE, _scoped(run))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.endswith(
        "led to GET /settings → 200, back to the page it was sent from, which showed the same "
        "as before"
    )


async def test_redirect_back_to_an_unrecorded_page_is_no_evidence() -> None:
    res = await RequestChecker().evaluate(SAVE, _scoped(_settings_run(after=None)))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.endswith(
        "back to the page it was sent from, and what that page showed was not recorded"
    )


def _order_run(landing: str, *, shown: list[PageSnapshot], final_path: str | None = None) -> Run:
    order = net(6, "POST", "/api/orders", 303).model_copy(
        update={"response_headers": {"Location": landing}}
    )
    end = final_path or landing
    return make_run(
        actions=[
            act(5, "goto", url=f"{HOST}/checkout"),
            act(6, "click", "#place", url=f"{HOST}/checkout"),
        ],
        network=[net(5, "GET", "/checkout"), order, net(7, "GET", landing), net(9, "GET", "/")],
        snapshots=[snap(5, "/checkout", "Checkout Confirm order"), *shown],
        final_state=final(end, "Kettles"),
    )


async def test_redirect_to_a_refusal_query_is_no_evidence() -> None:
    landing = "/order/pending?payment=card_refused"
    run = _order_run(landing, shown=[snap(7, landing, "Thank you for your order.")])
    res = await RequestChecker().evaluate(ORDER, _scoped(run))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.endswith("whose URL marks a failure ('refused')")


async def test_redirect_to_a_clean_url_showing_a_failure_is_no_evidence() -> None:
    run = _order_run("/order/A1", shown=[snap(7, "/order/A1", "Payment failed. Order A1")])
    res = await RequestChecker().evaluate(ORDER, _scoped(run))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.endswith("whose page reports a failure ('failed')")


async def test_text_shown_after_leaving_the_landing_page_does_not_count() -> None:
    later = snap(9, "/", "Error loading recommendations").model_copy(update={"ts": 960.0})
    run = _order_run("/order/A1", shown=[snap(7, "/order/A1", "Thank you"), later], final_path="/")
    res = await RequestChecker().evaluate(ORDER, _scoped(run))
    assert res.status is Support.SUPPORTED, res.reason


async def test_settings_saved_in_place_passes_the_spec() -> None:
    spec = TaskSpec.model_validate(
        {
            "id": "settings",
            "task": "Change the display name to Bob",
            "site": "shop.test",
            "expect": [
                {
                    "id": "saved",
                    "all": [{"kind": "request", "method": "POST", "url_contains": ["/settings"]}],
                }
            ],
        }
    )
    run = _settings_run(after="Settings saved. Display name: Bob").model_copy(
        update={"agent_answer": "I changed the display name to Bob; the page said Settings saved."}
    )
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=spec)
    )
    assert verdict.outcome is Outcome.PASS, verdict.summary
