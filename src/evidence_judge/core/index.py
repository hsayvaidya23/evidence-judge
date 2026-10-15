from __future__ import annotations

import bisect
import html
import math
import re
from collections.abc import Iterable, Iterator
from functools import cached_property
from pathlib import PureWindowsPath
from typing import Final
from urllib.parse import (
    parse_qsl,
    quote,
    quote_plus,
    unquote,
    unquote_plus,
    urljoin,
    urlsplit,
    urlunsplit,
)

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
from evidence_judge.core.site import Site
from evidence_judge.redact import TOKEN_RE
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
_REDACTION_TOKEN = re.compile(TOKEN_RE.pattern, re.IGNORECASE)
MIN_ECHO_PREFIX: Final = 8
"""Shortest prefix of an agent input that counts as a cut-short echo of it. Shorter shared
prefixes are mostly common word stems ("confirm" in "confirmation" and "confirmed")."""
MASK: Final = "\x00"
"""Stands in for agent input in masked evidence; it is never part of a predicate."""


class EvidenceIndex:
    """Read-only lookups over a Run. Checkers query evidence only through this class."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self._normalized: dict[str, str] = {}
        self._inputs_by_site: dict[Site | None, tuple[str, ...]] = {}

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
    def primary_site(self) -> Site | None:
        """Site (host and port) of the first page the run was on: the initial state, else the
        first action, snapshot, document navigation or final state that names a URL."""
        initial = self.run.initial_state
        candidates = [
            initial.url if initial else None,
            *(a.url for a in self.run.actions),
            *(s.url for s in self.run.snapshots),
            *(n.url for n in self.run.network if is_document_navigation(n)),
            self.final.url if self.final else None,
        ]
        return next((s for url in candidates if (s := Site.of_url(url)) is not None), None)

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
        (a form the agent submitted, or a navigation a HAR cannot attribute); the URL inputs
        only count as echoed on the site of the URL they came from. Each is also kept
        as a page rendering it as HTML would show it (tags dropped, character references
        decoded). Inputs shorter than MIN_PAGE_TEXT or without a letter or digit are left out.

        Request bodies are not inputs: whatever the agent typed into a form already comes from
        its typing action, so a body only adds what the site put in the form (hidden fields,
        product ids, option values), and masking that would hide the site's own text.
        """
        return self._agent_inputs

    @cached_property
    def _agent_inputs(self) -> tuple[str, ...]:
        return tuple(sorted({text for text, _ in self._scoped_inputs}, key=lambda t: (-len(t), t)))

    @cached_property
    def _scoped_inputs(self) -> tuple[tuple[str, Site | None], ...]:
        """Each input with the site it can be echoed on (None: any site)."""
        found = {
            (normalize(form), site)
            for text, site in _raw_inputs(self.run)
            for form in _rendered(text)
        }
        kept = [(t, site) for t, site in found if len(t) >= MIN_PAGE_TEXT and WORD.search(t)]
        return tuple(sorted(kept, key=lambda i: (-len(i[0]), i[0], str(i[1] or ""))))

    def _inputs_for(self, url: str | None) -> tuple[str, ...]:
        """The inputs that can be echoed on `url`: typed ones, and URL ones from its site."""
        site = Site.of_url(url)
        cached = self._inputs_by_site.get(site)
        if cached is None:
            cached = tuple(
                dict.fromkeys(
                    text
                    for text, origin in self._scoped_inputs
                    if origin is None or (site is not None and origin.covers(site))
                )
            )
            self._inputs_by_site[site] = cached
        return cached

    @cached_property
    def without_agent_input(self) -> EvidenceIndex:
        """This evidence with every echo of every agent input in page text and URLs (an
        occurrence of the input or of a prefix of at least MIN_ECHO_PREFIX characters of it)
        replaced by MASK, so a predicate that only holds on an echo of that input fails on it.

        Masked page text is normalised; page text and URLs without an occurrence are unchanged.
        """
        if not self.agent_inputs():
            return self
        run = self.run
        return EvidenceIndex(
            run.model_copy(
                update={
                    "actions": [_masked_url(a, self._inputs_for(a.url)) for a in run.actions],
                    "network": [_masked_url(n, self._inputs_for(n.url)) for n in run.network],
                    "snapshots": [_masked_page(s, self._inputs_for(s.url)) for s in run.snapshots],
                    "initial_state": None
                    if run.initial_state is None
                    else _masked_page(run.initial_state, self._inputs_for(run.initial_state.url)),
                    "final_state": None
                    if run.final_state is None
                    else _masked_page(run.final_state, self._inputs_for(run.final_state.url)),
                }
            )
        )


def _raw_inputs(run: Run) -> Iterator[tuple[str, Site | None]]:
    served = _site_served(run)
    keys: list[str] = []
    target: str | None = None
    last = -math.inf
    for action in run.actions:
        before, last = last, action.ts
        name = canonical_action(action.name)
        if name == PRESS and action.value is not None and len(action.value) == 1:
            if keys and action.selector != target:
                yield "".join(keys), None
                keys.clear()
            target = action.selector
            keys.append(action.value)
            continue
        if keys:
            yield "".join(keys), None
            keys.clear()
        if (name == FILL and action.value) or (
            name == PRESS and action.value and not _is_key_names(action.value)
        ):
            yield action.value, None
        elif name == UPLOAD and action.value:
            yield from ((n, None) for n in _file_names(action.value))
        elif (
            name == GOTO
            and (url := action.value or action.url)
            and served.get(_page_key(url), math.inf) >= before
        ):
            yield from ((v, Site.of_url(url)) for v in _url_inputs(url, with_path=True))
    if keys:
        yield "".join(keys), None
    for event in run.network:
        if event.method.upper() == "GET":
            yield from (
                (v, Site.of_url(event.url)) for v in _url_inputs(event.url, with_path=False)
            )


def _site_served(run: Run) -> dict[str, float]:
    """When the browser first loaded each URL, or was redirected to it (Location)."""
    served: dict[str, float] = {}
    for event in run.network:
        keys = [_page_key(event.url)] if event.method.upper() == "GET" else []
        keys += [
            _page_key(urljoin(event.url, value))
            for name, value in event.response_headers.items()
            if name.lower() == "location"
        ]
        for key in keys:
            served[key] = min(served.get(key, math.inf), event.ts)
    return served


def _page_key(url: str) -> str:
    return urlunsplit(urlsplit(url)._replace(fragment=""))


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
    """`text` normalised with every echo of an input replaced by MASK, or None if it has none.

    An echo is an occurrence of the whole input or of a prefix of at least MIN_ECHO_PREFIX
    characters (a field's maxlength, or the site, cut the input short); overlapping echoes
    become one MASK.
    """
    norm = normalize(text)
    spans = sorted(span for value in inputs for span in _echoes(norm, value))
    if not spans:
        return None
    parts: list[str] = []
    end = 0
    for start, stop in spans:
        if start < end:
            end = max(end, stop)
            continue
        parts += [norm[end:start], MASK]
        end = stop
    parts.append(norm[end:])
    return "".join(parts)


def _echoes(text: str, value: str) -> Iterator[tuple[int, int]]:
    """The span of every echo of `value` in `text`, each extended as far as `value` matches.

    A cut-short echo never ends inside a redaction token: distinct tokens share a long prefix.
    """
    head = value[:MIN_ECHO_PREFIX]
    tokens = [m.span() for m in _REDACTION_TOKEN.finditer(value)]
    at = text.find(head)
    while at != -1:
        length = _match_length(text, at, value, len(head))
        length = next((start for start, end in tokens if start < length < end), length)
        if length == len(value) or length >= MIN_ECHO_PREFIX:
            yield at, at + length
        at = text.find(head, at + (length if length == len(value) else 1))


def _match_length(text: str, at: int, value: str, known: int) -> int:
    """How many leading characters of `value` occur at `text[at:]`; the first `known` do."""
    low, high = known, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if text.startswith(value[known:mid], at + known):
            low = mid
        else:
            high = mid - 1
    return low


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
