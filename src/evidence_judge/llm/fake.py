"""Deterministic in-memory LLMClient for tests and offline demos."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import LLMError, LLMRequest, T

FakeResponse = BaseModel | Mapping[str, Any] | Exception
FakeHandler = Callable[[LLMRequest, type[BaseModel]], BaseModel | Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class RecordedCall:
    request: LLMRequest
    schema: type[BaseModel]


class FakeLLM:
    """Answers from per-schema queues (keyed by schema class name) or a handler callable.

    Queued items may be a model instance, a plain mapping validated against the schema, or an
    exception to raise. Every request is recorded in `calls` for assertions.
    """

    name = "fake"

    def __init__(
        self,
        responses: Mapping[str, Iterable[FakeResponse]] | None = None,
        *,
        handler: FakeHandler | None = None,
        usage: Usage | None = None,
    ) -> None:
        self._queues: defaultdict[str, deque[FakeResponse]] = defaultdict(deque)
        for schema_name, items in (responses or {}).items():
            self._queues[schema_name].extend(items)
        self._handler = handler
        self._usage = usage or Usage(llm_calls=1)
        self.calls: list[RecordedCall] = []

    def queue(self, schema: type[BaseModel] | str, *items: FakeResponse) -> None:
        key = schema if isinstance(schema, str) else schema.__name__
        self._queues[key].extend(items)

    def pending(self, schema: type[BaseModel] | str) -> int:
        key = schema if isinstance(schema, str) else schema.__name__
        return len(self._queues[key])

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]:
        self.calls.append(RecordedCall(request=request, schema=schema))
        queue = self._queues[schema.__name__]
        if queue:
            item = queue.popleft()
        elif self._handler is not None:
            item = self._handler(request, schema)
        else:
            raise LLMError(f"FakeLLM has no response queued for schema {schema.__name__}")
        if isinstance(item, Exception):
            raise item
        return _coerce(item, schema), self._usage.model_copy()


def _coerce(item: BaseModel | Mapping[str, Any], schema: type[T]) -> T:
    if isinstance(item, schema):
        return item
    payload = item.model_dump(mode="json") if isinstance(item, BaseModel) else dict(item)
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise LLMError(f"FakeLLM response does not match {schema.__name__}: {exc}") from exc
