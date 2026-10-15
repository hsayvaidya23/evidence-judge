"""Page text, URLs and page values that only echo the agent's own input prove nothing.

Each run below shows the claimed text or value only where the site repeats what the agent typed
or put in a URL; with that input masked out, the predicate no longer holds.
"""

from __future__ import annotations

import pytest

from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.registry import default_registry
from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.evidence import Channel, Run
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    UrlPredicate,
    ValuePredicate,
)
from evidence_judge.core.verdict import ClaimVerdict, Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import (
    CONFIRMATION_TEXT,
    HOST,
    act,
    ctx,
    final,
    honest_run,
    make_run,
    net,
    shop_spec,
    snap,
)

ORDER_TEXT = PageTextPredicate(contains="Order A77 confirmed")
SEARCH = "/search?q=Order+A77+confirmed"


def _claim(predicate: Predicate, kind: ClaimKind = ClaimKind.OUTCOME) -> Claim:
    return Claim(
        id="c1",
        text="Order placed",
        quote="Order placed",
        kind=kind,
        key=True,
        predicates=[predicate],
    )


async def _judge(run: Run, predicate: Predicate) -> ClaimVerdict:
    return await judge_claim(_claim(predicate), ctx(run), default_registry())


def _goto_echo() -> Run:
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "click", "#search"),
            act(3, "goto", url=f"{HOST}{SEARCH}"),
        ],
        network=[net(1, "GET", "/"), net(3, "GET", SEARCH)],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final(SEARCH, "Results for Order A77 confirmed: 0 items"),
    )


def _har_echo() -> Run:
    run = make_run(
        network=[net(1, "GET", "/"), net(2, "GET", SEARCH)],
        final_state=final(SEARCH, "Results for Order A77 confirmed: 0 items"),
        captured=frozenset({Channel.NETWORK, Channel.FINAL_STATE}),
    )
    network = [n.model_copy(update={"resource_type": "document"}) for n in run.network]
    return run.model_copy(update={"network": network})


def _partial_echo() -> Run:
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "fill", "#q", value="A77 confirmed"),
            act(3, "press", "#q", value="Enter"),
        ],
        network=[net(1, "GET", "/"), net(3, "GET", "/search?q=A77+confirmed")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/search?q=A77+confirmed", "No results for Order A77 confirmed"),
    )


def _per_key_echo() -> Run:
    keys = [act(2 + i, "press", "#q", value=c) for i, c in enumerate("Order A77 confirmed")]
    return make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), *keys],
        network=[net(1, "GET", "/")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/", "Search: Order A77 confirmed"),
    )


@pytest.mark.parametrize(
    "run",
    [_goto_echo(), _har_echo(), _partial_echo(), _per_key_echo()],
    ids=["goto-url", "har-only", "partial", "per-key-press"],
)
async def test_page_text_echoing_agent_input_proves_nothing(run: Run) -> None:
    verdict = await _judge(run, ORDER_TEXT)
    assert verdict.status is Support.NO_EVIDENCE
    assert "echoes the agent's own input" in verdict.reason
    assert "'order a77 confirmed'" in verdict.reason or "'a77 confirmed'" in verdict.reason


async def test_url_query_echoing_a_submitted_form_proves_nothing() -> None:
    run = make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/", value=f"{HOST}/"),
            act(2, "fill", "#q", value="payment=success"),
            act(3, "press", "#q", value="Enter"),
        ],
        network=[net(1, "GET", "/"), net(3, "GET", "/search?q=payment%3Dsuccess")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/search?q=payment%3Dsuccess", "No results"),
    )
    verdict = await _judge(run, UrlPredicate(query_contains="payment=success"))
    assert verdict.status is Support.NO_EVIDENCE
    assert "'payment=success'" in verdict.reason


async def test_url_path_the_agent_opened_proves_nothing() -> None:
    run = make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/"),
            act(2, "click", "#menu"),
            act(3, "goto", value=f"{HOST}/orders/A77-confirmed"),
        ],
        network=[net(1, "GET", "/"), net(3, "GET", "/orders/A77-confirmed", 404)],
        final_state=final("/orders/A77-confirmed", "Page not found"),
    )
    verdict = await _judge(run, UrlPredicate(contains="/orders/A77-confirmed"))
    assert verdict.status is Support.NO_EVIDENCE
    assert "echoes the agent's own input" in verdict.reason


async def test_page_value_read_from_an_echoed_field_proves_nothing() -> None:
    run = make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "fill", "#q", value="Refund total: $500")],
        network=[net(1, "GET", "/")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/search", "No results for Refund total: $500"),
    )
    predicate = ValuePredicate(
        field="refund", page_regex=r"Refund total:\s*\$(\d+)", op="==", value=500
    )
    verdict = await judge_claim(_claim(predicate, ClaimKind.VALUE), ctx(run), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert "echoes the agent's own input" in verdict.reason


async def test_value_from_a_request_body_is_not_an_echo() -> None:
    run = honest_run()
    run = run.model_copy(
        update={"actions": [*run.actions, act(9, "fill", "#note", value="total $24")]}
    )
    predicate = ValuePredicate(
        field="price",
        json_path="items.0.price",
        page_regex=r"total \$(\d+)",
        op="==",
        value=24,
    )
    verdict = await judge_claim(_claim(predicate, ClaimKind.VALUE), ctx(run), default_registry())
    assert verdict.status is Support.SUPPORTED


@pytest.mark.parametrize(
    "predicate",
    [
        PageTextPredicate(contains="Order A1 for Red"),
        UrlPredicate(contains="/order/A1"),
        ValuePredicate(field="total", page_regex=r"total \$([\d.]+)", op="<=", value=30),
    ],
)
async def test_genuine_confirmation_after_a_search_still_proves_the_claim(
    predicate: Predicate,
) -> None:
    verdict = await judge_claim(
        _claim(predicate, ClaimKind.STATE), ctx(_searched()), default_registry()
    )
    assert verdict.status is Support.SUPPORTED


async def test_page_text_containing_any_agent_input_cannot_prove_a_claim_alone() -> None:
    claim = _claim(PageTextPredicate(contains="Order A1 for Red Kettle"), ClaimKind.STATE)
    verdict = await judge_claim(claim, ctx(_searched()), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert "'kettle'" in verdict.reason


def _searched() -> Run:
    """The honest purchase, after the agent first searched the shop for 'kettle'."""
    run = honest_run()
    search = [
        act(90, "fill", "#search", value="kettle"),
        act(91, "press", "#search", value="Enter"),
    ]
    return run.model_copy(
        update={
            "actions": [*search, *run.actions],
            "network": [net(90, "GET", "/search?q=kettle"), *run.network],
        }
    )


async def test_echo_never_turns_a_contradiction_into_anything_else() -> None:
    verdict = await _judge(_partial_echo(), PageTextPredicate(contains="Thank you for your order"))
    assert verdict.status is Support.CONTRADICTED


@pytest.mark.parametrize("run", [_goto_echo(), _partial_echo()], ids=["goto-url", "partial"])
async def test_echoed_key_claim_sends_the_run_to_review(run: Run) -> None:
    claims = ClaimSet(claims=[_claim(ORDER_TEXT)])
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, claims=claims)
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW


async def test_genuine_confirmation_page_passes() -> None:
    claims = ClaimSet(claims=[_claim(PageTextPredicate(contains=CONFIRMATION_TEXT[:24]))])
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        honest_run(), JudgeOptions(offline=True, claims=claims, spec=shop_spec())
    )
    assert verdict.outcome is Outcome.PASS
