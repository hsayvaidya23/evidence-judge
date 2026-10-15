import asyncio

import pytest

from evidence_judge.llm.base import BudgetExceededError
from evidence_judge.llm.budget import Budget, Reservation, current_budget, run_budget
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


def test_settle_records_overspend_without_raising_and_blocks_the_next_call(
    caplog: pytest.LogCaptureFixture,
):
    budget = Budget(0.1)
    reserved = budget.reserve(0.05)
    budget.settle(reserved, 0.2)
    assert budget.spent_usd == pytest.approx(0.2)
    assert budget.remaining_usd == 0.0
    assert "exceeded" in caplog.text
    with pytest.raises(BudgetExceededError):
        budget.reserve(0.0)


def test_run_budget_is_scoped_to_the_context():
    outer = Budget(1.0)
    assert current_budget() is None
    with run_budget(outer):
        assert current_budget() is outer
    assert current_budget() is None


async def test_concurrent_tasks_see_their_own_run_budget():
    async def task(cap: float) -> float:
        with run_budget(Budget(cap)):
            await asyncio.sleep(0)
            budget = current_budget()
            assert budget is not None
            return budget.max_cost_usd

    assert await asyncio.gather(task(1.0), task(2.0)) == [1.0, 2.0]


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


def test_reservation_holds_the_run_and_client_budgets_and_settles_both():
    run, client = Budget(1.0), Budget(0.5)
    with run_budget(run):
        reservation = Reservation(0.2, client_budget=client, fallback_cap_usd=9.0)
    assert run.remaining_usd == pytest.approx(0.8)
    assert client.remaining_usd == pytest.approx(0.3)
    assert reservation.remaining_usd == pytest.approx(0.5)
    reservation.settle(0.1)
    reservation.settle(0.1)
    reservation.release()
    assert run.spent_usd == pytest.approx(0.1)
    assert client.spent_usd == pytest.approx(0.1)
    assert client.remaining_usd == pytest.approx(0.4)


def test_reservation_counts_a_shared_budget_once():
    budget = Budget(1.0)
    with run_budget(budget):
        Reservation(0.3, client_budget=budget, fallback_cap_usd=9.0).settle(0.3)
    assert budget.spent_usd == pytest.approx(0.3)


def test_reservation_without_budgets_is_capped_by_the_fallback():
    assert current_budget() is None
    assert Reservation(0.1, client_budget=None, fallback_cap_usd=0.25).remaining_usd == (
        pytest.approx(0.25)
    )
    with pytest.raises(BudgetExceededError):
        Reservation(0.3, client_budget=None, fallback_cap_usd=0.25)


def test_a_refused_reservation_releases_what_it_already_held():
    run, client = Budget(1.0), Budget(0.1)
    with run_budget(run), pytest.raises(BudgetExceededError):
        Reservation(0.2, client_budget=client, fallback_cap_usd=9.0)
    assert run.remaining_usd == pytest.approx(1.0)


def test_a_released_reservation_spends_nothing():
    budget = Budget(1.0)
    with run_budget(budget):
        reservation = Reservation(0.4, client_budget=None, fallback_cap_usd=9.0)
    reservation.release()
    reservation.settle(0.4)
    assert budget.spent_usd == 0
    assert budget.remaining_usd == pytest.approx(1.0)
