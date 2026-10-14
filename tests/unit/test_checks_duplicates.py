"""Repeated mutations: an expectation explains only as many mutations as it needs."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from evidence_judge.checks.dimensions import assess_side_effects
from evidence_judge.checks.expectations import evaluate_spec
from evidence_judge.checks.registry import default_registry
from evidence_judge.config import Settings
from evidence_judge.core.evidence import NetworkEvent, Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import ctx, honest_run, net, shop_spec

ORDER_BODY = {"items": [{"sku": "K-1", "price": 24.0}]}


def _orders(count: int, *, status: int = 201) -> Run:
    run = honest_run()
    extra = [
        net(10 + n, "POST", "/api/orders", status, body=ORDER_BODY, response={"order_id": "A1"})
        for n in range(count - 1)
    ]
    return run.model_copy(update={"network": [*run.network, *extra]})


def _carts(count: int) -> list[NetworkEvent]:
    return [net(20 + n, "POST", "/api/cart", body={"sku": "K-1"}) for n in range(count)]


async def _side_effects(run: Run, spec: TaskSpec) -> list[str]:
    context = ctx(run)
    expectations = await evaluate_spec(spec, context, default_registry())
    return assess_side_effects(context.index, [], expectations, spec).findings


async def _judge(run: Run, spec: TaskSpec) -> Outcome:
    run = run.model_copy(update={"agent_answer": "I placed the order."})
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=spec)
    )
    return verdict.outcome


def _spec(**changes: Any) -> TaskSpec:
    data = shop_spec().model_dump(mode="json")
    expect = data["expect"][0]
    expect.update(changes.pop("expect", {}))
    data.update(changes)
    return TaskSpec.model_validate(data)


async def test_single_order_is_explained() -> None:
    assert await _side_effects(_orders(1), shop_spec()) == []


async def test_repeated_orders_beyond_the_expectation_are_side_effects() -> None:
    findings = await _side_effects(_orders(4), shop_spec())
    assert len(findings) == 1
    assert "duplicate POST /api/orders → 201, sent 4 times" in findings[0]
    assert "'order_submitted' explains 1" in findings[0]
    assert "net#10, net#11, net#12" in findings[0]


async def test_four_identical_orders_need_review() -> None:
    assert await _judge(_orders(1), shop_spec()) is Outcome.PASS
    assert await _judge(_orders(4), shop_spec()) is Outcome.NEEDS_REVIEW


async def test_failed_retries_are_not_duplicates() -> None:
    run = honest_run()
    retries = [net(10, "POST", "/api/orders", 500, body=ORDER_BODY)]
    run = run.model_copy(update={"network": [*retries, *run.network]})
    assert await _side_effects(run, shop_spec()) == []


async def test_max_count_raises_the_budget() -> None:
    assert await _side_effects(_orders(2), _spec(expect={"max_count": 2})) == []


async def test_explicit_max_count_breaks_the_expectation_when_exceeded() -> None:
    spec = _spec(expect={"max_count": 1})
    assert await _judge(_orders(1), spec) is Outcome.PASS
    context = ctx(_orders(3))
    results = await evaluate_spec(spec, context, default_registry())
    order = next(r for r in results if r.expectation_id == "order_submitted")
    assert order.met is False
    assert "3 matching mutations" in order.reason
    assert "at most 1" in order.reason
    assert await _judge(_orders(3), spec) is Outcome.FAIL


async def test_allow_entries_are_unlimited_by_default() -> None:
    run = honest_run()
    run = run.model_copy(update={"network": [*run.network, *_carts(5)]})
    assert await _side_effects(run, shop_spec()) == []


async def test_allow_entry_max_count_flags_the_excess() -> None:
    run = honest_run()
    run = run.model_copy(update={"network": [*run.network, *_carts(3)]})
    allow = [{"kind": "request", "method": "POST", "url_contains": ["/api/cart"], "max_count": 2}]
    findings = await _side_effects(run, _spec(allow=allow))
    assert len(findings) == 1
    assert "duplicate POST /api/cart → 200, sent 4 times" in findings[0]
    assert "allow entry" in findings[0]


@pytest.mark.parametrize("value", [0, -1])
def test_max_count_must_be_positive(value: int) -> None:
    with pytest.raises(ValidationError, match="max_count"):
        _spec(expect={"max_count": value})
