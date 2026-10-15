"""Reading untrusted JSON Lines with size caps: a bad line is counted, never fatal."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, cast

from evidence_judge.ingest._json import JsonObject, as_str

MAX_FILE_BYTES: Final = 512 * 1024 * 1024
MAX_LINE_BYTES: Final = 64 * 1024 * 1024


@dataclass(slots=True)
class LineStats:
    lines: int = 0
    malformed: int = 0
    oversized: int = 0
    not_objects: int = 0
    capped: bool = False
    skipped_types: dict[str, int] = field(default_factory=dict[str, int])

    def skip(self, kind: str) -> None:
        self.skipped_types[kind] = self.skipped_types.get(kind, 0) + 1

    @property
    def damaged(self) -> bool:
        return bool(self.malformed or self.oversized or self.capped)

    def notes(self) -> list[str]:
        out: list[str] = []
        if self.malformed:
            out.append(f"{self.malformed} line(s) were not valid JSON and were skipped")
        if self.oversized:
            out.append(f"{self.oversized} line(s) over {MAX_LINE_BYTES} bytes were skipped")
        if self.not_objects:
            out.append(f"{self.not_objects} line(s) were not JSON objects and were skipped")
        if self.capped:
            out.append(f"the file is over {MAX_FILE_BYTES} bytes; only the start was read")
        return out


def iter_records(path: Path, stats: LineStats) -> Iterator[tuple[bytes, JsonObject]]:
    """Yield (raw line without newline, decoded object) for every readable line of `path`."""
    read = 0
    with path.open("rb") as handle:
        for raw in handle:
            read += len(raw)
            if read > MAX_FILE_BYTES:
                stats.capped = True
                return
            stats.lines += 1
            line = raw.rstrip(b"\r\n")
            if not line.strip():
                continue
            if len(line) > MAX_LINE_BYTES:
                stats.oversized += 1
                continue
            try:
                value: object = json.loads(line)
            except (ValueError, RecursionError):
                stats.malformed += 1
                continue
            if not isinstance(value, dict):
                stats.not_objects += 1
                continue
            yield line, cast(JsonObject, value)


def parse_ts(value: object) -> datetime | None:
    text = as_str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
