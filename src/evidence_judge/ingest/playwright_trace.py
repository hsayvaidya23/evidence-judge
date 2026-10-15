"""Load a Playwright trace (``context.tracing.stop(path=...)`` zip) into a normalised Run.

The format follows ``packages/isomorphic/trace`` in the Playwright repo (traceV10 types,
traceModernizer.ts, snapshotRenderer.ts). Each ``<prefix>.trace`` file is one browser context:
NDJSON events starting with ``context-options``; ``<prefix>.network`` holds HAR entries; body,
snapshot-resource and screencast blobs live in the archive under ``resources/`` and
``screencast/``.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import codecs
import contextlib
import json
import lzma
import math
import re
import zipfile
import zlib
from collections.abc import Generator, Iterable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import IO, Final, cast

from evidence_judge.core.actions import INJECT, OTHER, injected, known_action
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
from evidence_judge.core.provenance import mark_agent_page_navigations, script_hash
from evidence_judge.ingest._dom import FrameSnapshotStore, SnapshotDecodeError
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

_OTHER_METHODS: Final = frozenset(
    {
        "dragAndDrop",
        "dispatchEvent",
        "focus",
        "blur",
        "selectText",
        "scrollIntoViewIfNeeded",
        "mouseMove",
        "mouseDown",
        "mouseUp",
        "mouseWheel",
        "keyboardDown",
        "keyboardUp",
        "goBack",
        "goForward",
    }
)
"""Trace methods recorded as user actions without a canonical name of their own."""


_HARMLESS_METHODS: Final = frozenset(
    {
        "newPage",
        "newContext",
        "newContextForReuse",
        "launch",
        "launchPersistentContext",
        "connectOverCDP",
        "connect",
        "close",
        "dispose",
        "disposeAPIResponse",
        "bringToFront",
        "setViewportSize",
        "emulateMedia",
        "pause",
        "updateSubscription",
        "setTestIdAttributeName",
        "registerLocatorHandler",
        "unregisterLocatorHandler",
        "resolveLocatorHandlerNoReply",
        "runBeforeUnload",
        "requestGC",
        "pickLocator",
        "cancelPickLocator",
        "highlight",
        "hideHighlight",
        "reload",
        "dblclick",
        "tap",
        "selectText",
        "waitForSelector",
        "waitForTimeout",
        "waitForElementState",
        "waitForEventInfo",
        "waitForLoadState",
        "waitForURL",
        "waitForNavigation",
        "waitForEvent",
        "__waitInfo__",
        "expect",
        "expectScreenshot",
        "screenshot",
        "pdf",
        "title",
        "content",
        "innerText",
        "innerHTML",
        "textContent",
        "getAttribute",
        "inputValue",
        "isChecked",
        "isDisabled",
        "isEditable",
        "isEnabled",
        "isHidden",
        "isVisible",
        "boundingBox",
        "querySelector",
        "querySelectorAll",
        "queryCount",
        "resolveSelector",
        "ariaSnapshot",
        "ariaSnapshotJSON",
        "snapshotForAI",
        "contentFrame",
        "ownerFrame",
        "frameElement",
        "getProperty",
        "getPropertyList",
        "jsonValue",
        "cookies",
        "storageState",
        "webStorageGetItem",
        "webStorageItems",
        "consoleMessages",
        "pageErrors",
        "clearConsoleMessages",
        "clearPageErrors",
        "requests",
        "response",
        "body",
        "fetchResponseBody",
        "fetchLog",
        "rawRequestHeaders",
        "rawResponseHeaders",
        "httpVersion",
        "securityDetails",
        "serverAddr",
        "sizes",
        "startCSSCoverage",
        "stopCSSCoverage",
        "startJSCoverage",
        "stopJSCoverage",
        "screencastStart",
        "screencastStop",
        "screencastFrameAck",
        "tracingStart",
        "tracingStartChunk",
        "tracingStop",
        "tracingStopChunk",
        "tracingGroup",
        "tracingGroupEnd",
        "harStart",
        "harExport",
        "startTracing",
        "stopTracing",
        "zip",
        "tracingStarted",
        "traceDiscarded",
        "addStackToTracingNoReply",
        "globToRegex",
        "asLocatorDescription",
        "saveAs",
        "saveAsStream",
        "pathAfterFinished",
        "failure",
        "stream",
        "read",
        "cancel",
        "delete",
        "dismiss",
        "accept",
        "dispatchEvent",
    }
)
"""Trace methods that only open, close or arrange pages and contexts, interact the way a user
does, wait, read, or record: what the site sends and what the page shows stay the site's. The
known user actions (`known_action`, `_OTHER_METHODS`) are harmless too. Every other recorded
call taints the run (INJECT): deny by default, so a method this list does not know, or one
Playwright adds later, never passes as harmless."""
_UNTRUSTED_CLASSES: Final = frozenset({"WebSocketRoute", "CDPSession", "BindingCall"})
"""Classes whose every call answers the page or the browser with the agent's data."""
_SETTLE_SCRIPT: Final = "(ms) => new Promise((f) => setTimeout(f, ms))"
"""The script Playwright MCP evaluates to wait between actions (``Tab.waitForTimeout``); with a
number as its argument it only waits, so it is not an injection."""
_UPLOAD_TEXT_BYTES: Final = 64 * 1024
"""How much of each uploaded file's content is read for its text."""
_PRINTABLE_SHARE: Final = 0.9
"""Share of printable characters above which decoded upload bytes count as text."""
_BINARY_RUN = re.compile(rb"[\x20-\x7e]{8,}")
"""A run of printable ASCII in a binary upload, the way ``strings`` finds text in one."""

_INJECTED_CODE: Final[dict[str, tuple[str, str]]] = {
    "evaluateExpression": ("evaluate", "expression"),
    "evaluateExpressionHandle": ("evaluateHandle", "expression"),
    "evalOnSelector": ("$eval", "expression"),
    "evalOnSelectorAll": ("$$eval", "expression"),
    "waitForFunction": ("waitForFunction", "expression"),
    "addInitScript": ("addInitScript", "source"),
    "addScriptTag": ("addScriptTag", "content"),
    "addStyleTag": ("addStyleTag", "content"),
    "setContent": ("setContent", "html"),
    "exposeBinding": ("exposeBinding", "name"),
}
"""Trace methods that run the agent's code in the page or replace its content (INJECT): the
API the agent called, and the parameter holding the code."""
_ROUTE_CLASS: Final = "Route"
_ROUTE_OVERRIDES: Final = ("url", "method", "headers", "postData")
"""``Route.continue`` parameters that rewrite the request the site gets."""
_HARMLESS_ABORTS: Final = frozenset({"image", "font", "media"})
"""Resource types an agent may block (to save bandwidth) without changing what a page says."""
_MOCKED_ROUTE: Final = "Route"
"""Method of the INJECT action recorded for a route call no request is marked for."""
_INTERCEPT: Final = "setNetworkInterceptionPatterns"
"""How ``page.route`` and ``context.route`` register the URLs their handlers see."""


def _action_name(method: str) -> str | None:
    if method in _OTHER_METHODS:
        return OTHER
    return known_action(method)


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
    ``data:``, ``view-source:`` or browser-internal URLs. The main page is the page (``pageId``) of the agent's last
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
class _RawAction:
    call_id: str
    time: float
    method: str
    params: JsonObject
    error: str | None = None
    owner: str | None = None
    """The class whose method it is (Frame, Page, BrowserContext, ...)."""
    taints: bool = False


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
    page_id: str | None


@dataclass(slots=True)
class _RawConsole:
    time: float
    level: str
    text: str


def _entry_url(entry: JsonObject) -> str:
    return as_str(as_object(entry.get("request")).get("url")) or ""


@dataclass(slots=True)
class _RawRoute:
    """A ``Route.fulfill`` or ``Route.continue`` call of the agent's route handler."""

    time: float
    call: str
    overrides: bool


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
    actions: dict[_CallKey, _RawAction] = field(default_factory=dict[_CallKey, _RawAction])
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
    fulfills: list[_RawRoute] = field(default_factory=list["_RawRoute"])
    continues: list[_RawRoute] = field(default_factory=list["_RawRoute"])
    fallbacks: list[_RawRoute] = field(default_factory=list["_RawRoute"])
    aborts: list[float] = field(default_factory=list[float])
    """``route.fallback`` calls with overrides: no recorded request is marked for them."""
    intercepts: list[float] = field(default_factory=list[float])
    """When a route handler was registered (``_INTERCEPT`` with patterns)."""

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
            case "screencast-frame":
                self._on_image(event, as_float(event.get("timestamp")))
            case "screenshot":
                self._on_image(event, as_float(event.get("timestamp")))
            case "console":
                self._on_console(event)
            case "event" if event.get("method") == "pageError":
                self._on_page_error(event)
            case "event" if event.get("method") == "page":
                page_id = as_str(as_object(event.get("params")).get("pageId"))
                time = as_float(event.get("time"))
                if page_id is not None and time is not None:
                    self._page_seen(page_id, time)
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
        owner = as_str(event.get("class"))
        params = as_object(event.get("params"))
        if call_id is None or method is None or start is None or _is_settle(method, params):
            return
        if owner == _ROUTE_CLASS and self._on_route(method, params, start):
            return
        if method == _INTERCEPT:
            if as_list(params.get("patterns")):
                self.intercepts.append(start)
            return
        taints = owner == _ROUTE_CLASS or _taints(owner, method, params)
        if not taints and _action_name(method) is None:
            return
        self.actions[self._key(call_id)] = _RawAction(
            call_id, start, method, params, owner=owner, taints=taints
        )
        page_id = as_str(event.get("pageId"))
        if page_id is not None:
            self.action_pages[self._key(call_id)] = page_id

    def _on_route(self, method: str, params: JsonObject, start: float) -> bool:
        """Record a ``Route.fulfill``, ``Route.continue`` or ``Route.abort``; False for any other
        Route call (redirectNavigationRequest), which is an action that taints the run."""
        if method == "abort":
            self.aborts.append(start)
            return True
        if method == "fulfill":
            body = as_str(params.get("body")) or ""
            self.fulfills.append(_RawRoute(start, f"route.fulfill({body[:80]})", overrides=True))
            return True
        if method != "continue":
            return False
        changed = [k for k in _ROUTE_OVERRIDES if params.get(k) not in (None, [], "")]
        if params.get("isFallback") is not True:
            call = f"route.continue({', '.join(changed)})"
            self.continues.append(_RawRoute(start, call, overrides=bool(changed)))
        elif changed:
            call = f"route.fallback({', '.join(changed)})"
            self.fallbacks.append(_RawRoute(start, call, overrides=True))
        return True

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
        try:
            page = self.store.render(frame_id, index)
        except SnapshotDecodeError as exc:
            self.warnings.append(f"snapshot of {url} skipped: {exc}")
            return
        self.form_values += page.form_values
        self.snapshots.append(_RawSnapshot(time, page_id, url, page.title, page.text, call_id))

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
        unmarked = [*self.fallbacks, *self._content_aborts()]
        if not any(n.synthetic for n in network):
            unmarked += [r for r in [*self.fulfills, *self.continues] if r.overrides]
        for n, route in enumerate(unmarked):
            registered = min((t for t in self.intercepts if t <= route.time), default=route.time)
            actions[(-1, f"route{n}")] = _RawAction(
                f"route{n}", registered, _MOCKED_ROUTE, {"call": route.call}, taints=True
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
                    name=INJECT if raw.taints else _action_name(raw.method) or OTHER,
                    selector=as_str(raw.params.get("selector")),
                    value=_action_value(raw),
                    url=url,
                    error=raw.error,
                    call_id=raw.call_id,
                    script_sha256=_script_sha256(raw.method, raw.params),
                    file_text=_upload_text(raw.params) if raw.method == "setInputFiles" else None,
                )
            )
        return out

    def _build_network(self) -> list[NetworkEvent]:
        timed = [(self._resource_time(entry), entry) for entry in self.resources]
        timed.sort(key=lambda pair: pair[0])
        rewritten = self._rewritten([entry for _, entry in timed])
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

    def _content_aborts(self) -> list[_RawRoute]:
        """The ``Route.abort`` calls that could change what a page shows. Blocking an image, a
        font or media (to save bandwidth) cannot; blocking a document, script, stylesheet or API
        call can (the page falls back to text the agent chose to leave). Aborted entries
        (``_wasAborted``) pair, in order, with the calls; when the counts differ, every call
        counts."""
        aborted = sorted(
            (e for e in self.resources if e.get("_wasAborted") is True), key=self._resource_time
        )
        calls = sorted(self.aborts)
        if len(calls) != len(aborted):
            return [_RawRoute(t, "route.abort()", overrides=True) for t in calls]
        return [
            _RawRoute(t, f"route.abort({kind} {_entry_url(e)[:80]})", overrides=True)
            for t, e in zip(calls, aborted, strict=True)
            if (kind := as_str(e.get("_resourceType")) or "unknown") not in _HARMLESS_ABORTS
        ]

    def _rewritten(self, entries: list[JsonObject]) -> set[int]:
        """Ids of the entries the agent's ``route.continue`` rewrote. The continued entries
        (``_wasContinued``) pair, in order, with the ``Route.continue`` calls; when the counts
        differ, every continued entry counts as rewritten if any call had overrides."""
        continued = [e for e in entries if e.get("_wasContinued") is True]
        calls = sorted(self.continues, key=lambda r: r.time)
        if len(calls) != len(continued):
            return {id(e) for e in continued} if any(r.overrides for r in calls) else set()
        return {id(e) for e, r in zip(continued, calls, strict=True) if r.overrides}

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
        for raw in sorted(self.snapshots, key=lambda s: s.time):
            key = (raw.url, raw.title, raw.text)
            if last_by_page.get(raw.page_id) == key:
                continue
            last_by_page[raw.page_id] = key
            pages.append(raw.page_id)
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


def _taints(owner: str | None, method: str, params: JsonObject) -> bool:
    """Whether a recorded call can change what the site sends or what the page shows or
    stores: deny by default (see _HARMLESS_METHODS)."""
    if method in _INJECTED_CODE or owner in _UNTRUSTED_CLASSES:
        return True
    if method == "dispatchEvent":
        return _carries_value(as_object(params.get("eventInit")))
    if method == "accept":
        return bool(as_str(params.get("promptText")))
    known = _action_name(method)
    if known is not None:
        return known == INJECT
    return method not in _HARMLESS_METHODS


def _carries_value(arg: JsonObject) -> bool:
    """A serialised argument other than undefined, null or an empty object."""
    value = as_object(arg.get("value"))
    return value not in ({}, {"v": "undefined"}, {"v": "null"}, {"o": []}) or bool(
        as_list(arg.get("handles"))
    )


def _is_settle(method: str, params: JsonObject) -> bool:
    """``_SETTLE_SCRIPT`` evaluated with one finite number and no handles: Playwright MCP
    waiting. Any other text, argument or call is an injection."""
    if method != "evaluateExpression":
        return False
    if (as_str(params.get("expression")) or "").strip() != _SETTLE_SCRIPT:
        return False
    arg = as_object(params.get("arg"))
    value = as_object(arg.get("value"))
    number = value.get("n")
    return (
        set(value) == {"n"}
        and isinstance(number, int | float)
        and not isinstance(number, bool)
        and math.isfinite(number)
        and not as_list(arg.get("handles"))
    )


def _script_sha256(method: str, params: JsonObject) -> str | None:
    """`script_hash` of an injected script whose code the trace holds; None for any other
    call (an exposed binding runs the agent's own process, never vouched for)."""
    if method not in _INJECTED_CODE or method == "exposeBinding":
        return None
    api, key = _INJECTED_CODE[method]
    code = as_str(params.get(key))
    if code is None:
        return None
    arg = as_object(params.get("arg")).get("value") if "arg" in params else None
    return script_hash(api, code, arg)


def _call_summary(params: JsonObject) -> str:
    """A tainting call's parameters as ``name=value`` pairs, for its INJECT action's value."""
    parts: list[str] = []
    for name, value in params.items():
        if name == "timeout":
            continue
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{name}={shown}")
    return ", ".join(parts)


def _upload_text(params: JsonObject) -> str | None:
    """The text of the files a ``setInputFiles`` call sent with their content (``payloads``):
    the first _UPLOAD_TEXT_BYTES of each, decoded; a binary file contributes its runs of
    printable ASCII. Files sent by path or stream are not in the trace."""
    texts: list[str] = []
    for payload in as_list(params.get("payloads")):
        encoded = as_str(as_object(payload).get("buffer"))
        if not encoded:
            continue
        prefix = encoded[: (_UPLOAD_TEXT_BYTES // 3 + 1) * 4]
        try:
            data = base64.b64decode(prefix)[:_UPLOAD_TEXT_BYTES]
        except (binascii.Error, ValueError):
            continue
        if text := _decoded_text(data):
            texts.append(text)
    return "\n".join(texts) or None


def _decoded_text(data: bytes) -> str:
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(encoding, errors="ignore")
    candidates = [data.decode("utf-8", errors="ignore")]
    if data.count(0) * 4 > len(data):
        candidates += [data.decode(e, errors="ignore") for e in ("utf-16-le", "utf-16-be")]
    for text in candidates:
        printable = sum(c.isprintable() or c in "\t\n\r" for c in text)
        if text and printable >= _PRINTABLE_SHARE * len(text):
            return text
    return "\n".join(run.decode("ascii") for run in _BINARY_RUN.findall(data))


def _action_value(raw: _RawAction) -> str | None:
    method, params = raw.method, raw.params
    if method in _INJECTED_CODE:
        api, key = _INJECTED_CODE[method]
        code = as_str(params.get(key)) or as_str(params.get("url"))
        return injected(api, code)
    if raw.taints and method != _MOCKED_ROUTE:
        api = f"{raw.owner}.{method}" if raw.owner else method
        return injected(api, _call_summary(params))
    match method:
        case "Route":
            return as_str(params.get("call"))
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
