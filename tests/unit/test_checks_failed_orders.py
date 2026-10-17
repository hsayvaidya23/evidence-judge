"""An order answer that reports a failure at any depth proves neither the order nor its price."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_judge.checks.matching import json_failure, shows_amount
from evidence_judge.checks.request import response_doubt
from evidence_judge.config import Settings
from evidence_judge.core.evidence import Run
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import (
    _browse_actions,
    _browse_network,
    act,
    final,
    make_run,
    net,
    shop_spec,
    shot,
    snap,
)

FAILED_ORDERS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "prices" / "failed_orders.json").read_text()
)
IDS = [a["name"] for a in FAILED_ORDERS]
CONFIRMED = "Thank you for your order! Order A1 is confirmed."
ANSWER = "Bought the red kettle (order A1 confirmed). It cost $24.00."


@pytest.mark.parametrize("answer", FAILED_ORDERS, ids=IDS)
def test_a_failure_anywhere_hides_the_total(answer: dict[str, object]) -> None:
    body = json.dumps(answer["body"])
    assert (json_failure(answer["body"]) is not None) is answer["failed"]
    assert shows_amount(body, "$24.00") is not answer["failed"]


@pytest.mark.parametrize("answer", FAILED_ORDERS, ids=IDS)
def test_a_failure_anywhere_doubts_the_order(answer: dict[str, object]) -> None:
    doubt = response_doubt(net(6, "POST", "/api/orders", 201, response=answer["body"]))
    assert (doubt is not None) is answer["failed"]
    if doubt is not None:
        assert doubt.startswith("it was answered 201 and its response ")


def test_a_nested_failure_is_named_in_the_doubt() -> None:
    body = {"order": {"id": "A1", "status": "payment_declined", "total": 24.0}}
    assert response_doubt(net(6, "POST", "/api/orders", 201, response=body)) == (
        "it was answered 201 and its response says status: 'payment_declined'"
    )
    body = {"order": {"id": "A1", "is_paid": False}}
    assert response_doubt(net(6, "POST", "/api/orders", 201, response=body)) == (
        "it was answered 201 and its response says is_paid: false"
    )


def _order_run(response: object) -> Run:
    return make_run(
        actions=[
            *_browse_actions(),
            act(5, "click", "#checkout", url="https://shop.test/cart"),
            act(7, "click", "button#place-order", url="https://shop.test/checkout"),
        ],
        network=[
            *_browse_network(),
            net(5, "GET", "/checkout"),
            net(
                6,
                "POST",
                "/api/orders",
                201,
                body={"items": [{"sku": "K-1", "price": 24.0}]},
                response=response,
            ),
            net(7, "GET", "/order/A1"),
        ],
        snapshots=[
            snap(1, "/", "Kettles Red Kettle $24.00 Blue Kettle $19.00"),
            snap(4, "/cart", "Your cart: Red Kettle $24.00 Proceed to checkout"),
            snap(7, "/order/A1", CONFIRMED),
        ],
        screenshots=[shot(7)],
        final_state=final("/order/A1", CONFIRMED, "shot#7"),
    ).model_copy(update={"agent_answer": ANSWER})


@pytest.mark.parametrize("answer", FAILED_ORDERS, ids=IDS)
async def test_a_confirmation_page_does_not_pass_a_failed_order(answer: dict[str, object]) -> None:
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        _order_run(answer["body"]), JudgeOptions(offline=True, spec=shop_spec())
    )
    order = next(c for c in verdict.claims if "placed an order" in c.claim_text)
    if answer["failed"]:
        assert verdict.outcome is not Outcome.PASS
        assert order.status is Support.NO_EVIDENCE
    else:
        assert verdict.outcome is Outcome.PASS
        assert order.status is Support.SUPPORTED
