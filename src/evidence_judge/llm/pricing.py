"""Per-model token prices used for cost accounting and budget checks.

All prices are USD per million tokens and are configurable assumptions taken from Anthropic's
published first-party rate card (October 2026). They are not fetched at runtime: pass a custom
`PriceTable` to `AnthropicClient` when your contract, platform (Bedrock, Vertex) or the public
price list differs. Unknown models fall back to `DEFAULT_PRICE`, deliberately priced at the most
expensive listed tier so budgets fail safe rather than under-count; a warning is logged once per
unknown model so a typo or an unlisted model does not silently inflate reported costs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MILLION = 1_000_000
_warned_unknown: set[str] = set()


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input: float
    output: float
    cache_write: float
    cache_read: float
    long_prompt_threshold: int | None = None
    long_prompt_multiplier: float = 1.0

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        prompt_tokens = input_tokens + cache_write_tokens + cache_read_tokens
        factor = (
            self.long_prompt_multiplier
            if self.long_prompt_threshold is not None and prompt_tokens > self.long_prompt_threshold
            else 1.0
        )
        total = (
            input_tokens * self.input
            + output_tokens * self.output
            + cache_write_tokens * self.cache_write
            + cache_read_tokens * self.cache_read
        )
        return total * factor / _MILLION


PriceTable = Mapping[str, ModelPrice]

DEFAULT_PRICES: PriceTable = {
    "claude-haiku-5-5": ModelPrice(
        input=0.10,
        output=0.50,
        cache_write=0.125,
        cache_read=0.01,
        long_prompt_threshold=100_000,
        long_prompt_multiplier=5.0,
    ),
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_write=2.50, cache_read=0.20),
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
    "claude-haiku-4-5": ModelPrice(input=1.00, output=5.00, cache_write=1.25, cache_read=0.10),
    "claude-sonnet-4-6": ModelPrice(input=3.00, output=15.00, cache_write=3.75, cache_read=0.30),
}

DEFAULT_PRICE = ModelPrice(input=10.00, output=50.00, cache_write=12.50, cache_read=1.00)


def is_known_model(model: str, table: PriceTable = DEFAULT_PRICES) -> bool:
    return model in table


def price_for(model: str, table: PriceTable = DEFAULT_PRICES) -> ModelPrice:
    price = table.get(model)
    if price is not None:
        return price
    if model not in _warned_unknown:
        _warned_unknown.add(model)
        logger.warning(
            "No price listed for model %r; costing it at the fail-safe default "
            "(input $%.2f / output $%.2f per MTok). Pass a PriceTable to price it correctly.",
            model,
            DEFAULT_PRICE.input,
            DEFAULT_PRICE.output,
        )
    return DEFAULT_PRICE
