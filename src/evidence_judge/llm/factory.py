from __future__ import annotations

from collections.abc import Callable

from evidence_judge.config import Settings
from evidence_judge.llm.anthropic_client import AnthropicClient
from evidence_judge.llm.base import LLMClient, Tier
from evidence_judge.llm.budget import Budget
from evidence_judge.llm.cache import CachedClient
from evidence_judge.llm.claude_code import ClaudeCodeClient
from evidence_judge.llm.cli_client import CLIClient
from evidence_judge.llm.codex import CodexClient
from evidence_judge.llm.concurrency import ConcurrencyLimitedClient
from evidence_judge.llm.gemini import GeminiClient
from evidence_judge.llm.pricing import DEFAULT_PRICES, PriceTable


def build_llm(
    settings: Settings,
    *,
    budget: Budget | None = None,
    prices: PriceTable = DEFAULT_PRICES,
) -> LLMClient | None:
    """Return the client for `settings.llm_backend`, or None for the `anthropic` backend when
    no API key is set (offline mode).

    A CLI backend (`claude-code`, `codex`, `gemini`) needs its CLI on PATH and logged in; a
    missing CLI raises `LLMConfigError` here, a missing login on the first call.

    Each `Judge.judge` call caps its own run at `settings.max_cost_usd` (see `run_budget`),
    so the client can be reused across runs. `budget`, when given, is an extra cap on everything
    this client spends (a whole benchmark, say). Budgets cap real spend only; cache hits are
    free, and `codex` and `gemini` report no cost. Calls are limited to
    `settings.max_concurrent_llm_calls` in flight; cache hits do not take a slot.
    """
    client: LLMClient
    model_for: Callable[[Tier], str]
    match settings.llm_backend:
        case "anthropic":
            if (
                settings.anthropic_api_key is None
                or not settings.anthropic_api_key.get_secret_value()
            ):
                return None
            anthropic = AnthropicClient(settings, budget=budget, prices=prices)
            client, model_for = anthropic, anthropic.model_for
        case "claude-code":
            cli: CLIClient = ClaudeCodeClient(settings, budget=budget, prices=prices)
            client, model_for = cli, cli.cache_model
        case "codex":
            cli = CodexClient(settings, budget=budget)
            client, model_for = cli, cli.cache_model
        case "gemini":
            cli = GeminiClient(settings, budget=budget)
            client, model_for = cli, cli.cache_model
    limited = ConcurrencyLimitedClient(client, max_concurrent=settings.max_concurrent_llm_calls)
    if settings.cache_dir is None:
        return limited
    return CachedClient(limited, settings.cache_dir, model_for=model_for)
