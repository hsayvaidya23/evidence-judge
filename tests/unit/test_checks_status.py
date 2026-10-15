"""Request status semantics: the `ok` default, `none`, `any` and null."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.expectations import evaluate_expectation
from evidence_judge.checks.matching import status_matches
from evidence_judge.checks.registry import default_registry
from evidence_judge.checks.request import RequestChecker
from evidence_judge.config import Settings
from evidence_judge.core.evidence import Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import RequestPredicate
from evidence_judge.core.taskspec import Expectation
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import ctx, honest_run, make_run, net, shop_spec

ORDER = RequestPredicate(method="POST", url_contains=["/api/orders"])


def _prg_run() -> Run:
    """POST/Redirect/GET: the order POST answers 303 and the browser GETs the order page."""
    run = honest_run()
    network = [
        net(6, "POST", "/api/orders", 303, body={"items": [{"price": 24.0}]})
        if e.id == "net#6"
        else e
        for e in run.network
    ]
    return run.model_copy(update={"network": network})


def test_default_status_is_ok() -> None:
    assert ORDER.status == "ok"


@pytest.mark.parametrize(
    ("status", "failed", "spec", "expected"),
    [
        (201, False, "ok", True),
        (303, False, "ok", True),
        (303, True, "ok", False),
        (404, False, "ok", False),
        (None, True, "ok", False),
        (303, False, "2xx", False),
        (303, False, "3xx", True),
        (201, False, "201", True),
        (None, True, "none", True),
        (500, False, "none", False),
        (500, False, None, True),
        (None, True, None, False),
        (None, True, "any", True),
        (500, False, "any", True),
    ],
)
def test_status_matches(status: int | None, failed: bool, spec: str | None, expected: bool) -> None:
    assert status_matches(status, spec, failed=failed) is expected


def _proving(run: Run) -> CheckContext:
    index = EvidenceIndex(run)
    return CheckContext(index=index, scope=EvidenceScope.of(index))


@pytest.mark.parametrize("scoped", [False, True])
async def test_post_redirect_get_order_is_supported(scoped: bool) -> None:
    run = _prg_run()
    res = await RequestChecker().evaluate(ORDER, _proving(run) if scoped else ctx(run))
    assert res.status is Support.SUPPORTED, res.reason


def _redirected(landing: str, *, status: int = 200, location: str | None = None) -> Run:
    """The order POST answers 302; the next page load is `landing`."""
    run = honest_run()
    order = net(6, "POST", "/api/orders", 302, body={"items": [{"price": 24.0}]})
    if location is not None:
        order = order.model_copy(update={"response_headers": {"Location": location}})
    network = [
        order if e.id == "net#6" else net(7, "GET", landing, status) if e.id == "net#7" else e
        for e in run.network
    ]
    return run.model_copy(update={"network": network})


@pytest.mark.parametrize(
    ("landing", "status", "reason"),
    [
        (
            "/cart?error=card_declined",
            200,
            "POST /api/orders → 302 meets every other criterion of a POST request whose URL "
            "contains '/api/orders' with a 2xx or 3xx status, but it was answered 302 and led to "
            "GET /cart?error=card_declined → 200, whose URL marks a failure ('error')",
        ),
        (
            "/checkout/failed",
            200,
            "POST /api/orders → 302 meets every other criterion of a POST request whose URL "
            "contains '/api/orders' with a 2xx or 3xx status, but it was answered 302 and led to "
            "GET /checkout/failed → 200, whose URL marks a failure ('failed')",
        ),
        (
            "/checkout",
            200,
            "POST /api/orders → 302 meets every other criterion of a POST request whose URL "
            "contains '/api/orders' with a 2xx or 3xx status, but it was answered 302 and led to "
            "GET /checkout → 200, back to the page it was sent from, and what that page showed was "
            "not recorded",
        ),
        (
            "/order/A1",
            500,
            "POST /api/orders → 302 meets every other criterion of a POST request whose URL "
            "contains '/api/orders' with a 2xx or 3xx status, but it was answered 302 and led to "
            "GET /order/A1 → 500, which did not load",
        ),
    ],
)
async def test_redirect_to_a_failure_cannot_prove_the_order(
    landing: str, status: int, reason: str
) -> None:
    res = await RequestChecker().evaluate(ORDER, _proving(_redirected(landing, status=status)))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason == reason


async def test_redirect_without_a_later_page_load_cannot_prove_the_order() -> None:
    run = _redirected("/order/A1")
    run = run.model_copy(update={"network": [e for e in run.network if e.id != "net#7"]})
    res = await RequestChecker().evaluate(ORDER, _proving(run))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.endswith(
        "it was answered 302 and no page load after it was recorded, so where it led is unknown"
    )


async def test_redirect_follows_the_location_header() -> None:
    run = _redirected("/order/A1", location="/order/A1")
    beacon = net(7, "GET", "/cart?error=stale", 200).model_copy(
        update={"id": "net#6b", "ts": 650.0}
    )
    run = run.model_copy(update={"network": [*run.network, beacon]})
    res = await RequestChecker().evaluate(ORDER, _proving(run))
    assert res.status is Support.SUPPORTED, res.reason


async def test_redirect_to_a_failure_is_not_checked_without_a_scope() -> None:
    res = await RequestChecker().evaluate(ORDER, ctx(_redirected("/cart?error=card_declined")))
    assert res.status is Support.SUPPORTED


async def test_order_redirected_to_a_declined_cart_does_not_pass() -> None:
    run = _redirected("/cart?error=card_declined").model_copy(
        update={"agent_answer": "I placed the order for the red kettle at $24."}
    )
    spec = shop_spec()
    spec = spec.model_copy(update={"expect": spec.expect[:1]})
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=spec)
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW, verdict.summary
    [order] = verdict.expectations[:1]
    assert order.met is None
    assert "whose URL marks a failure" in order.reason


async def test_forbidden_mutation_redirected_to_an_error_page_still_breaks_the_rule() -> None:
    run = _redirected("/cart?error=card_declined")
    forbid = Expectation.model_validate(
        {
            "id": "no_orders",
            "any": [{"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}],
        }
    )
    res = await evaluate_expectation(forbid, "forbid", ctx(run), default_registry())
    assert res.met is False


async def test_explicit_2xx_still_rejects_a_redirect() -> None:
    pred = RequestPredicate(method="POST", url_contains=["/api/orders"], status="2xx")
    res = await RequestChecker().evaluate(pred, ctx(_prg_run()))
    assert res.status is Support.CONTRADICTED


async def test_post_redirect_get_run_passes_the_shop_spec() -> None:
    run = _prg_run().model_copy(update={"agent_answer": "I placed the order."})
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=shop_spec())
    )
    assert verdict.outcome is Outcome.PASS, verdict.summary


async def test_null_status_needs_a_response() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", None, failed=True)])
    anything = RequestPredicate(method="POST", url_contains=["/api/orders"], status=None)
    res = await RequestChecker().evaluate(anything, ctx(run))
    assert res.status is Support.CONTRADICTED


async def test_none_status_matches_a_request_without_response() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", None, failed=True)])
    pred = RequestPredicate(method="POST", url_contains=["/api/orders"], status="none")
    res = await RequestChecker().evaluate(pred, ctx(run))
    assert res.status is Support.SUPPORTED


def _forbid(status: str | None = "ok") -> Expectation:
    return Expectation.model_validate(
        {
            "id": "no_account_changes",
            "any": [
                {
                    "kind": "request",
                    "method": "POST",
                    "url_contains": ["/api/account"],
                    "status": status,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("status", "failed", "spec", "met"),
    [
        (303, False, "ok", False),
        (200, False, "ok", False),
        (500, False, "ok", None),
        (None, True, "ok", None),
        (500, False, None, False),
        (None, True, None, None),
        (303, False, "2xx", None),
    ],
)
async def test_forbidden_request_outcomes(
    status: int | None, failed: bool, spec: str | None, met: bool | None
) -> None:
    run = make_run(network=[net(1, "POST", "/api/account/email", status, failed=failed)])
    res = await evaluate_expectation(_forbid(spec), "forbid", ctx(run), default_registry())
    assert res.met is met, res.reason


@pytest.mark.parametrize("status", ["ok", "OK", "none", "any", "2xx", "5XX", "201", "599", 204])
def test_valid_status_values_load(status: str | int) -> None:
    pred = RequestPredicate.model_validate(
        {"method": "GET", "url_contains": ["/a"], "status": status}
    )
    assert pred.status == str(status).lower()


@pytest.mark.parametrize("status", ["6xx", "200-299", "20", "099", "600", "success", ""])
def test_invalid_status_values_are_refused_at_load(status: str) -> None:
    with pytest.raises(ValidationError, match="status"):
        RequestPredicate.model_validate({"method": "GET", "url_contains": ["/a"], "status": status})
