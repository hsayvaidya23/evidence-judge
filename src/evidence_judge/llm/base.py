from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from evidence_judge.core.verdict import Usage

T = TypeVar("T", bound=BaseModel)

Effort = Literal["low", "medium", "high"]


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
    max_output_tokens: int = 4096
    effort: Effort | None = None


class LLMError(RuntimeError):
    pass


class BudgetExceededError(LLMError):
    pass


class LLMConfigError(LLMError):
    """The LLM cannot work as configured (unknown model, missing SDK or key). Retrying or
    falling back to heuristics would only hide it, so the judge lets it propagate."""


class LLMAuthError(LLMConfigError):
    """The provider rejected the API key (401) or refused it access (403)."""


class LLMClient(Protocol):
    """Structured-output LLM access. Implementations must return a validated instance of `schema`."""

    name: str

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]: ...
