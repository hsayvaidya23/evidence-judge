from __future__ import annotations

from evidence_judge.config import Settings
from evidence_judge.llm.anthropic_client import AnthropicClient
from evidence_judge.llm.base import LLMClient
from evidence_judge.llm.budget import Budget
from evidence_judge.llm.cache import CachedClient
from evidence_judge.llm.pricing import DEFAULT_PRICES, PriceTable


def build_llm(
    settings: Settings,
    *,
    budget: Budget | None = None,
    prices: PriceTable = DEFAULT_PRICES,
) -> LLMClient | None:
    """Return the configured LLM client, or None when no API key is set (offline mode).

    The budget caps real API spend only; cache hits are free. Pass one `Budget` per judged run
    to share the cap across every call made for that run.
    """
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        return None
    client = AnthropicClient(
        settings, budget=budget or Budget(settings.max_cost_usd), prices=prices
    )
    if settings.cache_dir is None:
        return client
    return CachedClient(client, settings.cache_dir, model_for=client.model_for)
