"""LLMClient backed by the official Anthropic SDK (optional extra `anthropic`).

Structured output is obtained by offering exactly one tool whose input schema is the pydantic
schema, then validating the tool input with pydantic. Current models (Opus 5.5, Sonnet 5.5,
Fable 5.1) reject a forced `tool_choice`, and Haiku 5.5 runs with thinking on by default, which
is incompatible with forcing; for those the tool is offered with `auto` plus an explicit
instruction, and a missing call is treated like a validation failure (retried once).
"""

from __future__ import annotations

import base64
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from evidence_judge.config import Settings
from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import Image, LLMError, LLMRequest, T, Tier
from evidence_judge.llm.budget import Budget
from evidence_judge.llm.pricing import DEFAULT_PRICES, ModelPrice, PriceTable, price_for

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from anthropic.types import (
        ContentBlockParam,
        ImageBlockParam,
        Message,
        TextBlockParam,
        ToolChoiceParam,
        ToolParam,
    )

logger = logging.getLogger(__name__)

TOOL_NAME = "submit_result"
_MAX_ERROR_CHARS = 2_000
_CHARS_PER_TOKEN_ESTIMATE = 3.0
_IMAGE_TOKEN_ESTIMATE = 1_600
_IMAGE_MEDIA_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_AUTO_TOOL_CHOICE_MODELS = frozenset(
    {
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-1",
        "claude-mythos-5-1",
    }
)


@dataclass(slots=True)
class TokenTotals:
    """Cumulative token accounting, including the cache split that `Usage` folds into input."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


class _InvalidOutputError(Exception):
    pass


def _load_sdk_client(settings: Settings) -> AsyncAnthropic:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise LLMError(
            "The Anthropic SDK is not installed; install with `pip install evidence-judge[anthropic]`"
        ) from exc
    if settings.anthropic_api_key is None:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    return AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        timeout=settings.request_timeout_s,
        max_retries=settings.max_retries,
    )


class AnthropicClient:
    name = "anthropic"

    def __init__(
        self,
        settings: Settings,
        *,
        budget: Budget | None = None,
        prices: PriceTable = DEFAULT_PRICES,
        sdk_client: AsyncAnthropic | None = None,
        force_tool_choice: bool | None = None,
    ) -> None:
        self._settings = settings
        self._budget = budget
        self._prices = prices
        self._force_tool_choice = force_tool_choice
        self._client = sdk_client if sdk_client is not None else _load_sdk_client(settings)
        self.totals = TokenTotals()

    def model_for(self, tier: Tier) -> str:
        return self._settings.model_strong if tier is Tier.STRONG else self._settings.model_fast

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]:
        model = self.model_for(request.tier)
        price = price_for(model, self._prices)
        input_schema = schema.model_json_schema()
        usage = Usage()
        error: str | None = None
        for _attempt in range(2):
            message, call_usage = await self._call(model, price, request, input_schema, error)
            usage.add(call_usage)
            try:
                return self._parse(message, schema), usage
            except _InvalidOutputError as exc:
                error = str(exc)[:_MAX_ERROR_CHARS]
                logger.debug("structured output invalid for %s, retrying once", schema.__name__)
        raise LLMError(f"Model output did not satisfy {schema.__name__} after a retry: {error}")

    async def _call(
        self,
        model: str,
        price: ModelPrice,
        request: LLMRequest,
        input_schema: dict[str, Any],
        previous_error: str | None,
    ) -> tuple[Message, Usage]:
        from anthropic import (
            AnthropicError,
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            RateLimitError,
        )

        prompt = (
            request.prompt
            if previous_error is None
            else _with_error(request.prompt, previous_error)
        )
        reserved = 0.0
        if self._budget is not None:
            reserved = self._budget.reserve(self._estimate(price, request, prompt, input_schema))
        started = time.perf_counter()
        try:
            message = await self._client.messages.create(
                model=model,
                max_tokens=request.max_output_tokens,
                system=self._system(request.system),
                messages=[{"role": "user", "content": self._content(prompt, request.images)}],
                tools=[self._tool(input_schema)],
                tool_choice=self._tool_choice(model),
            )
        except APITimeoutError as exc:
            self._release(reserved)
            raise LLMError(
                f"Anthropic request timed out after {self._settings.request_timeout_s}s"
            ) from exc
        except APIConnectionError as exc:
            self._release(reserved)
            raise LLMError("Could not connect to the Anthropic API") from exc
        except RateLimitError as exc:
            self._release(reserved)
            raise LLMError(f"Anthropic rate limit hit (request_id={exc.request_id})") from exc
        except APIStatusError as exc:
            self._release(reserved)
            raise LLMError(
                f"Anthropic API error {exc.status_code} (request_id={exc.request_id})"
            ) from exc
        except AnthropicError as exc:
            self._release(reserved)
            raise LLMError(f"Anthropic SDK error: {type(exc).__name__}") from exc
        latency_ms = (time.perf_counter() - started) * 1000
        usage = self._record(message, price, latency_ms)
        if self._budget is not None:
            self._budget.settle(reserved, usage.cost_usd)
        logger.debug(
            "anthropic call model=%s in=%d out=%d cost=%.6f latency_ms=%.0f",
            model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
            latency_ms,
        )
        return message, usage

    def _release(self, reserved: float) -> None:
        if self._budget is not None:
            self._budget.release(reserved)

    def _record(self, message: Message, price: ModelPrice, latency_ms: float) -> Usage:
        raw = message.usage
        cache_write = raw.cache_creation_input_tokens or 0
        cache_read = raw.cache_read_input_tokens or 0
        cost = price.cost(
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
        )
        totals = self.totals
        totals.calls += 1
        totals.input_tokens += raw.input_tokens
        totals.output_tokens += raw.output_tokens
        totals.cache_write_tokens += cache_write
        totals.cache_read_tokens += cache_read
        totals.cost_usd += cost
        return Usage(
            llm_calls=1,
            input_tokens=raw.input_tokens + cache_write + cache_read,
            output_tokens=raw.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    def _parse(self, message: Message, schema: type[T]) -> T:
        if message.stop_reason == "refusal":
            raise LLMError("The model declined the request (stop_reason=refusal)")
        for block in message.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                try:
                    return schema.model_validate(block.input)
                except ValidationError as exc:
                    raise _InvalidOutputError(_format_validation_error(exc)) from exc
        raise _InvalidOutputError(
            f"No {TOOL_NAME} tool call in the response (stop_reason={message.stop_reason}); "
            f"you must call {TOOL_NAME}"
        )

    def _estimate(
        self, price: ModelPrice, request: LLMRequest, prompt: str, input_schema: dict[str, Any]
    ) -> float:
        chars = len(request.system) + len(prompt) + len(json.dumps(input_schema))
        input_tokens = math.ceil(chars / _CHARS_PER_TOKEN_ESTIMATE)
        input_tokens += _IMAGE_TOKEN_ESTIMATE * len(request.images)
        return price.cost(input_tokens=input_tokens, output_tokens=request.max_output_tokens)

    def _uses_forced_tool_choice(self, model: str) -> bool:
        if self._force_tool_choice is not None:
            return self._force_tool_choice
        return model not in _AUTO_TOOL_CHOICE_MODELS

    def _tool_choice(self, model: str) -> ToolChoiceParam:
        if self._uses_forced_tool_choice(model):
            return {"type": "tool", "name": TOOL_NAME, "disable_parallel_tool_use": True}
        return {"type": "auto", "disable_parallel_tool_use": True}

    @staticmethod
    def _system(system: str) -> list[TextBlockParam]:
        text = (
            f"{system}\n\nReturn your answer only by calling the `{TOOL_NAME}` tool exactly once, "
            "with input that satisfies its JSON schema. Do not answer in plain text."
        )
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    @staticmethod
    def _tool(input_schema: dict[str, Any]) -> ToolParam:
        return {
            "name": TOOL_NAME,
            "description": "Submit the final structured result.",
            "input_schema": input_schema,
        }

    @staticmethod
    def _content(prompt: str, images: list[Image]) -> list[ContentBlockParam]:
        blocks: list[ContentBlockParam] = [_image_block(image) for image in images]
        text: TextBlockParam = {"type": "text", "text": prompt}
        blocks.append(text)
        return blocks


def _image_block(image: Image) -> ImageBlockParam:
    if image.mime_type not in _IMAGE_MEDIA_TYPES:
        raise LLMError(f"Unsupported image type {image.mime_type!r}")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.mime_type,
            "data": base64.standard_b64encode(image.data).decode("ascii"),
        },
    }


def _with_error(prompt: str, error: str) -> str:
    return (
        f"{prompt}\n\n<previous_attempt_error>\n{error}\n</previous_attempt_error>\n"
        f"Your previous attempt was rejected for the reason above. "
        f"Call `{TOOL_NAME}` again with corrected input."
    )


def _format_validation_error(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False)
    ]
    return "Schema validation failed: " + "; ".join(parts)
