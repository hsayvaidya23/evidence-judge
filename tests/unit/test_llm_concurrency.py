from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import LLMError, LLMRequest
from evidence_judge.llm.concurrency import ConcurrencyLimitedClient


class Echo(BaseModel):
    value: str


class SlowLLM:
    name = "slow"

    def __init__(self, fail: bool = False) -> None:
        self.active = 0
        self.peak = 0
        self.fail = fail

    async def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            if self.fail:
                raise LLMError("boom")
            return schema.model_validate({"value": request.prompt}), Usage(llm_calls=1)
        finally:
            self.active -= 1


async def test_limits_in_flight_calls() -> None:
    inner = SlowLLM()
    client = ConcurrencyLimitedClient(inner, max_concurrent=2)
    results = await asyncio.gather(
        *(client.generate(LLMRequest(system="s", prompt=str(i)), Echo) for i in range(10))
    )
    assert [r.value for r, _ in results] == [str(i) for i in range(10)]
    assert inner.peak == 2
    assert client.name == "slow"
    assert client.inner is inner


async def test_errors_release_the_slot() -> None:
    client = ConcurrencyLimitedClient(SlowLLM(fail=True), max_concurrent=1)
    for _ in range(3):
        with pytest.raises(LLMError):
            await client.generate(LLMRequest(system="s", prompt="p"), Echo)


def test_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        ConcurrencyLimitedClient(SlowLLM(), max_concurrent=0)
