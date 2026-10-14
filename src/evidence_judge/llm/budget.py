"""A shared, hard USD budget for every LLM call made while judging one run."""

from __future__ import annotations

import threading

from evidence_judge.llm.base import BudgetExceededError


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
        with self._lock:
            self._reserved = max(0.0, self._reserved - reserved_usd)
            self._spent += max(0.0, actual_usd)
            spent = self._spent
        if spent > self.max_cost_usd:
            raise BudgetExceededError(
                f"LLM budget exceeded: spent ${spent:.4f} of ${self.max_cost_usd:.4f}"
            )

    def release(self, reserved_usd: float) -> None:
        with self._lock:
            self._reserved = max(0.0, self._reserved - reserved_usd)
