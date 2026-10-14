from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeVar

from pydantic import BaseModel

from evidence_judge.core.verdict import Usage

T = TypeVar("T", bound=BaseModel)


class Tier(StrEnum):
    FAST = "fast"
    STRONG = "strong"


@dataclass(frozen=True, slots=True)
class Image:
    data: bytes
    mime_type: str = "image/jpeg"


@dataclass(slots=True)
class LLMRequest:
    system: str
    prompt: str
    tier: Tier = Tier.FAST
    images: list[Image] = field(default_factory=list[Image])
    max_output_tokens: int = 2048


class LLMError(RuntimeError):
    pass


class BudgetExceededError(LLMError):
    pass


class LLMClient(Protocol):
    """Structured-output LLM access. Implementations must return a validated instance of `schema`."""

    name: str

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]: ...
