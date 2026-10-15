"""Hard USD budgets for LLM calls: one per judged run, plus an optional cap on a client."""

from __future__ import annotations

import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from evidence_judge.llm.base import BudgetExceededError

logger = logging.getLogger(__name__)

_RUN_BUDGET: ContextVar[Budget | None] = ContextVar("evidence_judge_run_budget", default=None)


class Budget:
    """Tracks spend against `max_cost_usd`.

    Callers `reserve` a worst-case estimate before a request and `settle` the reservation with
    the actual cost afterwards, so concurrent requests cannot jointly overshoot the cap.
    Methods never await, so they are atomic under asyncio; a lock covers threaded use.
    """

    def __init__(self, max_cost_usd: float) -> None:
        if max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive")
        self.max_cost_usd = max_cost_usd
        self._spent = 0.0
        self._reserved = 0.0
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        return self._spent

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_cost_usd - self._spent - self._reserved)

    def reserve(self, estimate_usd: float) -> float:
        estimate = max(0.0, estimate_usd)
        with self._lock:
            projected = self._spent + self._reserved + estimate
            if projected > self.max_cost_usd:
                raise BudgetExceededError(
                    f"LLM budget would be exceeded: spent ${self._spent:.4f}, "
                    f"in flight ${self._reserved:.4f}, next call up to ${estimate:.4f}, "
                    f"cap ${self.max_cost_usd:.4f}"
                )
            self._reserved += estimate
        return estimate

    def settle(self, reserved_usd: float, actual_usd: float) -> None:
        """Record a finished call's cost. A call that cost more than the cap allows is already
        paid for, so its result is kept: the overspend is logged and the next `reserve` fails."""
        with self._lock:
            self._reserved = max(0.0, self._reserved - reserved_usd)
            self._spent += max(0.0, actual_usd)
            spent = self._spent
        if spent > self.max_cost_usd:
            logger.warning(
                "LLM budget exceeded: spent $%.4f of $%.4f; further calls will be refused",
                spent,
                self.max_cost_usd,
            )

    def release(self, reserved_usd: float) -> None:
        with self._lock:
            self._reserved = max(0.0, self._reserved - reserved_usd)


class Reservation:
    """One call's hold on every budget that applies to it: the run's (see `run_budget`) and the
    client's own, if set. With neither, the call is capped by `fallback_cap_usd` alone.

    Settle it with the call's actual cost, or release it if the call failed before any cost
    was known. Either is final; a second settle or release is ignored.
    """

    def __init__(
        self, estimate_usd: float, *, client_budget: Budget | None, fallback_cap_usd: float
    ) -> None:
        budgets = [b for b in (current_budget(), client_budget) if b is not None]
        if not budgets:
            budgets = [Budget(fallback_cap_usd)]
        elif len(budgets) == 2 and budgets[0] is budgets[1]:
            budgets = budgets[:1]
        held: list[tuple[Budget, float]] = []
        try:
            for budget in budgets:
                held.append((budget, budget.reserve(estimate_usd)))
        except BaseException:
            for budget, reserved in held:
                budget.release(reserved)
            raise
        self._held = held
        self._open = True

    @property
    def remaining_usd(self) -> float:
        """What the tightest budget allows this call to spend: its own hold plus the headroom."""
        return min(budget.remaining_usd + reserved for budget, reserved in self._held)

    def settle(self, actual_usd: float) -> None:
        if self._open:
            self._open = False
            for budget, reserved in self._held:
                budget.settle(reserved, actual_usd)

    def release(self) -> None:
        if self._open:
            self._open = False
            for budget, reserved in self._held:
                budget.release(reserved)


def current_budget() -> Budget | None:
    """The budget of the run being judged in this context, if any."""
    return _RUN_BUDGET.get()


@contextmanager
def run_budget(budget: Budget) -> Generator[Budget]:
    """Charge every LLM call made in this context (and tasks started from it) to `budget`.

    `Judge.judge` opens one per run, so a client reused across runs, or by concurrent runs,
    caps each run separately.
    """
    token = _RUN_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _RUN_BUDGET.reset(token)
