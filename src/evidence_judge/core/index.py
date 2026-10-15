from __future__ import annotations

import bisect
import html
import math
import re
from collections.abc import Iterable, Iterator
from functools import cached_property
from pathlib import PureWindowsPath
from typing import Final
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlsplit, urlunsplit

from evidence_judge.core.actions import FILL, GOTO, PRESS, UPLOAD, canonical_action
from evidence_judge.core.evidence import (
    ActionEvent,
    Channel,
    NetworkEvent,
    PageSnapshot,
    PageState,
    Run,
    Screenshot,
)
from evidence_judge.core.predicates import MIN_PAGE_TEXT, WORD
from evidence_judge.textnorm import normalize

FINAL_KEY = "final"
_KEY_NAME = re.compile(
    r"(?:.|shift|control|ctrl|alt|option|meta|command|cmd|controlormeta|enter|return|tab|space"
    r"|escape|esc|backspace|delete|del|insert|home|end|pageup|pagedown|capslock|numlock"
    r"|scrolllock|pause|printscreen|contextmenu|arrow(?:up|down|left|right)|up|down|left|right"
    r"|f\d{1,2}|key[a-z]|digit\d|numpad\w+)",
    re.IGNORECASE,
)
_TAG = re.compile(r"</?[a-z][^<>]*>", re.IGNORECASE)
MASK: Final = "\x00"
"""Stands in for agent input in masked evidence; it is never part of a predicate."""


class EvidenceIndex:
    """Read-only lookups over a Run. Checkers query evidence only through this class."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self._normalized: dict[str, str] = {}

    def has(self, channel: Channel) -> bool:
        return channel in self.run.captured

    @cached_property
    def by_id(self) -> dict[str, object]:
        items: Iterable[ActionEvent | NetworkEvent | PageSnapshot | Screenshot] = [
            *self.run.actions,
            *self.run.network,
            *self.run.snapshots,
            *self.run.screenshots,
        ]
        out: dict[str, object] = {i.id: i for i in items}
        out.update({c.id: c for c in self.run.console})
        return out

    @property
    def final(self) -> PageState | None:
        return self.run.final_state

    def requests(self, method: str | None = None) -> list[NetworkEvent]:
        if method is None:
            return list(self.run.network)
        m = method.upper()
        return [n for n in self.run.network if n.method.upper() == m]

    @cached_property
    def primary_host(self) -> str | None:
        """Host of the first page the run was on: the initial state, else the first action,
        snapshot, document navigation or final state that names a URL."""
        initial = self.run.initial_state
        candidates = [
            initial.url if initial else None,
            *(a.url for a in self.run.actions),
            *(s.url for s in self.run.snapshots),
            *(n.url for n in self.run.network if is_document_navigation(n)),
            self.final.url if self.final else None,
        ]
        for url in candidates:
            host = urlsplit(url).hostname if url else None
            if host:
                return host.casefold()
        return None

    @cached_property
    def baseline(self) -> EvidenceIndex:
        """The evidence as it stood before the agent acted: what every run of the task has.

        The cut is the agent's first action that is not a navigation (opening the start page is
        set-up); a run whose actions are all navigations is all baseline. Without recorded actions
        the cut is the first mutation or the second page load (no page load: empty baseline). The
        baseline keeps the actions, requests and snapshots before the cut; its final state is the
        last snapshot before the cut, else the initial state (the run's final state when the
        whole run is baseline).
        """
        cut = self._baseline_cut()
        snapshots = [s for s in self.run.snapshots if s.ts < cut]
        last = snapshots[-1] if snapshots else None
        if cut == math.inf:
            start = self.run.final_state
        elif last is not None:
            start = PageState(url=last.url, title=last.title, text=last.text, snapshot_id=last.id)
        else:
            start = self.run.initial_state
        run = self.run.model_copy(
            update={
                "actions": [a for a in self.run.actions if a.ts < cut],
                "network": [n for n in self.run.network if n.ts < cut],
                "snapshots": snapshots,
                "screenshots": [],
                "final_state": start,
            }
        )
        return EvidenceIndex(run)

    def _baseline_cut(self) -> float:
        if self.run.actions:
            acted = [a.ts for a in self.run.actions if canonical_action(a.name) != GOTO]
            return min(acted, default=math.inf)
        loads = [n.ts for n in self.run.network if is_document_navigation(n)]
        marks = [n.ts for n in self.run.network if n.is_mutation] + loads[1:2]
        if marks:
            return min(marks)
        return math.nextafter(loads[0], math.inf) if loads else -math.inf

    def mutations(self) -> list[NetworkEvent]:
        return [n for n in self.run.network if n.is_mutation]

    @cached_property
    def _shot_ts(self) -> list[float]:
        return [s.ts for s in self.run.screenshots]

    def nearest_screenshot(self, ts: float | None = None) -> Screenshot | None:
        shots = self.run.screenshots
        if not shots:
            return None
        if ts is None:
            if self.final and self.final.screenshot_id:
                for s in shots:
                    if s.id == self.final.screenshot_id:
                        return s
            return shots[-1]
        i = bisect.bisect_left(self._shot_ts, ts)
        candidates = [shots[j] for j in (i - 1, i) if 0 <= j < len(shots)]
        return min(candidates, key=lambda s: abs(s.ts - ts))

    def screenshot_bytes(self, shot: Screenshot) -> bytes | None:
        return self.run.blobs.get(shot.resource)

    def page_states(self) -> list[PageState]:
        """Every page state seen, in order, ending with the final state. Built once per index."""
        return self._page_states

    @cached_property
    def _page_states(self) -> list[PageState]:
        states = [
            PageState(url=s.url, title=s.title, text=s.text, snapshot_id=s.id)
            for s in self.run.snapshots
        ]
        if self.final:
            states.append(self.final)
        return states

    def normalized_text(self, state: PageState) -> str:
        """`normalize(state.text)`, computed once per snapshot and once for the final state."""
        key = FINAL_KEY if state is self.final else state.snapshot_id
        if key is None:
            return normalize(state.text)
        cached = self._normalized.get(key)
        if cached is None:
            cached = self._normalized[key] = normalize(state.text)
        return cached

    def agent_inputs(self) -> tuple[str, ...]:
        """Normalised text the agent put into the run itself, longest first.

        That is the value of every typing action, each run of single-key presses on one target,
        every press of text that is not key names (browser-use ``send_keys``), the name (with
        and without extension) of every uploaded file, the decoded path segments and query
        values of every URL the agent opened, and the decoded query values of every GET request
        (a form the agent submitted, or a navigation a HAR cannot attribute). Each is also kept
        as a page rendering it as HTML would show it (tags dropped, character references
        decoded). Inputs shorter than MIN_PAGE_TEXT or without a letter or digit are left out.
        """
        return self._agent_inputs

    @cached_property
    def _agent_inputs(self) -> tuple[str, ...]:
        found = {normalize(form) for text in _raw_inputs(self.run) for form in _rendered(text)}
        kept = [t for t in found if len(t) >= MIN_PAGE_TEXT and WORD.search(t)]
        return tuple(sorted(kept, key=lambda t: (-len(t), t)))

    @cached_property
    def without_agent_input(self) -> EvidenceIndex:
        """This evidence with every occurrence of every agent input in page text and URLs
        replaced by MASK, so a predicate that only holds on an echo of that input fails on it.

        Masked page text is normalised; page text and URLs without an occurrence are unchanged.
        """
        inputs = self.agent_inputs()
        if not inputs:
            return self
        run = self.run
        return EvidenceIndex(
            run.model_copy(
                update={
                    "actions": [_masked_url(a, inputs) for a in run.actions],
                    "network": [_masked_url(n, inputs) for n in run.network],
                    "snapshots": [_masked_page(s, inputs) for s in run.snapshots],
                    "initial_state": None
                    if run.initial_state is None
                    else _masked_page(run.initial_state, inputs),
                    "final_state": None
                    if run.final_state is None
                    else _masked_page(run.final_state, inputs),
                }
            )
        )


def _raw_inputs(run: Run) -> Iterator[str]:
    keys: list[str] = []
    target: str | None = None
    for action in run.actions:
        name = canonical_action(action.name)
        if name == PRESS and action.value is not None and len(action.value) == 1:
            if keys and action.selector != target:
                yield "".join(keys)
                keys.clear()
            target = action.selector
            keys.append(action.value)
            continue
        if keys:
            yield "".join(keys)
            keys.clear()
        if (name == FILL and action.value) or (
            name == PRESS and action.value and not _is_key_names(action.value)
        ):
            yield action.value
        elif name == UPLOAD and action.value:
            yield from _file_names(action.value)
        elif name == GOTO and (url := action.value or action.url):
            yield from _url_inputs(url, with_path=True)
    if keys:
        yield "".join(keys)
    for event in run.network:
        if event.method.upper() == "GET":
            yield from _url_inputs(event.url, with_path=False)


def _rendered(text: str) -> tuple[str, ...]:
    """`text`, and how a page that renders it as HTML shows it: character references decoded
    and tags dropped (inline) or turned into a break (block)."""
    return text, *(html.unescape(_TAG.sub(gap, text)) for gap in ("", " "))


def _is_key_names(value: str) -> bool:
    """`value` is a key or chord ("Enter", "Control+Shift+T"), or several separated by spaces."""
    return all(
        all(not key or _KEY_NAME.fullmatch(key) for key in chord.split("+"))
        for chord in value.split()
    )


def _file_names(value: str) -> Iterator[str]:
    """Each uploaded file's name with and without its extension; an upload's value is one
    path or name, or several joined by ", "."""
    for part in {value, *value.split(", ")}:
        name = PureWindowsPath(part).name
        yield name
        stem, dot, _ = name.rpartition(".")
        if dot and stem:
            yield stem


def _url_inputs(url: str, *, with_path: bool) -> list[str]:
    parts = urlsplit(url)
    values = [value for _, value in parse_qsl(parts.query)]
    if with_path:
        values += [unquote(segment) for segment in parts.path.split("/")]
    return values


def _mask(text: str, inputs: tuple[str, ...]) -> str | None:
    """`text` normalised with every input replaced by MASK, or None if no input occurs in it."""
    masked = norm = normalize(text)
    for value in inputs:
        masked = masked.replace(value, MASK)
    return None if masked == norm else masked


def _mask_url(url: str, inputs: tuple[str, ...]) -> str:
    parts = urlsplit(url)
    path = _mask(unquote(parts.path), inputs)
    query = _mask(unquote_plus(parts.query), inputs)
    if path is None and query is None:
        return url
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path if path is None else quote(path, safe="/"),
            parts.query if query is None else quote_plus(query, safe="=&"),
            parts.fragment,
        )
    )


def _masked_url[E: (ActionEvent, NetworkEvent)](event: E, inputs: tuple[str, ...]) -> E:
    if event.url is None:
        return event
    return event.model_copy(update={"url": _mask_url(event.url, inputs)})


def _masked_page[S: (PageSnapshot, PageState)](page: S, inputs: tuple[str, ...]) -> S:
    text = _mask(page.text, inputs)
    update = {"url": _mask_url(page.url, inputs)}
    if text is not None:
        update["text"] = text
    return page.model_copy(update=update)


def is_document_navigation(event: NetworkEvent) -> bool:
    """A page load: a GET for a document, or one answered with HTML when the type is unknown."""
    if event.method.upper() != "GET":
        return False
    if event.resource_type:
        return event.resource_type.lower() == "document"
    return (event.mime_type or "").split(";", 1)[0].strip().lower() == "text/html"
