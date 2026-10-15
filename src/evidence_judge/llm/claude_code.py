"""LLM backend that runs Claude Code (`claude`) headless, on the user's Claude login.

`claude -p` reads one user message as stream-json on stdin (so screenshots go as base64 image
blocks, never as files) and answers with stream-json; the last `result` event carries the
schema-validated `structured_output` (from `--json-schema`), token usage and `total_cost_usd`.
Every built-in tool is off (`--tools ""` and `--restricted`; the CLI keeps only its internal
StructuredOutput tool), no MCP server, setting file, hook, plugin, skill or CLAUDE.md is loaded,
permission prompts are denied, and nothing is written to the session history.
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any, ClassVar, cast

from evidence_judge.config import Settings
from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import BudgetExceededError, Image, LLMError, LLMRequest, T, Tier
from evidence_judge.llm.budget import Budget
from evidence_judge.llm.cli_client import (
    CLIClient,
    CLIReply,
    CLIRun,
    Invocation,
    as_int,
    json_lines,
)
from evidence_judge.llm.json_schema import portable_json_schema
from evidence_judge.llm.pricing import DEFAULT_PRICES, PriceTable, price_for

_IMAGE_MEDIA_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_CHARS_PER_TOKEN_ESTIMATE = 3.0
_IMAGE_TOKEN_ESTIMATE = 1_600
_FIXED_OVERHEAD_TOKENS = 4_000
_TURNS_ESTIMATE = 2
PRICING_ALIASES: dict[str, str] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
"""Claude Code's model aliases, mapped to the model they are priced as for the budget estimate.
The actual cost of each call comes from the CLI's own `total_cost_usd`."""

LOCKDOWN_FLAGS: tuple[str, ...] = (
    "--tools",
    "",
    "--restricted",
    "--safe-mode",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
    "--disable-slash-commands",
    "--permission-mode",
    "dontAsk",
    "--permission-prompts",
    "none",
    "--max-turns",
    "2",
    "--no-session-persistence",
)
"""Flags that take every capability away from the agent: no built-in tool, no MCP server, no
settings file (so no hooks or permission rules), no CLAUDE.md, skills or plugins, nothing that
could prompt, at most two turns (the answer and one schema correction), nothing persisted.

`--disallowedTools "*"` is deliberately absent: it also denies the internal StructuredOutput
tool that `--json-schema` relies on, so the model could never answer in the schema (observed
with Claude Code 2.1.300)."""


class ClaudeCodeClient(CLIClient):
    name = "claude-code"
    executable: ClassVar[str] = "claude"
    supports_images: ClassVar[bool] = True
    login_hint: ClassVar[str] = "run `claude` once and log in (or `claude auth login`)"

    def __init__(
        self,
        settings: Settings,
        *,
        budget: Budget | None = None,
        prices: PriceTable = DEFAULT_PRICES,
        executable: str | None = None,
    ) -> None:
        super().__init__(settings, budget=budget, executable=executable)
        self._prices = prices

    def model_for(self, tier: Tier) -> str:
        configured = super().model_for(tier)
        if configured:
            return configured
        return self._settings.model_strong if tier is Tier.STRONG else self._settings.model_fast

    def _estimate_usd(self, request: LLMRequest, prompt: str, model: str | None) -> float:
        name = model or self._settings.model_fast
        price = price_for(PRICING_ALIASES.get(name, name), self._prices)
        chars = len(request.system) + len(prompt)
        input_tokens = math.ceil(chars / _CHARS_PER_TOKEN_ESTIMATE) + _FIXED_OVERHEAD_TOKENS
        input_tokens += _IMAGE_TOKEN_ESTIMATE * len(request.images)
        return price.cost(
            input_tokens=input_tokens * _TURNS_ESTIMATE, output_tokens=request.max_output_tokens
        )

    def _invocation(
        self,
        request: LLMRequest,
        prompt: str,
        schema: type[T],
        model: str | None,
        workdir: Path,
        budget_usd: float,
    ) -> Invocation:
        argv = [
            self.executable,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            *LOCKDOWN_FLAGS,
            "--system-prompt",
            request.system,
            "--json-schema",
            json.dumps(portable_json_schema(schema, all_required=False)),
            "--max-budget-usd",
            f"{max(budget_usd, 0.0):.6f}",
        ]
        if model:
            argv += ["--model", model]
        if request.effort is not None:
            argv += ["--effort", request.effort]
        content: list[dict[str, Any]] = [_image_block(image) for image in request.images]
        content.append({"type": "text", "text": prompt})
        message = {"type": "user", "message": {"role": "user", "content": content}}
        return Invocation(argv=argv, stdin=(json.dumps(message) + "\n").encode("utf-8"))

    def _parse(self, run: CLIRun) -> CLIReply:
        results = [event for event in json_lines(run.stdout) if event.get("type") == "result"]
        if not results:
            raise self._failure(run)
        result = results[-1]
        usage = _usage(result)
        subtype = result.get("subtype")
        if subtype == "error_max_budget_usd":
            raise BudgetExceededError(
                f"claude-code stopped at the LLM budget (spent ${usage.cost_usd:.4f})"
            )
        if result.get("is_error") or subtype != "success":
            message = _error_text(result)
            if subtype in ("error_max_structured_output_retries", "error_max_turns"):
                return CLIReply(
                    invalid=f"Claude Code gave up on the schema: {message}", usage=usage
                )
            raise self._failure(run, message)
        structured = result.get("structured_output")
        if structured is not None:
            return CLIReply(payload=structured, usage=usage)
        text = result.get("result")
        return CLIReply(text=text if isinstance(text, str) else None, usage=usage)


def _image_block(image: Image) -> dict[str, Any]:
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


def _usage(result: dict[str, Any]) -> Usage:
    raw = result.get("usage")
    tokens = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    cost = result.get("total_cost_usd")
    return Usage(
        input_tokens=as_int(tokens.get("input_tokens"))
        + as_int(tokens.get("cache_creation_input_tokens"))
        + as_int(tokens.get("cache_read_input_tokens")),
        output_tokens=as_int(tokens.get("output_tokens")),
        cost_usd=float(cost) if isinstance(cost, int | float) and cost > 0 else 0.0,
    )


def _error_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    text = result.get("result")
    if isinstance(text, str) and text.strip():
        parts.append(text.strip())
    errors = result.get("errors")
    if isinstance(errors, list):
        parts.extend(str(e) for e in cast("list[Any]", errors) if e)
    status = result.get("api_error_status")
    if status:
        parts.append(f"API status {status}")
    subtype = result.get("subtype")
    if not parts and isinstance(subtype, str):
        parts.append(subtype)
    return "; ".join(parts)
