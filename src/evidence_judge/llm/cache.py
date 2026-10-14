"""Content-addressed disk cache for structured LLM responses."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import LLMClient, LLMRequest, T, Tier

logger = logging.getLogger(__name__)

_CACHE_VERSION = 1


def cache_key(model: str, request: LLMRequest, schema_name: str, schema_json: str) -> str:
    material = {
        "v": _CACHE_VERSION,
        "model": model,
        "system": request.system,
        "prompt": request.prompt,
        "images": [
            [image.mime_type, hashlib.sha256(image.data).hexdigest()] for image in request.images
        ],
        "max_output_tokens": request.max_output_tokens,
        "schema_name": schema_name,
        "schema": schema_json,
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CachedClient:
    """Wraps an LLMClient; identical requests are answered from disk at zero cost.

    Entries hold the validated JSON plus the usage of the original call. A corrupt or
    schema-incompatible entry counts as a miss and is overwritten. Concurrent identical
    requests within one process share a single upstream call.
    """

    def __init__(
        self,
        inner: LLMClient,
        cache_dir: Path,
        *,
        model_for: Callable[[Tier], str] | None = None,
    ) -> None:
        self.inner = inner
        self.name = f"cached:{inner.name}"
        self._dir = cache_dir
        self._model_for = model_for
        self._locks: dict[str, asyncio.Lock] = {}
        self.hits = 0
        self.misses = 0

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]:
        model = (
            self._model_for(request.tier)
            if self._model_for
            else f"{self.inner.name}:{request.tier}"
        )
        schema_json = json.dumps(schema.model_json_schema(), sort_keys=True)
        key = cache_key(model, request, schema.__name__, schema_json)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await asyncio.to_thread(self._read, key)
            if cached is not None:
                try:
                    result = schema.model_validate(cached["data"])
                except (ValidationError, KeyError, TypeError):
                    logger.debug("ignoring incompatible cache entry %s", key[:12])
                else:
                    self.hits += 1
                    return result, Usage()
            self.misses += 1
            result, usage = await self.inner.generate(request, schema)
            entry = {
                "version": _CACHE_VERSION,
                "schema": schema.__name__,
                "data": result.model_dump(mode="json"),
                "usage": usage.model_dump(mode="json"),
            }
            await asyncio.to_thread(self._write, key, entry)
            return result, usage

    def _path(self, key: str) -> Path:
        return self._dir / key[:2] / f"{key}.json"

    def _read(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("LLM cache read failed: %s", type(exc).__name__)
            return None
        try:
            loaded: Any = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(loaded, dict) or loaded.get("version") != _CACHE_VERSION:  # pyright: ignore[reportUnknownMemberType]
            return None
        return loaded  # pyright: ignore[reportUnknownVariableType]

    def _write(self, key: str, entry: dict[str, Any]) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(entry, handle, ensure_ascii=False)
                Path(tmp).replace(path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("LLM cache write failed: %s", type(exc).__name__)
