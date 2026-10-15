"""`expect` entries are proven like claims: no echo of the agent's input, no baseline tautology."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from evidence_judge.checks.expectations import EXPECT, FORBID, evaluate_expectation
from evidence_judge.checks.registry import default_registry
from evidence_judge.cli import ExitCode, app
from evidence_judge.config import Settings
from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.evidence import Run
from evidence_judge.core.predicates import PageTextPredicate
from evidence_judge.core.taskspec import Expectation, TaskSpec
from evidence_judge.core.verdict import ExpectationResult, Outcome, RunVerdict
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import HOST, act, ctx, final, honest_run, make_run, net, shop_spec, snap

ECHO = "it holds only where the page or URL echoes the agent's own input"
BASELINE = "it was already true before the agent acted"


def _spec(phrase: str, kind: str = "page_text") -> TaskSpec:
    predicate = {"kind": kind, "contains": phrase}
    return TaskSpec.model_validate(
        {"id": "t", "task": "Check order A77", "expect": [{"id": "seen", "any": [predicate]}]}
    )


def _search_echo() -> Run:
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "fill", "#q", value="Order A77 shipped"),
            act(3, "press", "#q", value="Enter"),
        ],
        network=[net(1, "GET", "/"), net(4, "GET", "/search?q=Order+A77+shipped")],
        snapshots=[snap(1, "/", "Track your order")],
        final_state=final("/search?q=Order+A77+shipped", "No results for “Order A77 shipped”"),
    )


def _typed_echo() -> Run:
    return make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "fill", "#q", value="Order A77 confirmed")],
        network=[net(1, "GET", "/"), net(5, "GET", "/profile")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/profile", "You wrote Order A77 confirmed"),
    )


def _already_shown() -> Run:
    return make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "click", "#x")],
        network=[net(1, "GET", "/")],
        snapshots=[snap(1, "/", "Order A77 confirmed")],
        final_state=final("/", "Order A77 confirmed"),
    )


async def _judge(run: Run, spec: TaskSpec, answer: str | None = None) -> RunVerdict:
    return await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run.model_copy(update={"agent_answer": answer}),
        JudgeOptions(offline=True, spec=spec, claims=ClaimSet(claims=[])),
    )


async def _expect(run: Run, expectation: Expectation, kind: str = EXPECT) -> ExpectationResult:
    return await evaluate_expectation(expectation, kind, ctx(run), default_registry())


async def test_expect_met_only_by_a_search_echo_is_unknown() -> None:
    verdict = await _judge(_search_echo(), _spec("Order A77 shipped"), "Done.")

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    [result] = verdict.expectations
    assert result.met is None
    assert ECHO in result.reason
    assert "'order a77 shipped'" in result.reason


async def test_expect_met_only_by_typed_text_is_unknown() -> None:
    verdict = await _judge(_typed_echo(), _spec("Order A77 confirmed"))

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.expectations[0].met is None
    assert ECHO in verdict.expectations[0].reason


def _shown_then_left() -> Run:
    """Opens a page that already shows the text, leaves it, acts elsewhere and comes back."""
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/thanks", value=f"{HOST}/thanks"),
            act(2, "goto", url=f"{HOST}/", value=f"{HOST}/"),
            act(3, "click", "#x"),
            act(4, "goto", url=f"{HOST}/thanks", value=f"{HOST}/thanks"),
        ],
        network=[net(1, "GET", "/thanks"), net(2, "GET", "/"), net(4, "GET", "/thanks")],
        snapshots=[snap(1, "/thanks", "Thank you for your message"), snap(2, "/", "Welcome")],
        final_state=final("/thanks", "Thank you for your message"),
    )


async def test_expect_met_on_a_page_shown_before_the_agent_acted_is_unknown() -> None:
    spec = _spec("Thank you for your message").model_copy(update={"site": "shop.test"})
    verdict = await _judge(_shown_then_left(), spec, "Done.")

    assert verdict.outcome is Outcome.NEEDS_REVIEW, verdict.summary
    assert verdict.expectations[0].met is None
    assert BASELINE in verdict.expectations[0].reason


async def test_expect_met_before_the_agent_acted_is_unknown() -> None:
    verdict = await _judge(_already_shown(), _spec("Order A77 confirmed"))

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.expectations[0].met is None
    assert BASELINE in verdict.expectations[0].reason


async def test_expect_url_the_agent_opened_itself_is_unknown() -> None:
    run = make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "goto", url=f"{HOST}/order/A77/confirmed")],
        network=[net(1, "GET", "/"), net(3, "GET", "/order/A77/confirmed", 404)],
        final_state=final("/order/A77/confirmed", "Not found"),
    )
    verdict = await _judge(run, _spec("/order/A77/confirmed", kind="url"))

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.expectations[0].met is None
    assert BASELINE in verdict.expectations[0].reason


def test_browser_use_agent_that_opens_the_expected_url_itself_needs_review(
    tmp_path: Path,
) -> None:
    steps = [
        ("https://shop.test/", "about:blank"),
        ("https://shop.test/order/A77/confirmed", "https://shop.test/"),
    ]
    history = [
        {
            "model_output": {"action": [{"go_to_url": {"url": url}}]},
            "result": [{}],
            "state": {"url": before},
            "metadata": {"step_start_time": 1000 + 2 * i},
        }
        for i, (url, before) in enumerate(steps)
    ]
    history.append(
        {
            "model_output": {"action": [{"done": {"text": "Done.", "success": True}}]},
            "result": [{"is_done": True, "extracted_content": "Done."}],
            "state": {"url": "https://shop.test/order/A77/confirmed", "title": "Not found"},
            "metadata": {"step_start_time": 1004},
        }
    )
    (tmp_path / "history.json").write_text(json.dumps({"history": history}))
    (tmp_path / "spec.yaml").write_text(
        "id: confirm-a77\ntask: Confirm order A77\nexpect:\n  - id: on_confirmation_page\n"
        "    any: [{kind: url, contains: /order/A77/confirmed}]\n"
    )

    result = CliRunner().invoke(
        app,
        [
            "judge",
            "--browser-use",
            str(tmp_path / "history.json"),
            "--spec",
            str(tmp_path / "spec.yaml"),
            "--offline",
            "--quiet",
        ],
    )

    assert result.exit_code == ExitCode.NEEDS_REVIEW, result.output


async def test_genuine_expectations_still_pass() -> None:
    verdict = await _judge(honest_run(), shop_spec())

    assert verdict.outcome is Outcome.PASS
    assert all(e.met is True for e in verdict.expectations)


async def test_forbid_still_breaks_on_text_that_echoes_the_agent_input() -> None:
    forbid = Expectation(id="no_echo", any=[PageTextPredicate(contains="Order A77 shipped")])

    result = await _expect(_search_echo(), forbid, FORBID)

    assert result.met is False


async def test_forbid_still_breaks_on_what_was_true_before_the_agent_acted() -> None:
    forbid = Expectation(id="no_text", any=[PageTextPredicate(contains="Order A77 confirmed")])

    result = await _expect(_already_shown(), forbid, FORBID)

    assert result.met is False


async def test_contradicted_expectation_still_fails() -> None:
    verdict = await _judge(_search_echo(), _spec("Your order has shipped"))

    assert verdict.outcome is Outcome.FAIL
    assert verdict.expectations[0].met is False
