"""Request status semantics: the `ok` default, `none`, `any` and null."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from evidence_judge.checks.expectations import evaluate_expectation
from evidence_judge.checks.matching import status_matches
from evidence_judge.checks.registry import default_registry
from evidence_judge.checks.request import RequestChecker
from evidence_judge.config import Settings
from evidence_judge.core.evidence import Run
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


async def test_post_redirect_get_order_is_supported() -> None:
    res = await RequestChecker().evaluate(ORDER, ctx(_prg_run()))
    assert res.status is Support.SUPPORTED, res.reason


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
