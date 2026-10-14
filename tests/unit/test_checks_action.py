from __future__ import annotations

import pytest
from pydantic import ValidationError

from evidence_judge.checks.action import ActionChecker, canonical_action
from evidence_judge.core.evidence import Channel
from evidence_judge.core.predicates import ActionPredicate
from evidence_judge.core.verdict import Support
from tests.unit.conftest import ALL_CHANNELS, act, ctx, forged_run, honest_run, make_run

PLACE_ORDER = ActionPredicate(name="click", target_contains="place-order")


async def test_performed_action_is_supported() -> None:
    res = await ActionChecker().evaluate(PLACE_ORDER, ctx(honest_run()))
    assert res.status is Support.SUPPORTED
    assert [c.evidence_id for c in res.citations] == ["act#7"]
    assert res.citations[0].excerpt == "click button#place-order"


async def test_missing_action_is_contradicted_with_counts() -> None:
    res = await ActionChecker().evaluate(PLACE_ORDER, ctx(forged_run()))
    assert res.status is Support.CONTRADICTED
    assert res.reason == (
        "No click action whose target contains 'place-order' among 4 recorded actions "
        "(2 click actions)"
    )


async def test_failed_attempt_is_contradicted_with_error() -> None:
    run = make_run(actions=[act(1, "click", "#place-order", error="Timeout 30000ms exceeded")])
    res = await ActionChecker().evaluate(PLACE_ORDER, ctx(run))
    assert res.status is Support.CONTRADICTED
    assert res.confidence == pytest.approx(0.9)
    assert "attempted 1 time but failed: Timeout 30000ms exceeded" in res.reason


async def test_uncaptured_actions_are_no_evidence() -> None:
    run = make_run(captured=ALL_CHANNELS - {Channel.ACTIONS})
    res = await ActionChecker().evaluate(PLACE_ORDER, ctx(run))
    assert res.status is Support.NO_EVIDENCE


async def test_value_and_alias_matching() -> None:
    pred = ActionPredicate(name="type", target_contains="card", value_contains="4242")
    assert (await ActionChecker().evaluate(pred, ctx(honest_run()))).status is Support.SUPPORTED
    goto = ActionPredicate(name="navigate", target_contains="/cart")
    res = await ActionChecker().evaluate(goto, ctx(honest_run()))
    assert res.status is Support.SUPPORTED
    assert res.citations[0].excerpt == "goto /cart"


def test_canonical_action() -> None:
    assert canonical_action(" Press Key ") == "press"
    assert canonical_action("CLICK") == "click"


def test_action_predicate_needs_a_criterion() -> None:
    with pytest.raises(ValidationError, match="action predicate needs"):
        ActionPredicate()
