from __future__ import annotations

from typing import Protocol

from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.verdict import Usage

MAX_CLAIMS = 30
MAX_PREDICATES_PER_CLAIM = 3


class ClaimExtractor(Protocol):
    """Turns an agent's (untrusted) final answer and reasoning into atomic, checkable claims."""

    name: str

    async def extract(
        self, *, task: str | None, answer: str | None, reasoning: list[str]
    ) -> tuple[ClaimSet, Usage]: ...
