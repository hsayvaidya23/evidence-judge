"""A screenshot shows what the agent typed and the site's echo of it, and pixels cannot be masked:
in a run with agent input, a screenshot alone proves neither a claim nor an `expect` entry."""

from __future__ import annotations

from typing import Any

from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.proof import unwitnessed_screenshot
from evidence_judge.checks.registry import default_registry
from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.evidence import Run
from evidence_judge.core.predicates import PageTextPredicate, ScreenshotPredicate
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import HOST, FakeVisionLLM, act, ctx, final, make_run, shot, snap

IN_STOCK = ScreenshotPredicate(question="Does the page say the kettle is In stock?")
ALONE_REASON = "a screenshot alone proves nothing"
SETTINGS = Settings(anthropic_api_key=None, cache_dir=None, screenshots_to_llm=True)
NO_CLAIMS = ClaimSet(claims=[])


def _run(*, typed: bool) -> Run:
    """A stock lookup reached by clicks, or by typing "In stock" into the search box, which the
    results page echoes back."""
    if typed:
        actions = [
            act(1, "goto", url=f"{HOST}/"),
            act(2, "fill", "#search", value="In stock", url=f"{HOST}/"),
            act(3, "press", "#search", value="Enter", url=f"{HOST}/"),
        ]
        text = "No products match In stock"
        path = "/search?q=In+stock"
    else:
        actions = [act(1, "goto", url=f"{HOST}/"), act(2, "click", "#red-kettle", url=f"{HOST}/")]
        text = "Red Kettle $24.00 In stock"
        path = "/p/red-kettle"
    return make_run(
        actions=actions,
        snapshots=[snap(1, "/", "Shop home"), snap(4, path, text)],
        screenshots=[shot(4)],
        final_state=final(path, text, screenshot_id="shot#4"),
    )


def _spec(*expect: dict[str, Any]) -> TaskSpec:
    return TaskSpec.model_validate(
        {"id": "stock", "task": "Is the red kettle in stock?", "site": HOST, "expect": list(expect)}
    )


async def _judge(run: Run, spec: TaskSpec, answer: str = "yes") -> Any:
    llm = FakeVisionLLM("yes" if answer == "yes" else "no")
    return await Judge(SETTINGS, llm).judge(run, JudgeOptions(spec=spec, claims=NO_CLAIMS))


SCREENSHOT_ONLY = {"id": "in_stock", "any": [IN_STOCK.model_dump()]}


async def test_screenshot_only_expectation_passes_without_agent_input() -> None:
    verdict = await _judge(_run(typed=False), _spec(SCREENSHOT_ONLY))
    assert verdict.expectations[0].met is True
    assert verdict.outcome is Outcome.PASS


async def test_screenshot_only_expectation_cannot_pass_after_the_agent_typed() -> None:
    verdict = await _judge(_run(typed=True), _spec(SCREENSHOT_ONLY))
    [result] = verdict.expectations
    assert result.met is None
    assert ALONE_REASON in result.reason
    assert "'in stock'" in result.reason
    assert [c.evidence_id for c in result.citations] == ["shot#4"]
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary.startswith("NEEDS_REVIEW: expect 'in_stock' could not be verified")


async def test_deterministic_co_witness_lets_the_screenshot_count() -> None:
    spec = _spec(
        {
            "id": "in_stock",
            "all": [IN_STOCK.model_dump(), {"kind": "url", "contains": "/search"}],
        }
    )
    verdict = await _judge(_run(typed=True), spec)
    assert verdict.expectations[0].met is True
    assert verdict.outcome is Outcome.PASS


async def test_two_screenshots_do_not_witness_each_other() -> None:
    other = ScreenshotPredicate(question="Is a green badge shown?")
    spec = _spec({"id": "in_stock", "all": [IN_STOCK.model_dump(), other.model_dump()]})
    verdict = await _judge(_run(typed=True), spec)
    assert verdict.expectations[0].met is None
    assert verdict.outcome is Outcome.NEEDS_REVIEW


async def test_a_screenshot_still_refutes_after_the_agent_typed() -> None:
    verdict = await _judge(_run(typed=True), _spec(SCREENSHOT_ONLY), answer="no")
    assert verdict.expectations[0].met is False
    assert verdict.outcome is Outcome.FAIL


def _stock_claim() -> Claim:
    return Claim(
        id="c1",
        text="the kettle is in stock",
        quote="It is In stock",
        kind=ClaimKind.STATE,
        key=True,
        predicates=[IN_STOCK],
    )


async def test_screenshot_cannot_support_a_claim_after_the_agent_typed() -> None:
    verdict = await judge_claim(
        _stock_claim(), ctx(_run(typed=True), FakeVisionLLM("yes")), default_registry()
    )
    assert verdict.status is Support.NO_EVIDENCE
    assert ALONE_REASON in verdict.reason
    assert verdict.checker == "screenshot"


async def test_screenshot_supports_a_claim_without_agent_input() -> None:
    verdict = await judge_claim(
        _stock_claim(), ctx(_run(typed=False), FakeVisionLLM("yes")), default_registry()
    )
    assert verdict.status is Support.SUPPORTED


def test_injected_code_alone_also_rules_a_screenshot_out() -> None:
    run = make_run(
        actions=[act(1, "evaluate", value="evaluate(document.body.className = 'x')")],
        screenshots=[shot(2)],
    )
    reason = unwitnessed_screenshot(IN_STOCK, ctx(run))
    assert reason is not None
    assert reason.startswith("the agent ran its own code in the page")


def test_other_predicates_and_clean_runs_are_not_screenshot_guarded() -> None:
    assert unwitnessed_screenshot(IN_STOCK, ctx(_run(typed=False))) is None
    text = PageTextPredicate(contains="In stock")
    assert unwitnessed_screenshot(text, ctx(_run(typed=True))) is None
