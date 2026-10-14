import os

import pytest
from pydantic import BaseModel

from evidence_judge.config import Settings
from evidence_judge.llm.anthropic_client import AnthropicClient
from evidence_judge.llm.base import LLMRequest
from evidence_judge.llm.budget import Budget

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"),
]


class Capital(BaseModel):
    country: str
    capital: str


async def test_live_structured_output_round_trip():
    settings = Settings()
    budget = Budget(0.05)
    client = AnthropicClient(settings, budget=budget)
    result, usage = await client.generate(
        LLMRequest(
            system="You answer geography questions.",
            prompt="What is the capital of France?",
            max_output_tokens=1024,
        ),
        Capital,
    )
    assert result.capital.lower() == "paris"
    assert usage.llm_calls >= 1
    assert usage.cost_usd > 0
    assert budget.spent_usd == pytest.approx(usage.cost_usd)
