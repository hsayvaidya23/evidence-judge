from __future__ import annotations

import base64
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pydantic import BaseModel, SecretStr

from evidence_judge.config import Settings
from evidence_judge.llm.anthropic_client import TOOL_NAME, AnthropicClient
from evidence_judge.llm.base import BudgetExceededError, Image, LLMError, LLMRequest, Tier
from evidence_judge.llm.budget import Budget


class Verdict(BaseModel):
    label: str
    score: int


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "anthropic_api_key": SecretStr("sk-test-not-real"),
        "model_fast": "claude-haiku-5-5",
        "model_strong": "claude-sonnet-5-5",
        "cache_dir": None,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _message(
    content: list[dict[str, Any]], *, stop_reason: str = "tool_use", **usage: int
) -> Message:
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("input_tokens", 100),
                "output_tokens": usage.get("output_tokens", 20),
                "cache_creation_input_tokens": usage.get("cache_write", 0),
                "cache_read_input_tokens": usage.get("cache_read", 0),
            },
        }
    )


def _tool_use(payload: dict[str, Any], name: str = TOOL_NAME) -> dict[str, Any]:
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": payload}


class _Messages:
    def __init__(self, outcomes: list[Message | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSDK:
    def __init__(self, *outcomes: Message | Exception) -> None:
        self.messages = _Messages(list(outcomes))


def _client(sdk: FakeSDK, **kwargs: Any) -> AnthropicClient:
    return AnthropicClient(_settings(), sdk_client=sdk, **kwargs)  # type: ignore[arg-type]


async def test_returns_validated_model_and_usage_with_cache_tokens():
    sdk = FakeSDK(
        _message(
            [_tool_use({"label": "ok", "score": 3})],
            input_tokens=1_000,
            output_tokens=200,
            cache_write=500,
            cache_read=2_000,
        )
    )
    client = _client(sdk)
    result, usage = await client.generate(LLMRequest(system="sys", prompt="hi"), Verdict)
    assert result == Verdict(label="ok", score=3)
    assert usage.llm_calls == 1
    assert usage.input_tokens == 3_500
    assert usage.output_tokens == 200
    expected = (1_000 * 0.10 + 200 * 0.50 + 500 * 0.125 + 2_000 * 0.01) / 1_000_000
    assert usage.cost_usd == pytest.approx(expected)
    assert client.totals.cache_read_tokens == 2_000
    assert client.totals.cache_write_tokens == 500


async def test_request_shape_tool_schema_system_cache_and_tier_model():
    sdk = FakeSDK(_message([_tool_use({"label": "a", "score": 1})]))
    client = _client(sdk)
    image = Image(data=b"\x89PNG", mime_type="image/png")
    await client.generate(
        LLMRequest(system="judge", prompt="look", tier=Tier.STRONG, images=[image]), Verdict
    )
    call = sdk.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["tools"][0]["name"] == TOOL_NAME
    assert call["tools"][0]["input_schema"] == Verdict.model_json_schema()
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["system"][0]["text"].startswith("judge")
    assert call["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    image_block, text_block = call["messages"][0]["content"]
    assert image_block["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(b"\x89PNG").decode(),
    }
    assert text_block == {"type": "text", "text": "look"}


async def test_forced_tool_choice_for_models_that_support_it():
    sdk = FakeSDK(_message([_tool_use({"label": "a", "score": 1})]))
    client = AnthropicClient(_settings(model_fast="claude-haiku-4-5"), sdk_client=sdk)  # type: ignore[arg-type]
    await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert sdk.messages.calls[0]["tool_choice"]["type"] == "tool"
    assert sdk.messages.calls[0]["tool_choice"]["name"] == TOOL_NAME


async def test_validation_failure_retries_once_with_error_appended():
    sdk = FakeSDK(
        _message([_tool_use({"label": "a", "score": "many"})]),
        _message([_tool_use({"label": "a", "score": 2})]),
    )
    client = _client(sdk)
    result, usage = await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert result.score == 2
    assert usage.llm_calls == 2
    retry_text = sdk.messages.calls[1]["messages"][0]["content"][-1]["text"]
    assert retry_text.startswith("p")
    assert "<previous_attempt_error>" in retry_text
    assert "score" in retry_text


async def test_missing_tool_call_is_retried_then_raises():
    text_only = _message([{"type": "text", "text": "PASS"}], stop_reason="end_turn")
    sdk = FakeSDK(text_only, text_only)
    with pytest.raises(LLMError, match="after a retry"):
        await _client(sdk).generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert len(sdk.messages.calls) == 2


async def test_refusal_raises_without_retry():
    sdk = FakeSDK(_message([], stop_reason="refusal"))
    with pytest.raises(LLMError, match="declined"):
        await _client(sdk).generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert len(sdk.messages.calls) == 1


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    ("error", "match"),
    [
        (anthropic.APITimeoutError(request=_request()), "timed out"),
        (anthropic.APIConnectionError(request=_request()), "connect"),
        (
            anthropic.RateLimitError(
                "slow down",
                response=httpx2.Response(429, request=_request()),
                body=None,
            ),
            "rate limit",
        ),
        (
            anthropic.InternalServerError(
                "boom", response=httpx2.Response(500, request=_request()), body=None
            ),
            "error 500",
        ),
    ],
)
async def test_sdk_errors_map_to_llm_error_and_release_budget(error: Exception, match: str):
    budget = Budget(1.0)
    client = _client(FakeSDK(error), budget=budget)
    with pytest.raises(LLMError, match=match) as info:
        await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert "sk-test-not-real" not in str(info.value)
    assert budget.remaining_usd == pytest.approx(1.0)


async def test_budget_blocks_call_before_sending():
    sdk = FakeSDK(_message([_tool_use({"label": "a", "score": 1})]))
    client = _client(sdk, budget=Budget(1e-9))
    with pytest.raises(BudgetExceededError):
        await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert sdk.messages.calls == []


async def test_budget_settles_actual_cost():
    sdk = FakeSDK(
        _message([_tool_use({"label": "a", "score": 1})], input_tokens=10, output_tokens=10)
    )
    budget = Budget(1.0)
    _, usage = await _client(sdk, budget=budget).generate(
        LLMRequest(system="s", prompt="p"), Verdict
    )
    assert budget.spent_usd == pytest.approx(usage.cost_usd)
    assert budget.remaining_usd == pytest.approx(1.0 - usage.cost_usd)


async def test_unsupported_image_type_rejected():
    client = _client(FakeSDK())
    request = LLMRequest(system="s", prompt="p", images=[Image(data=b"x", mime_type="image/bmp")])
    with pytest.raises(LLMError, match="Unsupported image"):
        await client.generate(request, Verdict)


def test_missing_api_key_raises():
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicClient(_settings(anthropic_api_key=None))
