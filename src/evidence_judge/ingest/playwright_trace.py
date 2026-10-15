"""Load a Playwright trace (``context.tracing.stop(path=...)`` zip) into a normalised Run.

The format follows ``packages/isomorphic/trace`` in the Playwright repo (traceV10 types,
traceModernizer.ts, snapshotRenderer.ts). Each ``<prefix>.trace`` file is one browser context:
NDJSON events starting with ``context-options``; ``<prefix>.network`` holds HAR entries; body,
snapshot-resource and screencast blobs live in the archive under ``resources/`` and
``screencast/``.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import lzma
import re
import zipfile
import zlib
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import IO, Final, cast

from evidence_judge.core.actions import INJECT, OTHER
from evidence_judge.core.evidence import (
    ActionEvent,
    BlobLoader,
    BlobStore,
    Channel,
    ConsoleEvent,
    NetworkEvent,
    PageSnapshot,
    PageState,
    Run,
    Screenshot,
    is_web_page_url,
)
from evidence_judge.core.provenance import mark_agent_page_navigations
from evidence_judge.ingest._calls import (
    INTERCEPT,
    MOCKED_ROUTE,
    ROUTE_CLASS,
    RawAction,
    RouteCalls,
    action_name,
    action_value,
    is_settle,
    script_sha256,
    taints_run,
    upload_text,
)
from evidence_judge.ingest._css import MAX_SHEET_CHARS
from evidence_judge.ingest._dom import FrameSnapshotStore, SnapshotDecodeError, Viewport
from evidence_judge.ingest._json import (
    JsonObject,
    as_float,
    as_int,
    as_list,
    as_object,
    as_str,
)
from evidence_judge.ingest._network import (
    BodyPrefix,
    RawBody,
    body_prefix_bytes,
    check_max_body_chars,
    inline_body,
    network_event,
    wall_ms,
)
from evidence_judge.ingest.errors import (
    TraceFormatError,
    TraceTooLargeError,
    UnsupportedTraceVersionError,
)

SUPPORTED_TRACE_VERSIONS: Final = frozenset({7, 8, 9, 10})
"""Trace format versions (``context-options.version``) whose event layout this parser reads.

7 and 8 differ from 10 only in fields this parser ignores; 9 and earlier reference blobs by
``sha1``/``_sha1`` instead of ``file``/``_file``, which is handled. Version 10 is written by
Playwright 1.5x-1.6x.
"""

SOURCE: Final = "playwright-trace"

_MIB: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TraceLimits:
    """Size limits checked against the archive's declared sizes before anything is inflated.

    ``zipfile`` never inflates an entry past its declared size (and fails its CRC check if the
    data disagrees), so these bound the memory and time spent on a hostile or runaway trace.
    """

    max_entry_bytes: int = 200 * _MIB
    """Largest ``.trace`` or ``.network`` event file; larger ones fail the load."""
    max_resource_bytes: int = 10 * _MIB
    """Largest screenshot or body resource; larger ones are skipped with a warning."""
    max_total_bytes: int = 1024 * _MIB
    """Largest sum of uncompressed entry sizes; larger archives fail the load."""
    max_compression_ratio: float = 200.0
    """Largest uncompressed/compressed ratio for an entry over 1 MiB (zip bomb guard)."""
    max_entries: int = 100_000
    """Most entries an archive may hold."""


DEFAULT_TRACE_LIMITS: Final = TraceLimits()
_RATIO_EXEMPT_BYTES: Final = _MIB
_ZIP_ERRORS: Final = (
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
    EOFError,
    OSError,
    RuntimeError,
    NotImplementedError,
    ValueError,
)
"""What reading a damaged, encrypted or unsupported zip entry can raise."""


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
    limits: TraceLimits = DEFAULT_TRACE_LIMITS,
) -> Run:
    """Parse a Playwright trace zip.

    Event files are streamed line by line, bodies are read only up to what ``max_body_chars``
    can keep, and screenshots stay in the zip: ``Run.blobs`` reads each one on demand, so the
    trace file must stay in place while the run is judged and reported.

    ``final_state`` is the last snapshot of the main page that shows a page a site served:
    only ``http`` and ``https`` snapshots count (``is_web_page_url``), never ``about:``,
    ``data:``, ``blob:`` or browser-internal URLs. A ``view-source:`` page is not told apart
    here: Chromium reports it under the http URL of the page whose source it shows, so the
    ``goto`` that opens it is an INJECT (``opens_unserved_page``) and evidence from then on is
    not the site's. The main page is the page (``pageId``) of the agent's last
    action that has such a snapshot, so a tab opened after that action (a popup, an ad, a page
    the agent never acted on) is ignored, and a tab the agent did act in last is followed. With
    no such action, it is the first page opened. Its screenshot is the last screencast frame of
    the same page. With no web-page snapshot at all, there is no final state.

    Raises:
        TraceFormatError: the file is not a readable trace (bad zip, no ``.trace`` entry,
            missing ``context-options``, corrupt events).
        TraceTooLargeError: the archive exceeds ``limits``.
        UnsupportedTraceVersionError: the trace format version is not in
            ``SUPPORTED_TRACE_VERSIONS``.
    """
    check_max_body_chars(max_body_chars)
    trace_path = Path(path)
    if not trace_path.is_file():
        raise FileNotFoundError(f"trace not found: {trace_path}")
    try:
        archive = zipfile.ZipFile(trace_path)
    except (*_ZIP_ERRORS, KeyError) as exc:
        raise TraceFormatError(f"{trace_path}: not a readable trace zip ({exc})") from exc
    with archive:
        _check_limits(archive, limits)
        reader = _TraceReader(archive, max_body_chars, limits)
        reader.read_all()
        blobs = _ZipBlobs(trace_path.resolve(), limits.max_resource_bytes)
        return reader.build(run_id or trace_path.stem, blobs)


def _check_limits(archive: zipfile.ZipFile, limits: TraceLimits) -> None:
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise TraceTooLargeError(
            f"{archive.filename}: {len(infos)} entries exceed the limit of {limits.max_entries}"
        )
    total = 0
    for info in infos:
        total += info.file_size
        if _is_event_file(info.filename) and info.file_size > limits.max_entry_bytes:
            raise TraceTooLargeError(
                f"{info.filename}: {info.file_size} bytes uncompressed exceed the limit of "
                f"{limits.max_entry_bytes}"
            )
        ratio = info.file_size / max(info.compress_size, 1)
        if info.file_size > _RATIO_EXEMPT_BYTES and ratio > limits.max_compression_ratio:
            raise TraceTooLargeError(
                f"{info.filename}: compression ratio {ratio:.0f}x exceeds "
                f"{limits.max_compression_ratio:.0f}x; refusing a possible zip bomb"
            )
    if total > limits.max_total_bytes:
        raise TraceTooLargeError(
            f"{archive.filename}: {total} bytes uncompressed exceed the limit of "
            f"{limits.max_total_bytes}"
        )


def _is_event_file(name: str) -> bool:
    return name.endswith((".trace", ".network"))


class _ZipBlobs:
    """Reads blobs from the trace zip on demand; the zip is opened per read, never held open."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self._path = path
        self._max_bytes = max_bytes

    def read(self, name: str) -> bytes | None:
        try:
            with zipfile.ZipFile(self._path) as archive:
                info = archive.getinfo(name)
                if info.file_size > self._max_bytes:
                    return None
                with archive.open(info) as stream:
                    return stream.read()
        except (*_ZIP_ERRORS, KeyError):
            return None


_CallKey = tuple[int, str]


@dataclass(slots=True)
class _RawSnapshot:
    time: float
    page_id: str
    url: str
    title: str | None
    text: str
    call_id: str | None
    field_values: tuple[str, ...] = ()


@dataclass(slots=True)
class _PendingSnapshot:
    """A main-frame snapshot rendered once the context's stylesheets are read."""

    frame_id: str
    index: int
    time: float
    page_id: str
    url: str
    call_id: str | None
    viewport: Viewport | None


@dataclass(slots=True)
class _RawShot:
    time: float
    file: str
    width: int | None
    height: int | None
    page_id: str | None


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
    limits: TraceLimits
    origin: _Origin | None = None
    context: JsonObject = field(default_factory=JsonObject)
    context_index: int = 0
    actions: dict[_CallKey, RawAction] = field(default_factory=dict[_CallKey, RawAction])
    action_urls: dict[_CallKey, str] = field(default_factory=dict[_CallKey, str])
    action_pages: dict[_CallKey, str] = field(default_factory=dict[_CallKey, str])
    page_opened: dict[str, float] = field(default_factory=dict[str, float])
    snapshots: list[_RawSnapshot] = field(default_factory=list[_RawSnapshot])
    shots: list[_RawShot] = field(default_factory=list[_RawShot])
    console: list[_RawConsole] = field(default_factory=list[_RawConsole])
    resources: list[JsonObject] = field(default_factory=list[JsonObject])
    has_network_file: bool = False
    warnings: list[str] = field(default_factory=list[str])
    store: FrameSnapshotStore = field(default_factory=FrameSnapshotStore)
    form_values: int = 0
    routes: RouteCalls = field(default_factory=RouteCalls)
    pending: list[_PendingSnapshot] = field(default_factory=list[_PendingSnapshot])
    css_entries: dict[str, JsonObject] = field(default_factory=dict[str, JsonObject])
    css: dict[str, str | None] = field(default_factory=dict[str, str | None])

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
        with contextlib.closing(self._events(f"{prefix}.trace")) as events:
            browser = self._read_context_events(prefix, events)
        network_name = f"{prefix}.network"
        if browser and network_name in self.archive.namelist():
            self.has_network_file = True
            with contextlib.closing(self._events(network_name)) as events:
                for event in events:
                    self._on_event(event)
        self._render_pending()

    def _read_context_events(self, prefix: str, events: Iterator[JsonObject]) -> bool:
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
            return False
        self._set_origin(first)
        self.context_index += 1
        for event in events:
            self._on_event(event)
        return True

    def _set_origin(self, options: JsonObject) -> None:
        monotonic = as_float(options.get("monotonicTime"))
        if monotonic is None:
            raise TraceFormatError("context-options event has no monotonicTime")
        if self.origin is None or monotonic < self.origin.monotonic:
            self.origin = _Origin(monotonic, as_float(options.get("wallTime")))
            self.context = options

    def _events(self, name: str) -> Generator[JsonObject]:
        """Stream one NDJSON entry. A final line that is not valid JSON was cut off mid-write."""
        try:
            stream = self.archive.open(name)
        except (*_ZIP_ERRORS, KeyError) as exc:
            raise TraceFormatError(f"{name}: cannot read from trace archive ({exc})") from exc
        with stream:
            pending: tuple[int, bytes] | None = None
            for numbered in _lines(name, stream):
                if pending is not None:
                    yield _parse_event(name, *pending)
                pending = numbered
            if pending is None:
                return
            try:
                event = _parse_event(name, *pending)
            except _TruncatedEventError:
                self.warnings.append(f"{name}: ignored truncated final event")
                return
            yield event

    def _on_event(self, event: JsonObject) -> None:
        match event.get("type"):
            case "before":
                self._on_before(event)
            case "after":
                self._on_after(event)
            case "frame-snapshot":
                self._on_frame_snapshot(as_object(event.get("snapshot")))
            case "screencast-frame" | "screenshot":
                self._on_image(event, as_float(event.get("timestamp")))
            case "console":
                self._on_console(event)
            case "event":
                self._on_page_event(event)
            case "error":
                self._add_console(event.get("time"), "error", as_str(event.get("message")))
            case "resource-snapshot":
                self.resources.append(as_object(event.get("snapshot")))
            case _:
                pass

    def _on_page_event(self, event: JsonObject) -> None:
        match event.get("method"):
            case "pageError":
                self._on_page_error(event)
            case "page":
                page_id = as_str(as_object(event.get("params")).get("pageId"))
                time = as_float(event.get("time"))
                if page_id is not None and time is not None:
                    self._page_seen(page_id, time)
            case _:
                pass

    def _on_before(self, event: JsonObject) -> None:
        call_id = as_str(event.get("callId"))
        method = as_str(event.get("method"))
        start = as_float(event.get("startTime"))
        owner = as_str(event.get("class"))
        params = as_object(event.get("params"))
        if call_id is None or method is None or start is None or is_settle(method, params):
            return
        if self._on_route_call(owner, method, params, start):
            return
        taints = owner == ROUTE_CLASS or taints_run(owner, method, params)
        if not taints and action_name(method) is None:
            return
        self.actions[self._key(call_id)] = RawAction(
            call_id, start, method, params, owner=owner, taints=taints
        )
        page_id = as_str(event.get("pageId"))
        if page_id is not None:
            self.action_pages[self._key(call_id)] = page_id

    def _on_route_call(
        self, owner: str | None, method: str, params: JsonObject, start: float
    ) -> bool:
        """Record a route handler's call or registration; False for any other call."""
        if owner == ROUTE_CLASS and self.routes.record(method, params, start):
            return True
        if method == INTERCEPT:
            if as_list(params.get("patterns")):
                self.routes.intercepts.append(start)
            return True
        return False

    def _key(self, call_id: str) -> _CallKey:
        return (self.context_index, call_id)

    def _page_seen(self, page_id: str, time: float) -> None:
        if time < self.page_opened.get(page_id, float("inf")):
            self.page_opened[page_id] = time

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
        page_id = as_str(snapshot.get("pageId")) or frame_id
        if call_id is not None:
            self.action_urls.setdefault(self._key(call_id), url)
            self.action_pages.setdefault(self._key(call_id), page_id)
        time = as_float(snapshot.get("timestamp"))
        if time is None:
            return
        self._page_seen(page_id, time)
        viewport = as_object(snapshot.get("viewport"))
        width, height = as_int(viewport.get("width")), as_int(viewport.get("height"))
        size = (width, height) if width is not None and height is not None else None
        self.pending.append(_PendingSnapshot(frame_id, index, time, page_id, url, call_id, size))

    def _render_pending(self) -> None:
        """Render the context's main-frame snapshots, now that its stylesheets are read."""
        self.css_entries = {}
        self.css = {}
        for entry in self.resources:
            response = as_object(entry.get("response"))
            content = as_object(response.get("content"))
            mime = (as_str(content.get("mimeType")) or "text/css").lower()
            status = as_int(response.get("status")) or 0
            url = as_str(as_object(entry.get("request")).get("url"))
            if url and 200 <= status < 300 and "css" in mime:
                self.css_entries[url] = content
        for raw in self.pending:
            try:
                page = self.store.render(
                    raw.frame_id, raw.index, viewport=raw.viewport, stylesheets=self._stylesheet
                )
            except SnapshotDecodeError as exc:
                self.warnings.append(f"snapshot of {raw.url} skipped: {exc}")
                continue
            self.form_values += page.form_values
            self.snapshots.append(
                _RawSnapshot(
                    raw.time,
                    raw.page_id,
                    raw.url,
                    page.title,
                    page.text,
                    raw.call_id,
                    page.field_values,
                )
            )
        self.pending.clear()

    def _stylesheet(self, url: str) -> str | None:
        """The text of the last stylesheet the context recorded from `url` (up to
        MAX_SHEET_CHARS characters), or None."""
        if url not in self.css:
            content = self.css_entries.get(url)
            self.css[url] = None if content is None else self._css_text(content)
        return self.css[url]

    def _css_text(self, content: JsonObject) -> str | None:
        file = as_str(content.get("_file"))
        sha1 = as_str(content.get("_sha1"))
        name = file or (f"resources/{sha1}" if sha1 else None)
        if name is None:
            body = inline_body(content)
        elif (info := self._resource(name, "stylesheet")) is None:
            return None
        else:
            try:
                with self.archive.open(info) as stream:
                    body = stream.read(MAX_SHEET_CHARS)
            except _ZIP_ERRORS as exc:
                self.warnings.append(f"stylesheet {name} is corrupt: {exc}")
                return None
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="replace")
        return body

    def _on_image(self, event: JsonObject, time: float | None) -> None:
        file = as_str(event.get("file"))
        sha1 = as_str(event.get("sha1"))
        if file is None and sha1 is not None:
            file = f"resources/{sha1}"
        if file is None or time is None:
            return
        width, height = as_int(event.get("width")), as_int(event.get("height"))
        self.shots.append(_RawShot(time, file, width, height, as_str(event.get("pageId"))))

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

    def build(self, run_id: str, blob_source: _ZipBlobs) -> Run:
        assert self.origin is not None
        snapshots, snapshot_pages = self._build_snapshots()
        screenshots, shot_pages, loaders = self._build_screenshots(blob_source)
        final_state = self._final_state(snapshots, snapshot_pages, screenshots, shot_pages)
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
        network = self._build_network()
        run = Run(
            run_id=run_id,
            source=SOURCE,
            actions=self._build_actions(network),
            network=network,
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
            blobs=BlobStore(loaders=loaders),
            metadata=self._metadata(),
        )
        return mark_agent_page_navigations(run)

    def _ts(self, monotonic: float) -> float:
        assert self.origin is not None
        return round(monotonic - self.origin.monotonic, 3)

    def _build_actions(self, network: list[NetworkEvent]) -> list[ActionEvent]:
        """The recorded actions, plus an INJECT for every route call that answered or rewrote a
        request no recorded request is marked synthetic for: a ``route.fallback`` with
        overrides (Playwright marks none), and every overriding call when no request is marked
        (no network file, or a format without the marker). The request it changed started
        before the call was recorded, so the INJECT is placed when the first route handler was
        registered, before any request it could have changed."""
        snapshot_times = [s.time for s in self.snapshots]
        out: list[ActionEvent] = []
        actions = dict(self.actions)
        unmarked = self.routes.unmarked(network, self.resources, self._resource_time)
        for n, route in enumerate(unmarked):
            actions[(-1, f"route{n}")] = RawAction(
                f"route{n}",
                self.routes.registered(route.time),
                MOCKED_ROUTE,
                {"call": route.call},
                taints=True,
            )
        ordered = sorted(actions.items(), key=lambda item: item[1].time)
        for number, (key, raw) in enumerate(ordered, 1):
            url = self.action_urls.get(key)
            if url is None:
                at = bisect.bisect_right(snapshot_times, raw.time) - 1
                url = self.snapshots[at].url if at >= 0 else None
            out.append(
                ActionEvent(
                    id=f"act#{number}",
                    ts=self._ts(raw.time),
                    name=INJECT if raw.taints else action_name(raw.method) or OTHER,
                    selector=as_str(raw.params.get("selector")),
                    value=action_value(raw),
                    url=url,
                    error=raw.error,
                    call_id=raw.call_id,
                    script_sha256=script_sha256(raw.method, raw.params),
                    file_text=upload_text(raw.params) if raw.method == "setInputFiles" else None,
                )
            )
        return out

    def _build_network(self) -> list[NetworkEvent]:
        timed = [(self._resource_time(entry), entry) for entry in self.resources]
        timed.sort(key=lambda pair: pair[0])
        rewritten = self.routes.rewritten([entry for _, entry in timed])
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
            if id(entry) in rewritten:
                event = event.model_copy(update={"synthetic": True})
            out.append(event)
        return out

    def _resource_time(self, entry: JsonObject) -> float:
        assert self.origin is not None
        monotonic = as_float(entry.get("_monotonicTime"))
        if monotonic is not None:
            return monotonic
        started = wall_ms(as_str(entry.get("startedDateTime")))
        if started is not None and self.origin.wall is not None:
            return self.origin.monotonic + (started - self.origin.wall)
        return self.origin.monotonic

    def _body(self, part: JsonObject) -> RawBody | None:
        file = as_str(part.get("_file"))
        sha1 = as_str(part.get("_sha1"))
        name = file or (f"resources/{sha1}" if sha1 else None)
        if name is None:
            return inline_body(part)
        info = self._resource(name, "body resource")
        if info is None:
            return inline_body(part)
        limit = body_prefix_bytes(self.max_body_chars)
        try:
            with self.archive.open(info) as stream:
                data = stream.read(limit + 1)
        except _ZIP_ERRORS as exc:
            self.warnings.append(f"body resource {name} is corrupt: {exc}")
            return inline_body(part)
        if len(data) > limit:
            return BodyPrefix(data[:limit], info.file_size)
        return data

    def _resource(self, name: str, kind: str) -> zipfile.ZipInfo | None:
        try:
            info = self.archive.getinfo(name)
        except KeyError:
            self.warnings.append(f"{kind} {name} missing from trace")
            return None
        if info.file_size > self.limits.max_resource_bytes:
            self.warnings.append(
                f"{kind} {name} is larger than {self.limits.max_resource_bytes} bytes"
            )
            return None
        return info

    def _build_snapshots(self) -> tuple[list[PageSnapshot], list[str]]:
        out: list[PageSnapshot] = []
        pages: list[str] = []
        last_by_page: dict[str, tuple[str, str | None, str]] = {}
        kept: dict[str, int] = {}
        for raw in sorted(self.snapshots, key=lambda s: s.time):
            key = (raw.url, raw.title, raw.text)
            if last_by_page.get(raw.page_id) == key:
                same = out[kept[raw.page_id]]
                values = tuple(dict.fromkeys([*same.field_values, *raw.field_values]))
                out[kept[raw.page_id]] = same.model_copy(update={"field_values": values})
                continue
            last_by_page[raw.page_id] = key
            kept[raw.page_id] = len(out)
            pages.append(raw.page_id)
            out.append(
                PageSnapshot(
                    id=f"snap#{len(out) + 1}",
                    ts=self._ts(raw.time),
                    url=raw.url,
                    title=raw.title,
                    text=raw.text,
                    call_id=raw.call_id,
                    field_values=raw.field_values,
                )
            )
        return out, pages

    def _build_screenshots(
        self, blob_source: _ZipBlobs
    ) -> tuple[list[Screenshot], list[str | None], dict[str, BlobLoader]]:
        shots: list[Screenshot] = []
        pages: list[str | None] = []
        loaders: dict[str, BlobLoader] = {}
        for raw in sorted(self.shots, key=lambda s: s.time):
            if self._resource(raw.file, "screenshot") is None:
                continue
            loaders[raw.file] = partial(blob_source.read, raw.file)
            pages.append(raw.page_id)
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
        return shots, pages, loaders

    def _final_state(
        self,
        snapshots: list[PageSnapshot],
        snapshot_pages: list[str],
        shots: list[Screenshot],
        shot_pages: list[str | None],
    ) -> PageState | None:
        """The last web-page snapshot of the main page; see ``load_playwright_trace``."""
        web = [i for i, s in enumerate(snapshots) if is_web_page_url(s.url)]
        main = self._main_page({snapshot_pages[i] for i in web})
        if main is None:
            return None
        last = snapshots[max(i for i in web if snapshot_pages[i] == main)]
        shot = next(
            (
                s
                for s, p in zip(reversed(shots), reversed(shot_pages), strict=True)
                if p in (None, main)
            ),
            None,
        )
        return PageState(
            url=last.url,
            title=last.title,
            text=last.text,
            snapshot_id=last.id,
            screenshot_id=shot.id if shot else None,
        )

    def _main_page(self, candidates: set[str]) -> str | None:
        if not candidates:
            return None
        for key, raw in sorted(self.actions.items(), key=lambda i: i[1].time, reverse=True):
            if raw.taints:
                continue
            page = self.action_pages.get(key)
            if page in candidates:
                return page
        return min(candidates, key=lambda p: (self.page_opened.get(p, float("inf")), p))

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
        if self.form_values:
            metadata["form_values_excluded"] = self.form_values
        if self.warnings:
            metadata["warnings"] = list(self.warnings)
        return metadata


class _TruncatedEventError(TraceFormatError):
    pass


def _lines(name: str, stream: IO[bytes]) -> Iterator[tuple[int, bytes]]:
    try:
        for number, line in enumerate(stream, 1):
            if line.strip():
                yield number, line
    except _ZIP_ERRORS as exc:
        raise TraceFormatError(f"{name}: cannot read from trace archive ({exc})") from exc


def _parse_event(name: str, number: int, line: bytes) -> JsonObject:
    try:
        event = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise _TruncatedEventError(f"{name}:{number}: invalid JSON event ({exc})") from exc
    except RecursionError as exc:
        raise TraceFormatError(f"{name}:{number}: event nests too deeply") from exc
    if not isinstance(event, dict):
        raise TraceFormatError(f"{name}:{number}: event is not a JSON object")
    return cast(JsonObject, event)


def _trace_prefixes(archive: zipfile.ZipFile) -> list[str]:
    """``.trace`` entry prefixes in context order: ``trace``, ``0-trace``, ``1-trace``, ..."""
    found: list[tuple[int, str]] = []
    for name in archive.namelist():
        match = _TRACE_FILE.match(name)
        if match:
            ordinal = match.group("ordinal")
            found.append((int(ordinal) if ordinal else -1, name.removesuffix(".trace")))
    return [prefix for _, prefix in sorted(found)]
