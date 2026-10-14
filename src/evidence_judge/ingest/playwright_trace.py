"""Load a Playwright trace (``context.tracing.stop(path=...)`` zip) into a normalised Run.

The format follows ``packages/isomorphic/trace`` in the Playwright repo (traceV10 types,
traceModernizer.ts, snapshotRenderer.ts). Each ``<prefix>.trace`` file is one browser context:
NDJSON events starting with ``context-options``; ``<prefix>.network`` holds HAR entries; body,
snapshot-resource and screencast blobs live in the archive under ``resources/`` and
``screencast/``.
"""

from __future__ import annotations

import bisect
import json
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final, cast

from evidence_judge.core.evidence import (
    ActionEvent,
    Channel,
    ConsoleEvent,
    NetworkEvent,
    PageSnapshot,
    PageState,
    Run,
    Screenshot,
)
from evidence_judge.ingest._dom import FrameSnapshotStore, SnapshotDecodeError
from evidence_judge.ingest._json import (
    JsonObject,
    as_float,
    as_int,
    as_list,
    as_object,
    as_str,
)
from evidence_judge.ingest._network import check_max_body_chars, inline_body, network_event
from evidence_judge.ingest.errors import TraceFormatError, UnsupportedTraceVersionError

SUPPORTED_TRACE_VERSIONS: Final = frozenset({7, 8, 9, 10})
"""Trace format versions (``context-options.version``) whose event layout this parser reads.

7 and 8 differ from 10 only in fields this parser ignores; 9 and earlier reference blobs by
``sha1``/``_sha1`` instead of ``file``/``_file``, which is handled. Version 10 is written by
Playwright 1.5x-1.6x.
"""

SOURCE: Final = "playwright-trace"

_ACTION_NAMES: Final[dict[str, str]] = {
    "goto": "goto",
    "click": "click",
    "dblclick": "click",
    "tap": "click",
    "mouseClick": "click",
    "touchscreenTap": "click",
    "fill": "fill",
    "press": "press",
    "keyboardPress": "press",
    "type": "type",
    "keyboardType": "type",
    "keyboardInsertText": "type",
    "selectOption": "select",
    "check": "check",
    "uncheck": "uncheck",
    "hover": "hover",
    "setInputFiles": "upload",
    "dragAndDrop": "other",
    "dispatchEvent": "other",
    "focus": "other",
    "blur": "other",
    "selectText": "other",
    "scrollIntoViewIfNeeded": "other",
    "mouseMove": "other",
    "mouseDown": "other",
    "mouseUp": "other",
    "mouseWheel": "other",
    "keyboardDown": "other",
    "keyboardUp": "other",
    "goBack": "other",
    "goForward": "other",
    "reload": "other",
}
_TRACE_FILE = re.compile(r"^(?:(?P<ordinal>\d+)-)?(?P<stem>[^/]*)\.trace$")
_IMAGE_TYPES: Final[dict[str, str]] = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def load_playwright_trace(
    path: str | Path,
    *,
    run_id: str | None = None,
    max_body_chars: int = 20_000,
) -> Run:
    """Parse a Playwright trace zip.

    Raises:
        TraceFormatError: the file is not a readable trace (bad zip, no ``.trace`` entry,
            missing ``context-options``, corrupt events).
        UnsupportedTraceVersionError: the trace format version is not in
            ``SUPPORTED_TRACE_VERSIONS``.
    """
    check_max_body_chars(max_body_chars)
    trace_path = Path(path)
    if not trace_path.is_file():
        raise FileNotFoundError(f"trace not found: {trace_path}")
    try:
        archive = zipfile.ZipFile(trace_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise TraceFormatError(f"{trace_path}: not a readable trace zip ({exc})") from exc
    with archive:
        reader = _TraceReader(archive, max_body_chars)
        reader.read_all()
        return reader.build(run_id or trace_path.stem)


_CallKey = tuple[int, str]


@dataclass(slots=True)
class _RawAction:
    call_id: str
    time: float
    method: str
    params: JsonObject
    error: str | None = None


@dataclass(slots=True)
class _RawSnapshot:
    time: float
    page_id: str
    url: str
    title: str | None
    text: str
    call_id: str | None


@dataclass(slots=True)
class _RawShot:
    time: float
    file: str
    width: int | None
    height: int | None


@dataclass(slots=True)
class _RawConsole:
    time: float
    level: str
    text: str


@dataclass(slots=True)
class _Origin:
    monotonic: float
    wall: float | None


@dataclass(slots=True)
class _TraceReader:
    archive: zipfile.ZipFile
    max_body_chars: int
    origin: _Origin | None = None
    context: JsonObject = field(default_factory=JsonObject)
    context_index: int = 0
    actions: dict[_CallKey, _RawAction] = field(default_factory=dict[_CallKey, _RawAction])
    action_urls: dict[_CallKey, str] = field(default_factory=dict[_CallKey, str])
    snapshots: list[_RawSnapshot] = field(default_factory=list[_RawSnapshot])
    shots: list[_RawShot] = field(default_factory=list[_RawShot])
    console: list[_RawConsole] = field(default_factory=list[_RawConsole])
    resources: list[JsonObject] = field(default_factory=list[JsonObject])
    has_network_file: bool = False
    warnings: list[str] = field(default_factory=list[str])
    store: FrameSnapshotStore = field(default_factory=FrameSnapshotStore)

    def read_all(self) -> None:
        prefixes = _trace_prefixes(self.archive)
        if not prefixes:
            raise TraceFormatError(
                f"{self.archive.filename}: no .trace entry; not a Playwright trace"
            )
        for prefix in prefixes:
            self._read_context(prefix)
        if self.origin is None:
            raise TraceFormatError(
                f"{self.archive.filename}: no browser context found (only test-runner traces)"
            )

    def _read_context(self, prefix: str) -> None:
        events = self._events(f"{prefix}.trace")
        first = next(events, None)
        if first is None or first.get("type") != "context-options":
            got = "no events" if first is None else f"a {first.get('type')!r} event"
            raise TraceFormatError(
                f"{prefix}.trace: expected a context-options event first, found {got}; "
                "the trace is truncated or predates the supported format"
            )
        version = first.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version not in SUPPORTED_TRACE_VERSIONS
        ):
            raise UnsupportedTraceVersionError(version, SUPPORTED_TRACE_VERSIONS)
        if first.get("origin") == "testRunner":
            return
        self._set_origin(first)
        self.context_index += 1
        for event in events:
            self._on_event(event)
        network_name = f"{prefix}.network"
        if network_name in self.archive.namelist():
            self.has_network_file = True
            for event in self._events(network_name):
                self._on_event(event)

    def _set_origin(self, options: JsonObject) -> None:
        monotonic = as_float(options.get("monotonicTime"))
        if monotonic is None:
            raise TraceFormatError("context-options event has no monotonicTime")
        if self.origin is None or monotonic < self.origin.monotonic:
            self.origin = _Origin(monotonic, as_float(options.get("wallTime")))
            self.context = options

    def _events(self, name: str) -> Iterator[JsonObject]:
        try:
            raw = self.archive.read(name)
        except (KeyError, zipfile.BadZipFile, OSError) as exc:
            raise TraceFormatError(f"{name}: cannot read from trace archive ({exc})") from exc
        lines = raw.decode("utf-8", errors="replace").splitlines()
        last = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
        for number, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if number == last:
                    self.warnings.append(f"{name}: ignored truncated final event")
                    return
                raise TraceFormatError(f"{name}:{number + 1}: invalid JSON event ({exc})") from exc
            if not isinstance(event, dict):
                raise TraceFormatError(f"{name}:{number + 1}: event is not a JSON object")
            yield cast(JsonObject, event)

    def _on_event(self, event: JsonObject) -> None:
        match event.get("type"):
            case "before":
                self._on_before(event)
            case "after":
                self._on_after(event)
            case "frame-snapshot":
                self._on_frame_snapshot(as_object(event.get("snapshot")))
            case "screencast-frame":
                self._on_image(event, as_float(event.get("timestamp")))
            case "screenshot":
                self._on_image(event, as_float(event.get("timestamp")))
            case "console":
                self._on_console(event)
            case "event" if event.get("method") == "pageError":
                self._on_page_error(event)
            case "error":
                self._add_console(event.get("time"), "error", as_str(event.get("message")))
            case "resource-snapshot":
                self.resources.append(as_object(event.get("snapshot")))
            case _:
                pass

    def _on_before(self, event: JsonObject) -> None:
        call_id = as_str(event.get("callId"))
        method = as_str(event.get("method"))
        start = as_float(event.get("startTime"))
        if call_id is None or method not in _ACTION_NAMES or start is None:
            return
        params = as_object(event.get("params"))
        self.actions[self._key(call_id)] = _RawAction(call_id, start, method, params)

    def _key(self, call_id: str) -> _CallKey:
        return (self.context_index, call_id)

    def _on_after(self, event: JsonObject) -> None:
        action = self.actions.get(self._key(as_str(event.get("callId")) or ""))
        error = as_object(event.get("error"))
        if action is None or not error:
            return
        nested = as_object(error.get("error"))
        action.error = as_str(error.get("message")) or as_str(nested.get("message")) or "error"

    def _on_frame_snapshot(self, snapshot: JsonObject) -> None:
        frame_id = as_str(snapshot.get("frameId"))
        if frame_id is None:
            return
        index = self.store.add(frame_id, snapshot.get("html"))
        if snapshot.get("isMainFrame") is not True:
            return
        url = as_str(snapshot.get("frameUrl")) or ""
        call_id = as_str(snapshot.get("callId"))
        if call_id is not None:
            self.action_urls.setdefault(self._key(call_id), url)
        time = as_float(snapshot.get("timestamp"))
        if time is None:
            return
        try:
            page = self.store.render(frame_id, index)
        except SnapshotDecodeError as exc:
            self.warnings.append(f"snapshot of {url} skipped: {exc}")
            return
        page_id = as_str(snapshot.get("pageId")) or frame_id
        self.snapshots.append(_RawSnapshot(time, page_id, url, page.title, page.text, call_id))

    def _on_image(self, event: JsonObject, time: float | None) -> None:
        file = as_str(event.get("file"))
        sha1 = as_str(event.get("sha1"))
        if file is None and sha1 is not None:
            file = f"resources/{sha1}"
        if file is None or time is None:
            return
        width, height = as_int(event.get("width")), as_int(event.get("height"))
        self.shots.append(_RawShot(time, file, width, height))

    def _on_console(self, event: JsonObject) -> None:
        level = as_str(event.get("messageType")) or "log"
        self._add_console(event.get("time"), level, as_str(event.get("text")))

    def _on_page_error(self, event: JsonObject) -> None:
        error = as_object(as_object(event.get("params")).get("error"))
        message = as_str(as_object(error.get("error")).get("message"))
        self._add_console(event.get("time"), "error", message or as_str(error.get("value")))

    def _add_console(self, time: object, level: str, text: str | None) -> None:
        when = as_float(time)
        if when is not None and text is not None:
            self.console.append(_RawConsole(when, level, text))

    def build(self, run_id: str) -> Run:
        assert self.origin is not None
        snapshots = self._build_snapshots()
        screenshots, blobs = self._build_screenshots()
        final_state = _final_state(snapshots, screenshots)
        initial = snapshots[0] if snapshots else None
        captured = {Channel.ACTIONS, Channel.CONSOLE}
        if self.has_network_file:
            captured.add(Channel.NETWORK)
        if snapshots:
            captured.add(Channel.SNAPSHOTS)
        if screenshots:
            captured.add(Channel.SCREENSHOTS)
        if final_state is not None:
            captured.add(Channel.FINAL_STATE)
        return Run(
            run_id=run_id,
            source=SOURCE,
            actions=self._build_actions(),
            network=self._build_network(),
            snapshots=snapshots,
            screenshots=screenshots,
            console=self._build_console(),
            initial_state=PageState(
                url=initial.url, title=initial.title, text=initial.text, snapshot_id=initial.id
            )
            if initial
            else None,
            final_state=final_state,
            captured=captured,
            blobs=blobs,
            metadata=self._metadata(),
        )

    def _ts(self, monotonic: float) -> float:
        assert self.origin is not None
        return round(monotonic - self.origin.monotonic, 3)

    def _build_actions(self) -> list[ActionEvent]:
        snapshot_times = [s.time for s in self.snapshots]
        out: list[ActionEvent] = []
        ordered = sorted(self.actions.items(), key=lambda item: item[1].time)
        for number, (key, raw) in enumerate(ordered, 1):
            url = self.action_urls.get(key)
            if url is None:
                at = bisect.bisect_right(snapshot_times, raw.time) - 1
                url = self.snapshots[at].url if at >= 0 else None
            out.append(
                ActionEvent(
                    id=f"act#{number}",
                    ts=self._ts(raw.time),
                    name=_ACTION_NAMES[raw.method],
                    selector=as_str(raw.params.get("selector")),
                    value=_action_value(raw.method, raw.params),
                    url=url,
                    error=raw.error,
                    call_id=raw.call_id,
                )
            )
        return out

    def _build_network(self) -> list[NetworkEvent]:
        timed = [(self._resource_time(entry), entry) for entry in self.resources]
        timed.sort(key=lambda pair: pair[0])
        out: list[NetworkEvent] = []
        for time, entry in timed:
            event = network_event(
                entry,
                event_id=f"net#{len(out) + 1}",
                ts=self._ts(time),
                max_body_chars=self.max_body_chars,
                body_source=self._body,
            )
            if event is None:
                self.warnings.append("skipped a network entry without method or URL")
                continue
            out.append(event)
        return out

    def _resource_time(self, entry: JsonObject) -> float:
        assert self.origin is not None
        monotonic = as_float(entry.get("_monotonicTime"))
        if monotonic is not None:
            return monotonic
        started = _wall_ms(as_str(entry.get("startedDateTime")))
        if started is not None and self.origin.wall is not None:
            return self.origin.monotonic + (started - self.origin.wall)
        return self.origin.monotonic

    def _body(self, part: JsonObject) -> bytes | str | None:
        file = as_str(part.get("_file"))
        sha1 = as_str(part.get("_sha1"))
        name = file or (f"resources/{sha1}" if sha1 else None)
        if name is None:
            return inline_body(part)
        try:
            return self.archive.read(name)
        except KeyError:
            self.warnings.append(f"body resource {name} missing from trace")
            return inline_body(part)

    def _build_snapshots(self) -> list[PageSnapshot]:
        out: list[PageSnapshot] = []
        last_by_page: dict[str, tuple[str, str | None, str]] = {}
        for raw in sorted(self.snapshots, key=lambda s: s.time):
            key = (raw.url, raw.title, raw.text)
            if last_by_page.get(raw.page_id) == key:
                continue
            last_by_page[raw.page_id] = key
            out.append(
                PageSnapshot(
                    id=f"snap#{len(out) + 1}",
                    ts=self._ts(raw.time),
                    url=raw.url,
                    title=raw.title,
                    text=raw.text,
                    call_id=raw.call_id,
                )
            )
        return out

    def _build_screenshots(self) -> tuple[list[Screenshot], dict[str, bytes]]:
        shots: list[Screenshot] = []
        blobs: dict[str, bytes] = {}
        for raw in sorted(self.shots, key=lambda s: s.time):
            try:
                blobs[raw.file] = self.archive.read(raw.file)
            except KeyError:
                self.warnings.append(f"screenshot {raw.file} missing from trace")
                continue
            shots.append(
                Screenshot(
                    id=f"shot#{len(shots) + 1}",
                    ts=self._ts(raw.time),
                    mime_type=_IMAGE_TYPES.get(
                        PurePosixPath(raw.file).suffix.lower(), "image/jpeg"
                    ),
                    width=raw.width,
                    height=raw.height,
                    resource=raw.file,
                )
            )
        return shots, blobs

    def _build_console(self) -> list[ConsoleEvent]:
        ordered = sorted(self.console, key=lambda c: c.time)
        return [
            ConsoleEvent(id=f"con#{n}", ts=self._ts(c.time), level=c.level, text=c.text)
            for n, c in enumerate(ordered, 1)
        ]

    def _metadata(self) -> JsonObject:
        assert self.origin is not None
        options = as_object(self.context.get("options"))
        metadata: JsonObject = {
            "trace_version": self.context.get("version"),
            "playwright_version": self.context.get("playwrightVersion"),
            "browser": self.context.get("browserName"),
            "platform": self.context.get("platform"),
            "viewport": options.get("viewport"),
            "start_wall_time_ms": self.origin.wall,
        }
        if self.warnings:
            metadata["warnings"] = list(self.warnings)
        return metadata


def _trace_prefixes(archive: zipfile.ZipFile) -> list[str]:
    """``.trace`` entry prefixes in context order: ``trace``, ``0-trace``, ``1-trace``, ..."""
    found: list[tuple[int, str]] = []
    for name in archive.namelist():
        match = _TRACE_FILE.match(name)
        if match:
            ordinal = match.group("ordinal")
            found.append((int(ordinal) if ordinal else -1, name.removesuffix(".trace")))
    return [prefix for _, prefix in sorted(found)]


def _action_value(method: str, params: JsonObject) -> str | None:
    match method:
        case "goto":
            return as_str(params.get("url"))
        case "fill":
            return as_str(params.get("value"))
        case "type" | "keyboardType" | "keyboardInsertText":
            return as_str(params.get("text"))
        case "press" | "keyboardPress":
            return as_str(params.get("key"))
        case "selectOption":
            return _joined(_option_label(as_object(o)) for o in as_list(params.get("options")))
        case "setInputFiles":
            names = [as_str(as_object(p).get("name")) for p in as_list(params.get("payloads"))]
            paths = [as_str(p) for p in as_list(params.get("localPaths"))]
            return _joined([*names, *(PurePosixPath(p).name for p in paths if p)])
        case _:
            return None


def _option_label(option: JsonObject) -> str | None:
    for key in ("valueOrLabel", "value", "label"):
        if (text := as_str(option.get(key))) is not None:
            return text
    index = as_int(option.get("index"))
    return f"#{index}" if index is not None else None


def _joined(values: Iterable[str | None]) -> str | None:
    present = [v for v in values if v]
    return ", ".join(present) if present else None


def _final_state(snapshots: list[PageSnapshot], shots: list[Screenshot]) -> PageState | None:
    if not snapshots:
        return None
    last = snapshots[-1]
    return PageState(
        url=last.url,
        title=last.title,
        text=last.text,
        snapshot_id=last.id,
        screenshot_id=shots[-1].id if shots else None,
    )


def _wall_ms(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).timestamp() * 1000
    except ValueError:
        return None
