from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Literal

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pydantic import BaseModel, SecretStr

from evidence_judge.config import Settings
from evidence_judge.extract.schema import ClaimExtraction
from evidence_judge.llm.anthropic_client import TOOL_NAME, AnthropicClient, strict_json_schema
from evidence_judge.llm.base import BudgetExceededError, Image, LLMError, LLMRequest, Tier
from evidence_judge.llm.budget import Budget, run_budget


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
    content: list[dict[str, Any]], *, stop_reason: str = "end_turn", **usage: int
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


_THINKING = {"type": "thinking", "thinking": "", "signature": "sig"}


def _json(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [_THINKING, {"type": "text", "text": json.dumps(payload)}]


def _tool_use(payload: dict[str, Any], name: str = TOOL_NAME) -> dict[str, Any]:
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": payload}


class _Messages:
    def __init__(self, outcomes: list[Message | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append({k: v for k, v in kwargs.items() if not isinstance(v, anthropic.Omit)})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSDK:
    def __init__(self, *outcomes: Message | Exception) -> None:
        self.messages = _Messages(list(outcomes))


def _client(sdk: FakeSDK, settings: Settings | None = None, **kwargs: Any) -> AnthropicClient:
    return AnthropicClient(settings or _settings(), sdk_client=sdk, **kwargs)  # type: ignore[arg-type]


async def test_returns_validated_model_and_usage_with_cache_tokens():
    sdk = FakeSDK(
        _message(
            _json({"label": "ok", "score": 3}),
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


async def test_request_uses_structured_output_format_not_tools():
    sdk = FakeSDK(_message(_json({"label": "a", "score": 1})))
    client = _client(sdk)
    image = Image(data=b"\x89PNG", mime_type="image/png")
    await client.generate(
        LLMRequest(system="judge", prompt="look", tier=Tier.STRONG, images=[image]), Verdict
    )
    call = sdk.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert "tools" not in call
    assert "tool_choice" not in call
    assert call["output_config"] == {
        "format": {"type": "json_schema", "schema": strict_json_schema(Verdict)}
    }
    assert call["max_tokens"] == 4096
    assert call["system"] == [
        {"type": "text", "text": "judge", "cache_control": {"type": "ephemeral"}}
    ]
    image_block, text_block = call["messages"][0]["content"]
    assert image_block["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(b"\x89PNG").decode(),
    }
    assert text_block == {"type": "text", "text": "look"}


@pytest.mark.parametrize(
    ("model", "expected"),
    [("claude-sonnet-5-5", "low"), ("claude-haiku-5-5", "low"), ("claude-haiku-4-5", None)],
)
async def test_effort_is_sent_only_to_models_that_accept_it(model: str, expected: str | None):
    sdk = FakeSDK(_message(_json({"label": "a", "score": 1})))
    client = _client(sdk, _settings(model_fast=model))
    await client.generate(LLMRequest(system="s", prompt="p", effort="low"), Verdict)
    assert sdk.messages.calls[0]["output_config"].get("effort") == expected


async def test_legacy_models_use_a_forced_tool():
    sdk = FakeSDK(_message([_tool_use({"label": "a", "score": 1})], stop_reason="tool_use"))
    client = _client(sdk, _settings(model_fast="claude-sonnet-4-6"))
    result, _ = await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    call = sdk.messages.calls[0]
    assert result.score == 1
    assert "output_config" not in call
    assert call["tool_choice"] == {
        "type": "tool",
        "name": TOOL_NAME,
        "disable_parallel_tool_use": True,
    }
    assert call["tools"][0]["input_schema"] == Verdict.model_json_schema()
    assert TOOL_NAME in call["system"][0]["text"]


async def test_validation_failure_retries_once_with_error_appended():
    sdk = FakeSDK(
        _message(_json({"label": "a", "score": "many"})),
        _message(_json({"label": "a", "score": 2})),
    )
    client = _client(sdk)
    result, usage = await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert result.score == 2
    assert usage.llm_calls == 2
    retry_text = sdk.messages.calls[1]["messages"][0]["content"][-1]["text"]
    assert retry_text.startswith("p")
    assert "<previous_attempt_error>" in retry_text
    assert "score" in retry_text


async def test_non_json_output_is_retried_then_raises():
    text_only = _message([{"type": "text", "text": "PASS"}])
    sdk = FakeSDK(text_only, text_only)
    with pytest.raises(LLMError, match="after a retry"):
        await _client(sdk).generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert len(sdk.messages.calls) == 2


async def test_max_tokens_truncation_raises_without_retry():
    truncated = _message([_THINKING], stop_reason="max_tokens")
    sdk = FakeSDK(truncated)
    with pytest.raises(LLMError, match="max_tokens"):
        await _client(sdk).generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert len(sdk.messages.calls) == 1


async def test_refusal_raises_without_retry():
    sdk = FakeSDK(_message([], stop_reason="refusal"))
    with pytest.raises(LLMError, match="declined"):
        await _client(sdk).generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert len(sdk.messages.calls) == 1


def _walk(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)  # pyright: ignore[reportUnknownArgumentType]
        for value in node.values():  # pyright: ignore[reportUnknownVariableType]
            found.extend(_walk(value))
    elif isinstance(node, list):
        for value in node:  # pyright: ignore[reportUnknownVariableType]
            found.extend(_walk(value))
    return found


class Tagged(BaseModel):
    kind: Literal["tagged"] = "tagged"
    note: str | None = None


def test_strict_schema_closes_objects_and_keeps_discriminators():
    schema = strict_json_schema(ClaimExtraction)
    nodes = _walk(schema)
    objects = [n for n in nodes if n.get("type") == "object"]
    assert objects
    assert all(n["additionalProperties"] is False for n in objects)
    assert not any("const" in n or "discriminator" in n for n in nodes)
    assert not any("minLength" in n for n in nodes)
    tagged = strict_json_schema(Tagged)
    assert tagged["properties"]["kind"]["enum"] == ["tagged"]
    assert "kind" in tagged["required"]
    Tagged.model_validate({"kind": "tagged"})


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
    sdk = FakeSDK(_message(_json({"label": "a", "score": 1})))
    client = _client(sdk, budget=Budget(1e-9))
    with pytest.raises(BudgetExceededError):
        await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert sdk.messages.calls == []


async def test_budget_settles_actual_cost():
    sdk = FakeSDK(_message(_json({"label": "a", "score": 1}), input_tokens=10, output_tokens=10))
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


def test_transient_retries_are_delegated_to_the_sdk_client():
    client = AnthropicClient(_settings(max_retries=5, request_timeout_s=12.0))
    sdk = client._client  # pyright: ignore[reportPrivateUsage]
    assert sdk.max_retries == 5
    assert sdk.timeout == 12.0


class _Hanging:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def create(self, **kwargs: Any) -> Message:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_cancelled_call_releases_its_reservation():
    budget = Budget(1.0)
    sdk = FakeSDK()
    hanging = _Hanging()
    sdk.messages = hanging  # type: ignore[assignment]
    client = _client(sdk, budget=budget)
    task = asyncio.create_task(client.generate(LLMRequest(system="s", prompt="p"), Verdict))
    await hanging.started.wait()
    assert budget.remaining_usd < 1.0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.remaining_usd == pytest.approx(1.0)


async def test_overspent_call_returns_its_result_and_the_next_call_is_refused():
    sdk = FakeSDK(
        _message(_json({"label": "a", "score": 1}), input_tokens=10, output_tokens=200_000),
        _message(_json({"label": "b", "score": 2})),
    )
    budget = Budget(0.05)
    client = _client(sdk, budget=budget)
    result, usage = await client.generate(
        LLMRequest(system="s", prompt="p", max_output_tokens=10), Verdict
    )
    assert result.label == "a"
    assert usage.cost_usd > budget.max_cost_usd
    with pytest.raises(BudgetExceededError):
        await client.generate(LLMRequest(system="s", prompt="p", max_output_tokens=10), Verdict)


async def test_run_budget_and_client_budget_are_both_enforced():
    sdk = FakeSDK(_message(_json({"label": "a", "score": 1}), input_tokens=10, output_tokens=10))
    client_budget = Budget(1.0)
    per_run = Budget(1.0)
    client = _client(sdk, budget=client_budget)
    with run_budget(per_run):
        _, usage = await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert per_run.spent_usd == pytest.approx(usage.cost_usd)
    assert client_budget.spent_usd == pytest.approx(usage.cost_usd)
    with run_budget(Budget(1e-9)), pytest.raises(BudgetExceededError):
        await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert client_budget.remaining_usd == pytest.approx(1.0 - usage.cost_usd)


@pytest.mark.parametrize(
    ("error_type", "status", "expected", "match"),
    [
        (anthropic.AuthenticationError, 401, "LLMAuthError", "ANTHROPIC_API_KEY rejected"),
        (anthropic.PermissionDeniedError, 403, "LLMAuthError", "ANTHROPIC_API_KEY"),
        (anthropic.NotFoundError, 404, "LLMConfigError", "claude-haiku-5-5"),
    ],
)
async def test_auth_and_config_errors_are_typed_and_release_budget(
    error_type: type[anthropic.APIStatusError], status: int, expected: str, match: str
):
    from evidence_judge.llm import base

    error = error_type("nope", response=httpx2.Response(status, request=_request()), body=None)
    budget = Budget(1.0)
    client = _client(FakeSDK(error), budget=budget)
    with pytest.raises(getattr(base, expected), match=match) as info:
        await client.generate(LLMRequest(system="s", prompt="p"), Verdict)
    assert isinstance(info.value, base.LLMConfigError)
    assert "sk-test-not-real" not in str(info.value)
    assert budget.remaining_usd == pytest.approx(1.0)
