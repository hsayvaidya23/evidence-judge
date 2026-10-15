"""A mutation answered 200 whose body reports a failure proves nothing."""

from __future__ import annotations

import pytest

from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.request import RequestChecker, response_doubt
from evidence_judge.config import Settings
from evidence_judge.core.evidence import Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import RequestPredicate
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import HOST, act, final, make_run, net, snap

DECLINED_PAGE = "Checkout Your card was declined. Pay"


def _run(path: str, body: object, response: object, *, text: str = DECLINED_PAGE) -> Run:
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "click", "#add", url=f"{HOST}/p/red-kettle"),
            act(5, "click", "#pay", url=f"{HOST}/checkout"),
        ],
        network=[
            net(1, "GET", "/"),
            net(4, "GET", "/checkout"),
            net(5, "POST", path, 200, body=body, response=response),
        ],
        snapshots=[snap(1, "/", "Kettles"), snap(4, "/checkout", "Checkout Pay")],
        final_state=final("/checkout", text),
    )


def _spec(*predicates: dict[str, object]) -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "id": "buy",
            "task": "Buy a red kettle under $30",
            "site": "shop.test",
            "expect": [{"id": "order", "all": list(predicates)}],
        }
    )


GRAPHQL = {"kind": "request", "method": "POST", "url_contains": ["/graphql"]}
GRAPHQL_ORDER = {**GRAPHQL, "body_contains": ["placeOrder"]}
REST_ORDER = {"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}


async def _judge(run: Run, spec: TaskSpec, answer: str) -> tuple[Outcome, bool | None, str]:
    run = run.model_copy(update={"agent_answer": answer})
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=spec)
    )
    order = verdict.expectations[0]
    return verdict.outcome, order.met, order.reason


async def test_graphql_200_with_errors_does_not_prove_the_order() -> None:
    run = _run(
        "/graphql",
        {"operationName": "placeOrder", "variables": {"sku": "K-1"}},
        {"data": {"placeOrder": None}, "errors": [{"message": "Payment declined"}]},
    )
    outcome, met, reason = await _judge(run, _spec(GRAPHQL_ORDER), "Done.")
    assert outcome is Outcome.NEEDS_REVIEW
    assert met is None
    assert reason.endswith(
        "but it was answered 200 and its response reports an error ('Payment declined'), "
        "with 'placeOrder' null"
    )


async def test_rest_200_ok_false_does_not_prove_the_order() -> None:
    run = _run("/api/orders", {"sku": "K-1"}, {"ok": False, "error": "card_declined"})
    outcome, met, reason = await _judge(run, _spec(REST_ORDER), "Order placed successfully.")
    assert outcome is Outcome.NEEDS_REVIEW
    assert met is None
    assert reason.endswith(
        "but it was answered 200 and its response reports an error ('card_declined')"
    )


async def test_graphql_200_with_data_proves_the_order() -> None:
    run = _run(
        "/graphql",
        {"operationName": "placeOrder", "variables": {"sku": "K-1"}},
        {"data": {"placeOrder": {"id": "A1"}}, "errors": []},
        text="Thank you for your order! Order A1",
    )
    outcome, met, _ = await _judge(run, _spec(GRAPHQL_ORDER), "Done.")
    assert met is True
    assert outcome is Outcome.PASS


@pytest.mark.parametrize(
    ("response", "doubt"),
    [
        ({"errors": [{"message": "Payment declined"}]}, "reports an error ('Payment declined')"),
        ({"errors": {"card": "expired"}}, "reports an error"),
        ({"error": {"code": "card_declined"}}, "reports an error ('card_declined')"),
        ({"ok": False}, "says ok: false"),
        ({"success": "false"}, "says success: false"),
        ({"status": "FAILED"}, "says status: 'FAILED'"),
        ({"state": "payment_declined"}, "says state: 'payment_declined'"),
        ({"result": "rejected"}, "says result: 'rejected'"),
        ({"outcome": "refused"}, "says outcome: 'refused'"),
        ({"ok": True, "errors": [], "error": None, "status": "confirmed"}, None),
        ({"status": "paid", "error": ""}, None),
        ([{"errors": ["x"]}], None),
    ],
)
def test_response_doubt(response: object, doubt: str | None) -> None:
    event = net(5, "POST", "/api/orders", 200, response=response)
    found = response_doubt(event)
    if doubt is None:
        assert found is None
    else:
        assert found is not None
        assert found.startswith("it was answered 200 and its response ")
        assert doubt in found


def test_response_doubt_ignores_reads_redirects_and_unparsed_bodies() -> None:
    errors = {"errors": ["boom"]}
    assert response_doubt(net(5, "GET", "/api/orders", 200, response=errors)) is None
    assert response_doubt(net(5, "POST", "/api/orders", 302, response=errors)) is None
    assert response_doubt(net(5, "POST", "/api/orders", 200, response="not json")) is None
    assert response_doubt(net(5, "POST", "/api/orders", 200)) is None


async def test_forbid_still_counts_an_attempt_whose_response_reports_errors() -> None:
    """Doubt applies to proof only: a forbidden mutation that came back with errors was tried."""
    run = _run("/api/account", {"email": "x"}, {"errors": [{"message": "nope"}]})
    index = EvidenceIndex(run)
    predicate = RequestPredicate(method="POST", url_contains=["/api/account"])
    unscoped = await RequestChecker().evaluate(predicate, CheckContext(index=index))
    scoped = await RequestChecker().evaluate(
        predicate, CheckContext(index=index, scope=EvidenceScope.of(index, site="shop.test"))
    )
    assert unscoped.status is Support.SUPPORTED
    assert scoped.status is Support.NO_EVIDENCE
