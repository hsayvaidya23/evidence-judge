"""An honest checkout through a 3-D Secure challenge on a payment provider's host."""

from __future__ import annotations

from evidence_judge.config import Settings
from evidence_judge.core.evidence import NetworkEvent, PageSnapshot, PageState, Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import HOST, act, make_run, off_site_hint, snap

PAY = "https://hooks.payprovider.test"
CHALLENGE = f"{PAY}/3ds/challenge?pi=pi_1"
CONFIRMED = f"{HOST}/order/A1/confirmed?payment_intent=pi_1"
CONFIRMATION = (
    "Thank you for your order! Order A1 for Red Kettle, total $24.00. If your card is declined "
    "later, we will email you."
)
ANSWER = "I bought the red kettle for $24.00. Order A1 is confirmed."
CALLBACK = "net#8: POST /3ds/authenticate → 302 went to hooks.payprovider.test"


def _ev(
    n: int,
    method: str,
    url: str,
    status: int = 200,
    *,
    location: str | None = None,
    body: str | None = None,
    resource_type: str = "document",
) -> NetworkEvent:
    return NetworkEvent(
        id=f"net#{n}",
        ts=n * 100.0,
        method=method,
        url=url,
        status=status,
        request_body=body,
        response_headers={"Location": location} if location else {},
        resource_type=resource_type,
    )


def _three_ds_run() -> Run:
    """POST /checkout/submit → 303 to the provider's challenge; the provider's POST → 302 back."""
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "click", "#add", url=f"{HOST}/p/red-kettle"),
            act(3, "click", "#checkout", url=f"{HOST}/cart"),
            act(5, "click", "#pay", url=f"{HOST}/checkout"),
            act(7, "fill", "#otp", value="123456", url=CHALLENGE),
            act(8, "click", "#submit", url=CHALLENGE),
        ],
        network=[
            _ev(1, "GET", f"{HOST}/"),
            _ev(2, "POST", f"{HOST}/api/cart", body='{"sku":"K-1"}', resource_type="fetch"),
            _ev(4, "GET", f"{HOST}/checkout"),
            _ev(5, "POST", f"{HOST}/checkout/submit", 303, location=CHALLENGE, body="sku=K-1"),
            _ev(6, "GET", CHALLENGE),
            _ev(8, "POST", f"{PAY}/3ds/authenticate", 302, location=CONFIRMED, body="otp=123456"),
            _ev(9, "GET", CONFIRMED),
        ],
        snapshots=[
            snap(1, "/", "Kettles Red Kettle $24.00"),
            snap(4, "/checkout", "Checkout Pay $24.00"),
            PageSnapshot(
                id="snap#6", ts=660.0, url=CHALLENGE, text="Verify it's you. Enter the code."
            ),
        ],
        final_state=PageState(url=CONFIRMED, text=CONFIRMATION),
    ).model_copy(update={"agent_answer": ANSWER})


def _spec(
    *, allow: list[dict[str, object]] | None = None, allowed_hosts: list[str] | None = None
) -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "id": "buy",
            "task": "Buy a red kettle under $30",
            "site": "shop.test",
            "expect": [
                {
                    "id": "order",
                    "all": [
                        {"kind": "request", "method": "POST", "url_contains": ["/checkout/submit"]}
                    ],
                },
                {
                    "id": "confirmation",
                    "any": [{"kind": "page_text", "contains": "Thank you for your order"}],
                },
            ],
            "allow": [
                {"kind": "request", "method": "POST", "url_contains": ["/api/cart"]},
                *(allow or []),
            ],
            "allowed_hosts": allowed_hosts or [],
        }
    )


async def _judge(spec: TaskSpec) -> RunVerdict:
    return await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        _three_ds_run(), JudgeOptions(offline=True, spec=spec)
    )


async def test_the_provider_callback_is_a_side_effect_with_a_hint_to_allow_it() -> None:
    verdict = await _judge(_spec())
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == (
        f"NEEDS_REVIEW: side effect: {CALLBACK}, outside the site (shop.test), so no "
        "expectation can explain it; "
        + off_site_hint("hooks.payprovider.test", "hooks.payprovider.test/3ds")
        + "."
    )


async def test_following_the_hint_passes_the_honest_checkout() -> None:
    allow = {"kind": "request", "method": "POST", "url_contains": ["hooks.payprovider.test/3ds"]}
    verdict = await _judge(_spec(allow=[allow]))
    assert verdict.outcome is Outcome.PASS, verdict.summary
    assert {e.expectation_id: e.met for e in verdict.expectations} == {
        "order": True,
        "confirmation": True,
    }


async def test_an_allow_entry_may_name_the_provider_by_its_registered_domain() -> None:
    allow = {"kind": "request", "method": "POST", "url_contains": ["payprovider.test/3ds"]}
    verdict = await _judge(_spec(allow=[allow]))
    assert verdict.outcome is Outcome.PASS, verdict.summary


async def test_an_allow_entry_explains_another_host_only_by_name() -> None:
    allow = {"kind": "request", "method": "POST", "url_contains": ["/3ds"]}
    verdict = await _judge(_spec(allow=[allow]))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary.startswith(f"NEEDS_REVIEW: side effect: {CALLBACK}, outside the site")


async def test_listing_the_provider_in_allowed_hosts_does_not_explain_its_callback() -> None:
    """allowed_hosts makes the provider part of the site; its mutation still needs an entry."""
    verdict = await _judge(_spec(allowed_hosts=["hooks.payprovider.test"]))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == (
        "NEEDS_REVIEW: side effect: net#8: POST /3ds/authenticate → 302 is not explained by "
        "the task spec."
    )
