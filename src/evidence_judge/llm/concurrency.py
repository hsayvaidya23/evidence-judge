"""Caps the number of in-flight LLM requests so a run with many model checks cannot fan out
into an unbounded burst of concurrent (often image-carrying) API calls.

Transient failures (429, 529 overloaded, other 5xx, timeouts, connection errors) are retried by
the Anthropic SDK itself with exponential backoff and jitter, honouring `retry-after`; the number
of retries is `Settings.max_retries`. This wrapper deliberately does not retry again on top.
"""

from __future__ import annotations

import asyncio
import weakref

from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import LLMClient, LLMRequest, T


class ConcurrencyLimitedClient:
    """Wraps an LLMClient so at most `max_concurrent` `generate` calls run at once per event loop."""

    def __init__(self, inner: LLMClient, *, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.inner = inner
        self.name = inner.name
        self.max_concurrent = max_concurrent
        self._semaphores: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Semaphore
        ] = weakref.WeakKeyDictionary()

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]:
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = self._semaphores[loop] = asyncio.Semaphore(self.max_concurrent)
        async with semaphore:
            return await self.inner.generate(request, schema)
