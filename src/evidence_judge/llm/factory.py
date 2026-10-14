from __future__ import annotations

from evidence_judge.config import Settings
from evidence_judge.llm.anthropic_client import AnthropicClient
from evidence_judge.llm.base import LLMClient
from evidence_judge.llm.budget import Budget
from evidence_judge.llm.cache import CachedClient
from evidence_judge.llm.concurrency import ConcurrencyLimitedClient
from evidence_judge.llm.pricing import DEFAULT_PRICES, PriceTable


def build_llm(
    settings: Settings,
    *,
    budget: Budget | None = None,
    prices: PriceTable = DEFAULT_PRICES,
) -> LLMClient | None:
    """Return the configured LLM client, or None when no API key is set (offline mode).

    Each `Judge.judge` call caps its own run at `settings.max_cost_usd` (see `run_budget`),
    so the client can be reused across runs. `budget`, when given, is an extra cap on everything
    this client spends (a whole benchmark, say). Budgets cap real API spend only; cache hits are
    free. Real API calls are limited to `settings.max_concurrent_llm_calls` in flight; cache hits
    do not take a slot.
    """
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        return None
    client = AnthropicClient(settings, budget=budget, prices=prices)
    limited = ConcurrencyLimitedClient(client, max_concurrent=settings.max_concurrent_llm_calls)
    if settings.cache_dir is None:
        return limited
    return CachedClient(limited, settings.cache_dir, model_for=client.model_for)
