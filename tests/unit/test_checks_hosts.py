"""Evidence must come from the site the run was about, not from another host or a data: page."""

from __future__ import annotations

import pytest

from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.dimensions import assess_side_effects
from evidence_judge.checks.expectations import evaluate_expectation, evaluate_spec
from evidence_judge.checks.registry import default_registry
from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.evidence import PageState, Run, is_web_page_url
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import PageTextPredicate, RequestPredicate, Scope, UrlPredicate
from evidence_judge.core.taskspec import Expectation, TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import (
    act,
    ctx,
    final,
    forged_run,
    honest_run,
    make_run,
    net,
    shop_spec,
    snap,
)

ORDER = RequestPredicate(method="POST", url_contains=["/api/orders"])
CONFIRMED = PageTextPredicate(contains="Thank you for your order")


def _scoped(run: Run, allowed: list[str] | None = None) -> CheckContext:
    index = EvidenceIndex(run)
    return CheckContext(index=index, scope=EvidenceScope.of(index, allowed or []))


def _third_party_order() -> Run:
    run = forged_run()
    order = net(9, "POST", "/api/orders", 201, host="https://httpbin.org", body={"price": 24})
    return run.model_copy(update={"network": [*run.network, order]})


async def _check(pred: RequestPredicate | PageTextPredicate | UrlPredicate, c: CheckContext):
    return await default_registry().evaluate(pred, c)


async def test_third_party_mutation_cannot_prove_an_order() -> None:
    res = await _check(ORDER, _scoped(_third_party_order()))
    assert res.status is Support.CONTRADICTED
    assert "httpbin.org" in res.reason


async def test_unscoped_checks_still_see_every_host() -> None:
    res = await _check(ORDER, ctx(_third_party_order()))
    assert res.status is Support.SUPPORTED


async def test_mutation_on_a_subdomain_of_the_primary_host_counts() -> None:
    run = honest_run()
    order = net(9, "POST", "/api/orders", 201, host="https://api.shop.test")
    run = run.model_copy(update={"network": [e for e in run.network if e.id != "net#6"] + [order]})
    assert (await _check(ORDER, _scoped(run))).status is Support.SUPPORTED


async def test_allowed_hosts_restrict_reads_too() -> None:
    run = make_run(network=[net(1, "GET", "/search?q=kettle", host="https://other.test")])
    search = RequestPredicate(method="GET", path_contains=["/search"])
    assert (await _check(search, _scoped(run))).status is Support.SUPPORTED
    assert (await _check(search, _scoped(run, ["shop.test"]))).status is Support.CONTRADICTED


async def test_final_data_page_cannot_show_a_confirmation() -> None:
    run = honest_run().model_copy(
        update={"final_state": PageState(url="data:text/html,x", text="Thank you for your order")}
    )
    res = await _check(CONFIRMED, _scoped(run))
    assert res.status is Support.NO_EVIDENCE
    assert "data:" in res.reason


NON_WEB_PAGES = [
    "view-source:https://shop.test/static/app.js",
    "chrome-error://chromewebdata/",
    "chrome-extension://abcdef/popup.html",
    "edge://settings",
    "moz-extension://abcdef/page.html",
    "file:///tmp/order.html",
    "about:blank",
    "data:text/html,Thank you for your order",
]


@pytest.mark.parametrize("url", NON_WEB_PAGES)
def test_only_http_and_https_pages_are_web_pages(url: str) -> None:
    assert not is_web_page_url(url)
    assert not EvidenceScope(primary_host="shop.test").page_allowed(url)


@pytest.mark.parametrize("url", ["https://shop.test/order", "http://127.0.0.1:8000/order"])
def test_site_pages_on_http_and_https_are_allowed(url: str) -> None:
    assert is_web_page_url(url)
    assert EvidenceScope(primary_host="shop.test").page_allowed(url)


@pytest.mark.parametrize("url", NON_WEB_PAGES[:2])
async def test_a_final_non_web_page_cannot_pass_an_order(url: str) -> None:
    claim = Claim(
        id="c1",
        text="Order placed",
        quote="Order placed",
        kind=ClaimKind.OUTCOME,
        key=True,
        predicates=[PageTextPredicate(contains="Thank you for your order", scope=Scope.FINAL)],
    )
    run = make_run(
        actions=[act(1, "goto", url="https://shop.test/"), act(2, "goto", url=url)],
        network=[net(1, "GET", "/")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=PageState(url=url, text='el.textContent = "Thank you for your order!"'),
    )
    judge = Judge(Settings(anthropic_api_key=None, cache_dir=None))
    verdict = await judge.judge(run, JudgeOptions(offline=True, claims=ClaimSet(claims=[claim])))
    assert verdict.outcome is not Outcome.PASS


async def test_any_scope_ignores_pages_off_the_allowed_hosts() -> None:
    other = snap(2, "/notes", "Thank you for your order").model_copy(
        update={"url": "https://notes.example/x"}
    )
    run = make_run(snapshots=[other], final_state=final("/cart", "Your cart"))
    pred = PageTextPredicate(contains="Thank you for your order", scope=Scope.ANY)
    assert (await _check(pred, _scoped(run))).status is Support.SUPPORTED
    assert (await _check(pred, _scoped(run, ["shop.test"]))).status is Support.CONTRADICTED


async def test_url_predicates_ignore_blank_and_data_pages() -> None:
    run = make_run(
        actions=[act(1, "goto", url="about:blank")],
        final_state=PageState(url="data:text/html,/orders/1001", text="x"),
    )
    pred = UrlPredicate(contains="/orders/1001")
    assert (await _check(pred, _scoped(run))).status is not Support.SUPPORTED


async def test_forbid_still_sees_third_party_mutations() -> None:
    forbid = Expectation.model_validate(
        {
            "id": "no_orders",
            "any": [{"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}],
        }
    )
    res = await evaluate_expectation(
        forbid, "forbid", ctx(_third_party_order()), default_registry()
    )
    assert res.met is False


async def test_expect_is_scoped_inside_evaluate_spec() -> None:
    results = await evaluate_spec(shop_spec(), ctx(_third_party_order()), default_registry())
    order = next(r for r in results if r.expectation_id == "order_submitted")
    assert order.met is False


async def test_claim_on_a_third_party_mutation_is_not_supported() -> None:
    claim = Claim(
        id="c1",
        text="order placed",
        quote="placed",
        kind=ClaimKind.OUTCOME,
        key=True,
        predicates=[ORDER],
    )
    verdict = await judge_claim(claim, _scoped(_third_party_order()), default_registry())
    assert verdict.status is not Support.SUPPORTED


async def test_third_party_mutation_matching_an_expectation_is_a_side_effect() -> None:
    c = ctx(_third_party_order())
    spec = shop_spec()
    expectations = await evaluate_spec(spec, c, default_registry())
    findings = assess_side_effects(c.index, [], expectations, spec).findings
    assert any(f.startswith("net#9:") for f in findings)


async def test_foreign_order_and_data_page_do_not_pass_the_generic_spec() -> None:
    spec = TaskSpec.model_validate(
        {
            "id": "g",
            "task": "Buy a red kettle under $30",
            "expect": [
                {
                    "id": "order_placed",
                    "any": [
                        {
                            "kind": "request",
                            "method": "POST",
                            "url_contains": ["order"],
                            "body_contains": ["kettle"],
                        }
                    ],
                },
                {
                    "id": "confirmed",
                    "any": [{"kind": "page_text", "contains": "thank you", "scope": "final"}],
                },
            ],
        }
    )
    run = make_run(
        network=[
            net(0, "GET", "/"),
            net(
                1,
                "POST",
                "/anything/order",
                200,
                host="https://httpbin.org",
                body={"item": "red kettle"},
            ),
        ],
        final_state=PageState(url="data:text/html,x", text="Thank you! red kettle $24.00"),
    )
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=spec)
    )
    assert verdict.outcome is not Outcome.PASS, verdict.summary
