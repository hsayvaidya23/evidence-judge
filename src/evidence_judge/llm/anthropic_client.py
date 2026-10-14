"""LLMClient backed by the official Anthropic SDK (optional extra `anthropic`).

Models that support structured outputs get `output_config.format` with a strict JSON schema
derived from the pydantic model, so the API constrains the reply to schema-valid JSON. The reply
is still validated with pydantic (string and number constraints the API cannot enforce are checked
client-side) and an invalid reply is retried once with the error appended. Older models get one
forced tool whose input schema is the pydantic schema.

Thinking: Haiku 5.5, Sonnet 5.5 and Opus 5.5 think by default and thinking tokens count toward
`max_tokens`; callers bound it with `LLMRequest.effort` (sent as `output_config.effort` to models
that accept it) and a `max_output_tokens` with room for thinking. A reply cut off at `max_tokens`
raises instead of being retried with the same limit.

Transient errors (408/409/429/5xx, connection errors, timeouts) are retried by the SDK itself
with exponential backoff and jitter, honouring `retry-after`; see `Settings.max_retries`.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ValidationError

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
        OutputConfigParam,
        TextBlockParam,
        ToolParam,
    )

logger = logging.getLogger(__name__)

TOOL_NAME = "submit_result"
_MAX_ERROR_CHARS = 2_000
_CHARS_PER_TOKEN_ESTIMATE = 3.0
_IMAGE_TOKEN_ESTIMATE = 1_600
_IMAGE_MEDIA_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_STRUCTURED_OUTPUT_MODELS = frozenset(
    {
        "claude-fable-5-1",
        "claude-mythos-5-1",
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-haiku-5-5",
        "claude-haiku-4-5",
    }
)
_EFFORT_MODELS = frozenset(
    {
        "claude-fable-5-1",
        "claude-mythos-5-1",
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-haiku-5-5",
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
    ) -> None:
        self._settings = settings
        self._budget = budget
        self._prices = prices
        self._client = sdk_client if sdk_client is not None else _load_sdk_client(settings)
        self.totals = TokenTotals()

    def model_for(self, tier: Tier) -> str:
        return self._settings.model_strong if tier is Tier.STRONG else self._settings.model_fast

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]:
        model = self.model_for(request.tier)
        price = price_for(model, self._prices)
        structured = model in _STRUCTURED_OUTPUT_MODELS
        json_schema = strict_json_schema(schema) if structured else schema.model_json_schema()
        usage = Usage()
        error: str | None = None
        for _attempt in range(2):
            message, call_usage = await self._call(
                model, price, request, json_schema, error, structured=structured
            )
            usage.add(call_usage)
            try:
                return self._parse(message, schema, request, structured=structured), usage
            except _InvalidOutputError as exc:
                error = str(exc)[:_MAX_ERROR_CHARS]
                logger.debug("structured output invalid for %s, retrying once", schema.__name__)
        raise LLMError(f"Model output did not satisfy {schema.__name__} after a retry: {error}")

    async def _call(
        self,
        model: str,
        price: ModelPrice,
        request: LLMRequest,
        json_schema: dict[str, Any],
        previous_error: str | None,
        *,
        structured: bool,
    ) -> tuple[Message, Usage]:
        from anthropic import (
            AnthropicError,
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            RateLimitError,
            omit,
        )

        prompt = (
            request.prompt
            if previous_error is None
            else _with_error(request.prompt, previous_error)
        )
        reserved = 0.0
        if self._budget is not None:
            reserved = self._budget.reserve(self._estimate(price, request, prompt, json_schema))
        output_config: OutputConfigParam = {}
        if structured:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if request.effort is not None and model in _EFFORT_MODELS:
            output_config["effort"] = request.effort
        started = time.perf_counter()
        try:
            message = await self._client.messages.create(
                model=model,
                max_tokens=request.max_output_tokens,
                system=self._system(request.system, structured=structured),
                messages=[{"role": "user", "content": self._content(prompt, request.images)}],
                output_config=output_config or omit,
                tools=omit if structured else [self._tool(json_schema)],
                tool_choice=(
                    omit
                    if structured
                    else {"type": "tool", "name": TOOL_NAME, "disable_parallel_tool_use": True}
                ),
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

    def _parse(
        self, message: Message, schema: type[T], request: LLMRequest, *, structured: bool
    ) -> T:
        if message.stop_reason == "refusal":
            raise LLMError("The model declined the request (stop_reason=refusal)")
        if message.stop_reason == "max_tokens":
            raise LLMError(
                f"Model output was cut off at max_tokens={request.max_output_tokens} "
                "(thinking counts toward it); raise max_output_tokens or lower effort"
            )
        if structured:
            return _parse_json(message, schema)
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
        self, price: ModelPrice, request: LLMRequest, prompt: str, json_schema: dict[str, Any]
    ) -> float:
        chars = len(request.system) + len(prompt) + len(json.dumps(json_schema))
        input_tokens = math.ceil(chars / _CHARS_PER_TOKEN_ESTIMATE)
        input_tokens += _IMAGE_TOKEN_ESTIMATE * len(request.images)
        return price.cost(input_tokens=input_tokens, output_tokens=request.max_output_tokens)

    @staticmethod
    def _system(system: str, *, structured: bool) -> list[TextBlockParam]:
        text = (
            system
            if structured
            else (
                f"{system}\n\nReturn your answer only by calling the `{TOOL_NAME}` tool exactly "
                "once, with input that satisfies its JSON schema. Do not answer in plain text."
            )
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


def strict_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema sent as `output_config.format`.

    Every object is closed with `additionalProperties: false`, `const` discriminators become
    required single-value enums, and keywords the API does not support (string lengths, numeric
    bounds, pydantic's `discriminator` mapping) are moved into descriptions by the SDK's
    `transform_schema`; pydantic validation enforces them afterwards.
    """
    from anthropic import transform_schema

    pinned = cast("dict[str, Any]", _pin_constants(schema.model_json_schema()))
    return transform_schema(pinned)


def _pin_constants(node: Any) -> Any:
    if isinstance(node, list):
        return [_pin_constants(item) for item in cast("list[Any]", node)]
    if not isinstance(node, dict):
        return node
    source = cast("dict[str, Any]", node)
    out: dict[str, Any] = {
        key: _pin_constants(value) for key, value in source.items() if key != "discriminator"
    }
    if "const" in out and "enum" not in out:
        out["enum"] = [out.pop("const")]
    properties = source.get("properties")
    if isinstance(properties, dict):
        props = cast("dict[str, Any]", properties)
        constant = [
            name for name, prop in props.items() if isinstance(prop, dict) and "const" in prop
        ]
        required = [*cast("list[str]", out.get("required", [])), *constant]
        if required:
            out["required"] = list(dict.fromkeys(required))
    return out


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
        "Your previous attempt was rejected for the reason above. "
        "Answer again with corrected output."
    )


def _parse_json(message: Message, schema: type[T]) -> T:
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if not text:
        raise _InvalidOutputError(
            f"No JSON output in the response (stop_reason={message.stop_reason})"
        )
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise _InvalidOutputError(_format_validation_error(exc)) from exc


def _format_validation_error(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False)
    ]
    return "Schema validation failed: " + "; ".join(parts)
