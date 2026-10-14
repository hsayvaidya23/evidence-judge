from __future__ import annotations

import pytest

from evidence_judge.checks.expectations import (
    all_of,
    any_of,
    evaluate_expectation,
    evaluate_spec,
)
from evidence_judge.checks.registry import default_registry
from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import Expectation
from evidence_judge.core.verdict import ExpectationResult
from tests.unit.conftest import (
    constraint_violation_run,
    ctx,
    forged_run,
    honest_run,
    shop_spec,
    side_effect_run,
    uncaptured_network_run,
)


async def judge(run: Run) -> dict[str, ExpectationResult]:
    results = await evaluate_spec(shop_spec(), ctx(run), default_registry())
    return {r.expectation_id: r for r in results}


async def test_honest_run_meets_everything() -> None:
    results = await judge(honest_run())
    assert {k: r.met for k, r in results.items()} == {
        "order_submitted": True,
        "confirmation_shown": True,
        "no_account_changes": True,
        "price <= 30": True,
    }
    assert [r.kind for r in results.values()] == ["expect", "expect", "forbid", "constraint"]
    assert results["order_submitted"].citations[0].evidence_id == "net#6"


async def test_forged_run_misses_expectations() -> None:
    results = await judge(forged_run())
    assert results["order_submitted"].met is False
    assert "closest was POST /api/cart" in results["order_submitted"].reason
    assert results["confirmation_shown"].met is False
    assert results["no_account_changes"].met is True
    assert results["price <= 30"].met is None


async def test_constraint_violation() -> None:
    results = await judge(constraint_violation_run())
    assert results["order_submitted"].met is True
    assert results["price <= 30"].met is False
    assert "price was 45.0" in results["price <= 30"].reason


async def test_side_effect_breaks_forbid() -> None:
    results = await judge(side_effect_run())
    forbid = results["no_account_changes"]
    assert forbid.met is False
    assert forbid.reason.startswith("POST /api/account/email → 200 matched")
    assert forbid.citations[0].evidence_id == "net#8"


async def test_uncaptured_network_is_unknown_not_failed() -> None:
    results = await judge(uncaptured_network_run())
    assert results["order_submitted"].met is None
    assert results["no_account_changes"].met is None
    assert results["price <= 30"].met is None
    assert results["confirmation_shown"].met is False


async def test_forbid_with_all_requires_every_predicate() -> None:
    forbid = Expectation.model_validate(
        {
            "id": "cart_then_account",
            "all": [
                {"kind": "request", "method": "POST", "url_contains": ["/api/cart"]},
                {"kind": "request", "method": "POST", "url_contains": ["/api/account"]},
            ],
        }
    )
    res = await evaluate_expectation(forbid, "forbid", ctx(honest_run()), default_registry())
    assert res.met is True
    res = await evaluate_expectation(forbid, "forbid", ctx(side_effect_run()), default_registry())
    assert res.met is False


async def test_any_needs_one_supported() -> None:
    expectation = Expectation.model_validate(
        {
            "id": "confirmed",
            "any": [
                {"kind": "page_text", "contains": "Order shipped"},
                {"kind": "url", "contains": "/order/A1"},
            ],
        }
    )
    res = await evaluate_expectation(expectation, "expect", ctx(honest_run()), default_registry())
    assert res.met is True
    assert [c.evidence_id for c in res.citations] == ["final"]


@pytest.mark.parametrize(
    ("values", "expected_all", "expected_any"),
    [
        ([True, True], True, True),
        ([True, None], None, True),
        ([True, False], False, True),
        ([None, None], None, None),
        ([False, None], False, None),
        ([False, False], False, False),
    ],
)
def test_kleene_logic(
    values: list[bool | None], expected_all: bool | None, expected_any: bool | None
) -> None:
    assert all_of(values) is expected_all
    assert any_of(values) is expected_any
