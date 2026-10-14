"""Typed accessors for untrusted JSON decoded from recorded runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from evidence_judge.ingest.errors import TooLargeError

JsonObject = dict[str, Any]


def read_capped(path: Path, max_bytes: int) -> bytes:
    """Read a whole file, refusing it (without reading past the cap) when over ``max_bytes``."""
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise TooLargeError(
            f"{path}: file is larger than the {max_bytes}-byte limit; pass a larger "
            "max_file_bytes to load it"
        )
    return data


def as_object(value: object) -> JsonObject:
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)
