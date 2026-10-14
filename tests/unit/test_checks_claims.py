from __future__ import annotations

from evidence_judge.checks._common import WEAK_CONTRADICTION_CONFIDENCE
from evidence_judge.checks.claims import NO_PREDICATE_REASON, judge_claim
from evidence_judge.checks.registry import default_registry
from evidence_judge.core.claims import Claim, ClaimKind
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    RequestPredicate,
    Scope,
    ScreenshotPredicate,
    UrlPredicate,
)
from evidence_judge.core.verdict import Support
from tests.unit.conftest import (
    FakeVisionLLM,
    ctx,
    final,
    forged_run,
    honest_run,
    make_run,
    net,
    shot,
    snap,
    uncaptured_network_run,
)

ORDER_SENT = RequestPredicate(method="POST", url_contains=["/api/orders"])
THANKS = PageTextPredicate(contains="Thank you for your order")
ON_ORDER_PAGE = UrlPredicate(contains="/order/")
LOOKS_CONFIRMED = ScreenshotPredicate(question="Is an order confirmation visible?")


def order_claim(*predicates: Predicate) -> Claim:
    return Claim(
        id="c1",
        text="the order was placed",
        quote="I placed the order",
        kind=ClaimKind.OUTCOME,
        key=True,
        predicates=list(predicates),
    )


async def test_honest_claim_is_supported_with_merged_citations() -> None:
    verdict = await judge_claim(
        order_claim(ORDER_SENT, THANKS), ctx(honest_run()), default_registry()
    )
    assert verdict.status is Support.SUPPORTED
    assert verdict.key
    assert verdict.checker == "request+page_text"
    assert [c.evidence_id for c in verdict.citations] == ["net#6", "final"]
    assert verdict.confidence == 1.0


async def test_forged_claim_is_contradicted_by_every_predicate() -> None:
    verdict = await judge_claim(
        order_claim(ORDER_SENT, THANKS, ON_ORDER_PAGE), ctx(forged_run()), default_registry()
    )
    assert verdict.status is Support.CONTRADICTED
    assert "No POST request whose URL contains '/api/orders'" in verdict.reason
    assert "Final page text does not contain 'Thank you for your order'" in verdict.reason
    assert "Final URL is /cart" in verdict.reason
    assert verdict.checker == "request+page_text+url"
    assert verdict.confidence == 1.0


async def test_contradiction_is_as_firm_as_its_firmest_refutation() -> None:
    seen_any = PageTextPredicate(contains="Order A1", scope=Scope.ANY)
    weak = await judge_claim(order_claim(seen_any), ctx(forged_run()), default_registry())
    assert weak.status is Support.CONTRADICTED
    assert weak.confidence < 0.7
    firm = await judge_claim(
        order_claim(seen_any, ORDER_SENT), ctx(forged_run()), default_registry()
    )
    assert firm.status is Support.CONTRADICTED
    assert firm.confidence == 1.0


async def test_uncaptured_channel_keeps_claim_unverified() -> None:
    verdict = await judge_claim(
        order_claim(ORDER_SENT, THANKS), ctx(uncaptured_network_run()), default_registry()
    )
    assert verdict.status is Support.NO_EVIDENCE
    assert verdict.reason.startswith("Network traffic was not captured")
    assert "; also Final page text does not contain" in verdict.reason
    assert verdict.checker == "request"


async def test_one_supported_predicate_is_enough() -> None:
    unseen = PageTextPredicate(contains="Gift wrap added", scope=Scope.ANY)
    verdict = await judge_claim(
        order_claim(unseen, ORDER_SENT), ctx(honest_run()), default_registry()
    )
    assert verdict.status is Support.SUPPORTED
    assert verdict.checker == "request"


async def test_support_against_a_confident_contradiction_is_a_weak_conflict() -> None:
    no_order = make_run(
        network=[net(1, "GET", "/")],
        snapshots=[snap(1, "/", "Thank you for visiting our shop")],
        final_state=final("/", "Thank you for visiting our shop"),
    )
    verdict = await judge_claim(
        order_claim(ORDER_SENT, PageTextPredicate(contains="Thank you", scope=Scope.ANY)),
        ctx(no_order),
        default_registry(),
    )
    assert verdict.status is Support.CONTRADICTED
    assert verdict.confidence == WEAK_CONTRADICTION_CONFIDENCE
    assert verdict.reason.startswith("conflicting evidence: ")
    assert "'Thank you' appears on" in verdict.reason
    assert "No POST request whose URL contains '/api/orders'" in verdict.reason
    assert {c.evidence_id for c in verdict.citations} >= {"snap#1", "net#1"}
    assert verdict.checker == "page_text+request"


async def test_no_predicates() -> None:
    verdict = await judge_claim(order_claim(), ctx(honest_run()), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert verdict.reason == NO_PREDICATE_REASON
    assert verdict.checker == "none"
    assert verdict.confidence == 0.0


async def test_screenshot_skipped_when_deterministic_is_decisive() -> None:
    llm = FakeVisionLLM("yes")
    context = ctx(forged_run(), llm)
    verdict = await judge_claim(
        order_claim(ORDER_SENT, LOOKS_CONFIRMED), context, default_registry()
    )
    assert verdict.status is Support.CONTRADICTED
    assert llm.requests == []
    assert context.usage.llm_calls == 0


async def test_screenshot_consulted_when_deterministic_is_not_decisive() -> None:
    run = make_run(screenshots=[shot(1)])
    llm = FakeVisionLLM("yes")
    context = ctx(run.model_copy(update={"captured": set()}), llm)
    verdict = await judge_claim(
        order_claim(ORDER_SENT, LOOKS_CONFIRMED), context, default_registry()
    )
    assert verdict.status is Support.SUPPORTED
    assert verdict.checker == "screenshot"
    assert verdict.confidence == 0.8
    assert len(llm.requests) == 1
    assert context.usage.llm_calls == 1


async def test_screenshot_only_claim() -> None:
    llm = FakeVisionLLM("no")
    verdict = await judge_claim(
        order_claim(LOOKS_CONFIRMED), ctx(forged_run(), llm), default_registry()
    )
    assert verdict.status is Support.CONTRADICTED
    assert verdict.confidence == WEAK_CONTRADICTION_CONFIDENCE
    assert [c.evidence_id for c in verdict.citations] == ["shot#4"]
