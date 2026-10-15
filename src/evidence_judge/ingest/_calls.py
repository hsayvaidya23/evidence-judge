"""Recorded Playwright calls: which are user actions, which taint the run (INJECT) and what
value each records, and the agent's route handler calls (``route.fulfill``, ``continue``,
``fallback``, ``abort``) reconciled with the requests they changed."""

from __future__ import annotations

import base64
import binascii
import codecs
import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final

from evidence_judge.core.actions import INJECT, OTHER, injected, known_action
from evidence_judge.core.evidence import NetworkEvent
from evidence_judge.core.provenance import is_script_url, opens_unserved_page, script_hash
from evidence_judge.ingest._json import JsonObject, as_int, as_list, as_object, as_str

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
_CAPTURES: Final = frozenset({"screenshot", "expectScreenshot"})
_RESTYLING: Final = ("style", "mask", "maskColor")
"""Screenshot options that change what the page shows while it is captured (a stylesheet, boxes
over elements): the screencast and snapshots taken meanwhile record the agent's page."""
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
ROUTE_CLASS: Final = "Route"
_ROUTE_OVERRIDES: Final = ("url", "method", "headers", "postData")
"""``Route.continue`` parameters that rewrite the request the site gets."""
_HARMLESS_ABORTS: Final = frozenset({"image", "font", "media"})
"""Resource types an agent may block (to save bandwidth) without changing what a page says."""
MOCKED_ROUTE: Final = "Route"
"""Method of the INJECT action recorded for a route call no request is marked for."""
INTERCEPT: Final = "setNetworkInterceptionPatterns"
"""How ``page.route`` and ``context.route`` register the URLs their handlers see."""


@dataclass(slots=True)
class RawAction:
    call_id: str
    time: float
    method: str
    params: JsonObject
    error: str | None = None
    owner: str | None = None
    """The class whose method it is (Frame, Page, BrowserContext, ...)."""
    taints: bool = False


@dataclass(slots=True)
class RawRoute:
    """A ``Route.fulfill`` or ``Route.continue`` call of the agent's route handler."""

    time: float
    call: str
    overrides: bool


def _entry_url(entry: JsonObject) -> str:
    return as_str(as_object(entry.get("request")).get("url")) or ""


def action_name(method: str) -> str | None:
    if method in _OTHER_METHODS:
        return OTHER
    return known_action(method)


def taints_run(owner: str | None, method: str, params: JsonObject) -> bool:
    """Whether a recorded call can change what the site sends or what the page shows or
    stores: deny by default (see _HARMLESS_METHODS)."""
    if method in _INJECTED_CODE or owner in _UNTRUSTED_CLASSES or _opens_unserved(method, params):
        return True
    if method == "dispatchEvent":
        return _carries_value(as_object(params.get("eventInit")))
    if method == "accept":
        return bool(as_str(params.get("promptText")))
    if method in _CAPTURES:
        return any(params.get(key) not in (None, "", []) for key in _RESTYLING)
    known = action_name(method)
    if known is not None:
        return known == INJECT
    return method not in _HARMLESS_METHODS


def _opens_script(method: str, params: JsonObject) -> bool:
    """A ``goto`` of a URL that runs its code in the page (`is_script_url`)."""
    return method == "goto" and is_script_url(as_str(params.get("url")))


def _opens_unserved(method: str, params: JsonObject) -> bool:
    """A ``goto`` of a URL that shows no site's page under its own URL (`opens_unserved_page`):
    a script URL, view-source:, file:, chrome:, ... Chromium reports a view-source: page under
    the http URL whose source it shows, so only the call can tell it from the page itself."""
    return method == "goto" and opens_unserved_page(as_str(params.get("url")))


def _carries_value(arg: JsonObject) -> bool:
    """A serialised argument other than undefined, null or an empty object."""
    value = as_object(arg.get("value"))
    return value not in ({}, {"v": "undefined"}, {"v": "null"}, {"o": []}) or bool(
        as_list(arg.get("handles"))
    )


def is_settle(method: str, params: JsonObject) -> bool:
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


def script_sha256(method: str, params: JsonObject) -> str | None:
    """`script_hash` of an injected script whose code the trace holds; None for any other
    call (an exposed binding runs the agent's own process, never vouched for). A ``goto`` of a
    script URL hashes as api "goto" with the URL as its code."""
    if _opens_script(method, params):
        return script_hash("goto", as_str(params.get("url")) or "")
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


_UNSEEN_FILES: Final = ("localPaths", "localDirectory", "streams", "directoryStream")
"""``setInputFiles`` parameters that send files by path, folder or stream: their content is
not in the trace."""


def upload_text(params: JsonObject) -> str | None:
    """The text of the files a ``setInputFiles`` call sent with their content (``payloads``):
    the first _UPLOAD_TEXT_BYTES of each, decoded; a binary file contributes its runs of
    printable ASCII. Files sent by path, folder or stream are not in the trace, so a call that
    sends any (_UNSEEN_FILES) has no text: none of its files counts as seen."""
    if any(params.get(key) not in (None, "", []) for key in _UNSEEN_FILES):
        return None
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


def action_value(raw: RawAction) -> str | None:
    method, params = raw.method, raw.params
    if method in _INJECTED_CODE:
        api, key = _INJECTED_CODE[method]
        code = as_str(params.get(key)) or as_str(params.get("url"))
        return injected(api, code)
    if _opens_unserved(method, params):
        return injected("goto", as_str(params.get("url")))
    if raw.taints and method != MOCKED_ROUTE:
        api = f"{raw.owner}.{method}" if raw.owner else method
        return injected(api, _call_summary(params))
    return _recorded_value(method, params)


def _recorded_value(method: str, params: JsonObject) -> str | None:
    """What a user action typed, chose, uploaded or opened; None for any other call."""
    match method:
        case "Route":
            return as_str(params.get("call"))
        case "goto":
            return as_str(params.get("url"))
        case "fill":
            return as_str(params.get("value"))
        case "type" | "keyboardType" | "keyboardInsertText":
            return as_str(params.get("text"))
        case "press" | "keyboardPress" | "keyboardDown" | "keyboardUp":
            return as_str(params.get("key"))
        case "selectOption":
            return _joined(_option_label(as_object(o)) for o in as_list(params.get("options")))
        case "setInputFiles":
            return _uploaded_names(params)
        case _:
            return None


def _uploaded_names(params: JsonObject) -> str | None:
    """The names of the files a ``setInputFiles`` call sent: each file's name, a folder's name
    with a trailing "/", and how many files went as streams."""
    names = [as_str(as_object(p).get("name")) for p in as_list(params.get("payloads"))]
    paths = [as_str(p) for p in as_list(params.get("localPaths"))]
    names += [PurePosixPath(p).name for p in paths if p]
    if folder := as_str(params.get("localDirectory")):
        names.append(f"{PurePosixPath(folder).name or folder}/")
    if params.get("directoryStream") not in (None, "", []):
        names.append("a streamed folder")
    if streams := len(as_list(params.get("streams"))):
        names.append(f"{streams} streamed file{'s' if streams > 1 else ''}")
    return _joined(names)


def _option_label(option: JsonObject) -> str | None:
    for key in ("valueOrLabel", "value", "label"):
        if (text := as_str(option.get(key))) is not None:
            return text
    index = as_int(option.get("index"))
    return f"#{index}" if index is not None else None


def _joined(values: Iterable[str | None]) -> str | None:
    present = [v for v in values if v]
    return ", ".join(present) if present else None


@dataclass(slots=True)
class RouteCalls:
    """The calls of the agent's route handlers, and when it registered one."""

    fulfills: list[RawRoute] = field(default_factory=list["RawRoute"])
    continues: list[RawRoute] = field(default_factory=list["RawRoute"])
    fallbacks: list[RawRoute] = field(default_factory=list["RawRoute"])
    """``route.fallback`` calls with overrides: no recorded request is marked for them."""
    aborts: list[float] = field(default_factory=list[float])
    intercepts: list[float] = field(default_factory=list[float])
    """When a route handler was registered (``INTERCEPT`` with patterns)."""

    def record(self, method: str, params: JsonObject, start: float) -> bool:
        """Record a ``Route.fulfill``, ``Route.continue`` or ``Route.abort``; False for any other
        Route call (redirectNavigationRequest), which is an action that taints the run."""
        if method == "abort":
            self.aborts.append(start)
            return True
        if method == "fulfill":
            body = as_str(params.get("body")) or ""
            self.fulfills.append(RawRoute(start, f"route.fulfill({body[:80]})", overrides=True))
            return True
        if method != "continue":
            return False
        changed = [k for k in _ROUTE_OVERRIDES if params.get(k) not in (None, [], "")]
        if params.get("isFallback") is not True:
            call = f"route.continue({', '.join(changed)})"
            self.continues.append(RawRoute(start, call, overrides=bool(changed)))
        elif changed:
            call = f"route.fallback({', '.join(changed)})"
            self.fallbacks.append(RawRoute(start, call, overrides=True))
        return True

    def unmarked(
        self,
        network: list[NetworkEvent],
        resources: list[JsonObject],
        resource_time: Callable[[JsonObject], float],
    ) -> list[RawRoute]:
        """The calls that answered or rewrote a request no recorded request is marked synthetic
        for: a ``route.fallback`` with overrides (Playwright marks none), an abort that could
        change a page, and every overriding call when no request is marked (no network file, or
        a format without the marker)."""
        unmarked = [*self.fallbacks, *self._content_aborts(resources, resource_time)]
        if not any(n.synthetic for n in network):
            unmarked += [r for r in [*self.fulfills, *self.continues] if r.overrides]
        return unmarked

    def registered(self, time: float) -> float:
        """When the first route handler that could see a request at `time` was registered."""
        return min((t for t in self.intercepts if t <= time), default=time)

    def _content_aborts(
        self, resources: list[JsonObject], resource_time: Callable[[JsonObject], float]
    ) -> list[RawRoute]:
        """The ``Route.abort`` calls that could change what a page shows. Blocking an image, a
        font or media (to save bandwidth) cannot; blocking a document, script, stylesheet or API
        call can (the page falls back to text the agent chose to leave). Aborted entries
        (``_wasAborted``) pair, in order, with the calls; when the counts differ, every call
        counts."""
        aborted = sorted((e for e in resources if e.get("_wasAborted") is True), key=resource_time)
        calls = sorted(self.aborts)
        if len(calls) != len(aborted):
            return [RawRoute(t, "route.abort()", overrides=True) for t in calls]
        return [
            RawRoute(t, f"route.abort({kind} {_entry_url(e)[:80]})", overrides=True)
            for t, e in zip(calls, aborted, strict=True)
            if (kind := as_str(e.get("_resourceType")) or "unknown") not in _HARMLESS_ABORTS
        ]

    def rewritten(self, entries: list[JsonObject]) -> set[int]:
        """Ids of the entries the agent's ``route.continue`` rewrote. The continued entries
        (``_wasContinued``) pair, in order, with the ``Route.continue`` calls; when the counts
        differ, every continued entry counts as rewritten if any call had overrides."""
        continued = [e for e in entries if e.get("_wasContinued") is True]
        calls = sorted(self.continues, key=lambda r: r.time)
        if len(calls) != len(continued):
            return {id(e) for e in continued} if any(r.overrides for r in calls) else set()
        return {id(e) for e, r in zip(continued, calls, strict=True) if r.overrides}
