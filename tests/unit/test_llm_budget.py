import pytest

from evidence_judge.llm.base import BudgetExceededError
from evidence_judge.llm.budget import Budget
from evidence_judge.llm.pricing import DEFAULT_PRICE, DEFAULT_PRICES, ModelPrice, price_for


def test_reserve_and_settle_tracks_spend():
    budget = Budget(1.0)
    reserved = budget.reserve(0.4)
    assert budget.remaining_usd == pytest.approx(0.6)
    budget.settle(reserved, 0.1)
    assert budget.spent_usd == pytest.approx(0.1)
    assert budget.remaining_usd == pytest.approx(0.9)


def test_reserve_refuses_when_estimate_overshoots():
    budget = Budget(0.5)
    budget.reserve(0.3)
    with pytest.raises(BudgetExceededError):
        budget.reserve(0.3)


def test_settle_raises_when_actual_exceeds_cap():
    budget = Budget(0.1)
    reserved = budget.reserve(0.05)
    with pytest.raises(BudgetExceededError):
        budget.settle(reserved, 0.2)
    assert budget.spent_usd == pytest.approx(0.2)


def test_release_frees_reservation():
    budget = Budget(0.1)
    reserved = budget.reserve(0.1)
    budget.release(reserved)
    assert budget.remaining_usd == pytest.approx(0.1)


def test_budget_rejects_non_positive_cap():
    with pytest.raises(ValueError):
        Budget(0)


def test_price_cost_includes_cache_tokens():
    price = ModelPrice(input=2.0, output=10.0, cache_write=2.5, cache_read=0.2)
    cost = price.cost(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_write_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert cost == pytest.approx(2.0 + 1.0 + 2.5 + 0.2)


def test_long_prompt_multiplier_applies_above_threshold():
    haiku = DEFAULT_PRICES["claude-haiku-5-5"]
    short = haiku.cost(input_tokens=100_000, output_tokens=0)
    long = haiku.cost(input_tokens=100_001, output_tokens=0)
    assert short == pytest.approx(0.01)
    assert long == pytest.approx(100_001 * 0.5 / 1_000_000)


def test_unknown_model_falls_back_to_most_expensive_default():
    assert price_for("some-future-model") is DEFAULT_PRICE
    assert DEFAULT_PRICE.input >= max(p.input for p in DEFAULT_PRICES.values())
