"""Contract tests: the real SDK client serialises our request and parses a recorded-shape API
response over a mocked HTTP transport, so no network is used but the SDK's own request building,
response models and status handling are exercised end to end."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from pydantic import SecretStr

from evidence_judge.checks.screenshot import ScreenshotAnswer, build_request
from evidence_judge.config import Settings
from evidence_judge.core.predicates import ScreenshotPredicate
from evidence_judge.llm.anthropic_client import AnthropicClient, strict_json_schema
from evidence_judge.llm.base import Image, LLMError

RECORDED_RESPONSE: dict[str, Any] = {
    "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "EqQBCkYIBxgCKkDp2"},
        {
            "type": "text",
            "text": '{"answer":"yes","reason":"The page shows an Order #1042 confirmed banner."}',
        },
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "stop_details": None,
    "usage": {
        "input_tokens": 1830,
        "output_tokens": 61,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 412,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        "server_tool_use": None,
        "service_tier": "standard",
    },
}


class Recorder:
    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        return self.responses.pop(0)


def _client(recorder: Recorder, *, max_retries: int = 0) -> AnthropicClient:
    settings = Settings.model_validate(
        {
            "anthropic_api_key": SecretStr("sk-test-not-real"),
            "model_strong": "claude-sonnet-5-5",
            "cache_dir": None,
            "max_retries": max_retries,
        }
    )
    sdk = AsyncAnthropic(
        api_key="sk-test-not-real",
        max_retries=max_retries,
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(recorder)),
    )
    return AnthropicClient(settings, sdk_client=sdk)


def _request() -> Any:
    pred = ScreenshotPredicate(question="Is an order confirmation shown?")
    return build_request(pred, [Image(data=b"\xff\xd8jpeg", mime_type="image/jpeg")])


async def test_structured_output_round_trip_through_the_real_sdk() -> None:
    recorder = Recorder(httpx2.Response(200, json=RECORDED_RESPONSE))
    client = _client(recorder)
    answer, usage = await client.generate(_request(), ScreenshotAnswer)
    assert answer == ScreenshotAnswer(
        answer="yes", reason="The page shows an Order #1042 confirmed banner."
    )
    assert usage.input_tokens == 1830 + 412
    assert usage.output_tokens == 61
    body = recorder.bodies[0]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 4096
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": strict_json_schema(ScreenshotAnswer)},
        "effort": "low",
    }
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert "tools" not in body
    assert "tool_choice" not in body
    assert "thinking" not in body
    assert [block["type"] for block in body["messages"][0]["content"]] == ["image", "text"]


async def test_overloaded_is_retried_by_the_sdk_not_by_us() -> None:
    overloaded = httpx2.Response(
        529,
        headers={"retry-after-ms": "1"},
        json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    recorder = Recorder(overloaded, httpx2.Response(200, json=RECORDED_RESPONSE))
    answer, usage = await _client(recorder, max_retries=1).generate(_request(), ScreenshotAnswer)
    assert answer.answer == "yes"
    assert len(recorder.bodies) == 2
    assert usage.llm_calls == 1


async def test_exhausted_sdk_retries_surface_as_llm_error() -> None:
    overloaded = httpx2.Response(
        529,
        json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    recorder = Recorder(overloaded)
    with pytest.raises(LLMError, match="error 529"):
        await _client(recorder).generate(_request(), ScreenshotAnswer)
    assert len(recorder.bodies) == 1
