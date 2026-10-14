"""A claim predicate the run already met before the agent acted, or that only echoes what the
agent typed, proves nothing; a key outcome claim needs a mutation or the final page."""

from __future__ import annotations

import pytest

from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.registry import default_registry
from evidence_judge.core.claims import Claim, ClaimKind
from evidence_judge.core.evidence import PageState, Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import (
    ActionPredicate,
    PageTextPredicate,
    Predicate,
    RequestPredicate,
    Scope,
    UrlPredicate,
)
from evidence_judge.core.verdict import Support
from tests.unit.conftest import HOST, act, ctx, final, honest_run, make_run, net, snap


def _null_run() -> Run:
    """The agent only opened the shop."""
    return make_run(
        actions=[act(1, "goto", url="about:blank", value=f"{HOST}/")],
        network=[net(1, "GET", "/")],
        snapshots=[snap(1, "/", "Kettles Red Kettle $24.00")],
        final_state=final("/", "Kettles Red Kettle $24.00"),
    )


def _claim(*predicates: Predicate, kind: ClaimKind = ClaimKind.OUTCOME, key: bool = True) -> Claim:
    return Claim(
        id="c1",
        text="Order 1042 is confirmed",
        quote="Order 1042 is confirmed",
        kind=kind,
        key=key,
        predicates=list(predicates),
    )


async def _status(run: Run, claim: Claim) -> Support:
    return (await judge_claim(claim, ctx(run), default_registry())).status


@pytest.mark.parametrize(
    "predicate",
    [
        RequestPredicate(method="GET", url_contains=["//"]),
        RequestPredicate(method="GET", url_contains=["shop.test"]),
        UrlPredicate(contains="://", scope=Scope.ANY),
        UrlPredicate(contains="shop.test"),
        ActionPredicate(name="goto", value_contains="http"),
    ],
)
@pytest.mark.parametrize("kind", [ClaimKind.STATE, ClaimKind.ACTION])
async def test_predicates_the_null_agent_meets_prove_nothing(
    predicate: Predicate, kind: ClaimKind
) -> None:
    verdict = await judge_claim(_claim(predicate, kind=kind), ctx(_null_run()), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert "before the agent acted" in verdict.reason


async def test_baseline_ignores_what_the_agent_did_after_its_first_action() -> None:
    claim = _claim(RequestPredicate(method="POST", url_contains=["/api/orders"]))
    assert await _status(honest_run(), claim) is Support.SUPPORTED


async def test_state_claim_may_cite_text_of_the_start_page() -> None:
    claim = _claim(PageTextPredicate(contains="Red Kettle $24.00"), kind=ClaimKind.STATE)
    assert await _status(_null_run(), claim) is Support.SUPPORTED


async def test_outcome_claim_cannot_cite_text_of_the_start_page() -> None:
    claim = _claim(PageTextPredicate(contains="Red Kettle $24.00"))
    assert await _status(_null_run(), claim) is Support.NO_EVIDENCE


async def test_key_outcome_claim_needs_a_mutation_or_the_final_page() -> None:
    claim = _claim(
        ActionPredicate(name="click", target_contains="place-order"),
        PageTextPredicate(contains="Thank you for your order", scope=Scope.ANY),
    )
    verdict = await judge_claim(claim, ctx(honest_run()), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert "mutation request or the final page" in verdict.reason


@pytest.mark.parametrize(
    "predicate",
    [
        RequestPredicate(method="POST", url_contains=["/api/orders"]),
        PageTextPredicate(contains="Thank you for your order"),
        UrlPredicate(contains="/order/A1"),
    ],
)
async def test_key_outcome_claim_with_a_strong_predicate_is_judged(predicate: Predicate) -> None:
    claim = _claim(ActionPredicate(name="click", target_contains="place-order"), predicate)
    assert await _status(honest_run(), claim) is Support.SUPPORTED


async def test_non_key_outcome_claim_is_not_restricted() -> None:
    claim = _claim(ActionPredicate(name="click", target_contains="place-order"), key=False)
    assert await _status(honest_run(), claim) is Support.SUPPORTED


async def test_text_the_agent_typed_itself_proves_nothing() -> None:
    run = make_run(
        actions=[
            act(1, "goto", url="about:blank", value=f"{HOST}/"),
            act(2, "fill", "#search", value="Order #1042 confirmed", url=f"{HOST}/"),
            act(3, "press", "#search", value="Enter", url=f"{HOST}/"),
        ],
        network=[net(1, "GET", "/"), net(4, "GET", "/search?q=Order+%231042+confirmed")],
        final_state=PageState(
            url=f"{HOST}/search?q=Order+%231042+confirmed",
            text="Results for Order #1042 confirmed: no products found",
        ),
    )
    claim = _claim(PageTextPredicate(contains="Order #1042 confirmed"), kind=ClaimKind.STATE)
    verdict = await judge_claim(claim, ctx(run), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert "typed" in verdict.reason


def test_baseline_index_is_cut_at_the_first_non_navigation_action() -> None:
    baseline = EvidenceIndex(honest_run()).baseline
    assert [a.name for a in baseline.run.actions] == ["goto"]
    assert [n.id for n in baseline.run.network] == ["net#1", "net#2"]
    assert baseline.final is not None
    assert baseline.final.url == f"{HOST}/"
