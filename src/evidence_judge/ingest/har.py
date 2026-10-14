"""Load a HAR 1.2 network log into a Run that carries only network evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from evidence_judge.core.evidence import Channel, NetworkEvent, Run
from evidence_judge.ingest._json import JsonObject, as_list, as_object, as_str, read_capped
from evidence_judge.ingest._network import (
    BodyPrefix,
    RawBody,
    body_prefix_bytes,
    check_max_body_chars,
    inline_body,
    network_event,
    wall_ms,
)
from evidence_judge.ingest.errors import HarFormatError

SOURCE: Final = "har"
MAX_HAR_BYTES: Final = 200 * 1024 * 1024
"""Default cap on a HAR file's size; larger files fail with ``TooLargeError``."""


def load_har(
    path: str | Path,
    *,
    run_id: str | None = None,
    max_body_chars: int = 20_000,
    max_file_bytes: int = MAX_HAR_BYTES,
) -> Run:
    """Parse a HAR file. Event timestamps are milliseconds after the earliest page or entry.

    Raises:
        HarFormatError: the file is not HAR JSON or an entry lacks ``startedDateTime``.
        TooLargeError: the file is larger than ``max_file_bytes``.
    """
    check_max_body_chars(max_body_chars)
    har_path = Path(path)
    log = _read_log(har_path, max_file_bytes)
    entries = [as_object(e) for e in as_list(log.get("entries"))]
    started = [_entry_start(entry, number) for number, entry in enumerate(entries, 1)]
    page_starts = [
        wall_ms(as_str(as_object(p).get("startedDateTime"))) for p in as_list(log.get("pages"))
    ]
    origin = min([*started, *(s for s in page_starts if s is not None)], default=0.0)
    body_source = _BodySource(har_path.parent, body_prefix_bytes(max_body_chars))
    network: list[NetworkEvent] = []
    for start, entry in sorted(zip(started, entries, strict=True), key=lambda pair: pair[0]):
        event = network_event(
            entry,
            event_id=f"net#{len(network) + 1}",
            ts=round(start - origin, 3),
            max_body_chars=max_body_chars,
            body_source=body_source,
        )
        if event is None:
            raise HarFormatError(f"{har_path}: an entry has no request method or URL")
        network.append(event)
    return Run(
        run_id=run_id or har_path.stem,
        source=SOURCE,
        network=network,
        captured={Channel.NETWORK},
        metadata=_metadata(log, origin if entries else None, body_source.warnings),
    )


def _read_log(path: Path, max_bytes: int) -> JsonObject:
    raw = read_capped(path, max_bytes)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise HarFormatError(f"{path}: not valid JSON ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise HarFormatError(f"{path}: not UTF-8 text") from exc
    except RecursionError as exc:
        raise HarFormatError(f"{path}: JSON nests too deeply") from exc
    log = as_object(as_object(data).get("log"))
    if not isinstance(log.get("entries"), list):
        raise HarFormatError(f"{path}: missing log.entries; not a HAR file")
    return log


def _entry_start(entry: JsonObject, number: int) -> float:
    started = wall_ms(as_str(entry.get("startedDateTime")))
    if started is None:
        raise HarFormatError(f"entry {number} has no valid startedDateTime")
    return started


class _BodySource:
    """Inline HAR bodies plus Playwright's ``_file`` attachments stored next to the HAR."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self._root = root.resolve()
        self._max_bytes = max_bytes
        self.warnings: list[str] = []

    def __call__(self, part: JsonObject) -> RawBody | None:
        name = as_str(part.get("_file"))
        if name is None:
            return inline_body(part)
        try:
            target = (self._root / name).resolve()
        except (OSError, RuntimeError, ValueError):
            self.warnings.append(f"ignored an unresolvable body file: {name}")
            return None
        if not target.is_relative_to(self._root):
            self.warnings.append(f"ignored body file outside the HAR directory: {name}")
            return None
        if not target.exists():
            self.warnings.append(f"body file {name} not found next to the HAR")
            return None
        if not target.is_file():
            self.warnings.append(f"body file {name} is not a regular file")
            return None
        try:
            with target.open("rb") as handle:
                data = handle.read(self._max_bytes + 1)
                total = os.fstat(handle.fileno()).st_size
        except OSError:
            self.warnings.append(f"body file {name} is not readable")
            return None
        if len(data) > self._max_bytes:
            return BodyPrefix(data[: self._max_bytes], total)
        return data


def _metadata(log: JsonObject, origin: float | None, warnings: list[str]) -> JsonObject:
    creator = as_object(log.get("creator"))
    browser = as_object(log.get("browser"))
    metadata: JsonObject = {
        "har_version": log.get("version"),
        "creator": " ".join(
            filter(None, [as_str(creator.get("name")), as_str(creator.get("version"))])
        )
        or None,
        "browser": as_str(browser.get("name")),
        "start_wall_time_ms": origin,
    }
    if warnings:
        metadata["warnings"] = list(warnings)
    return metadata
