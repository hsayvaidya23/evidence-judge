"""Load a HAR 1.2 network log into a Run that carries only network evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from evidence_judge.core.evidence import Channel, NetworkEvent, Run
from evidence_judge.ingest._json import JsonObject, as_list, as_object, as_str
from evidence_judge.ingest._network import (
    check_max_body_chars,
    inline_body,
    network_event,
    wall_ms,
)
from evidence_judge.ingest.errors import HarFormatError

SOURCE: Final = "har"


def load_har(
    path: str | Path,
    *,
    run_id: str | None = None,
    max_body_chars: int = 20_000,
) -> Run:
    """Parse a HAR file. Event timestamps are milliseconds after the earliest page or entry.

    Raises:
        HarFormatError: the file is not HAR JSON or an entry lacks ``startedDateTime``.
    """
    check_max_body_chars(max_body_chars)
    har_path = Path(path)
    log = _read_log(har_path)
    entries = [as_object(e) for e in as_list(log.get("entries"))]
    started = [_entry_start(entry, number) for number, entry in enumerate(entries, 1)]
    page_starts = [
        wall_ms(as_str(as_object(p).get("startedDateTime"))) for p in as_list(log.get("pages"))
    ]
    origin = min([*started, *(s for s in page_starts if s is not None)], default=0.0)
    body_source = _BodySource(har_path.parent)
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


def _read_log(path: Path) -> JsonObject:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise HarFormatError(f"{path}: not valid JSON ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise HarFormatError(f"{path}: not UTF-8 text") from exc
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

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self.warnings: list[str] = []

    def __call__(self, part: JsonObject) -> bytes | str | None:
        name = as_str(part.get("_file"))
        if name is None:
            return inline_body(part)
        target = (self._root / name).resolve()
        if not target.is_relative_to(self._root):
            self.warnings.append(f"ignored body file outside the HAR directory: {name}")
            return None
        try:
            return target.read_bytes()
        except OSError:
            self.warnings.append(f"body file {name} not found next to the HAR")
            return None


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
