from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable
from functools import cached_property
from typing import Final

from evidence_judge.core.actions import GOTO, canonical_action
from evidence_judge.core.agent_input import raw_inputs, rendered
from evidence_judge.core.echo import (
    covered_page,
    covered_request,
    masked_page,
    masked_request,
    masked_url,
)
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
from evidence_judge.textnorm import normalize

FINAL_KEY = "final"
_DIGIT = re.compile(r"\d")
MIN_ECHO_WORD: Final = 6
"""Shortest single word without a digit that the agent can echo in every predicate's evidence.
Shorter ones ("your", "cart") are common words of honest page text; they only mask text that is
itself a single short word."""


class EvidenceIndex:
    """Read-only lookups over a Run. Checkers query evidence only through this class."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self._normalized: dict[str, str] = {}
        self._inputs_by_site: dict[tuple[Site | None, bool], tuple[str, ...]] = {}
        self._covered: dict[tuple[str, ...], EvidenceIndex] = {}

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

        The cut is the agent's first action, where a first action that opens a page is set-up
        (the start page) and every later navigation is the agent's; a run whose only action opens
        the start page is all baseline. Without recorded actions the cut is, the same way, the
        first mutation or the second page load (no page load: empty baseline). The
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
            first, *rest = sorted(self.run.actions, key=lambda a: a.ts)
            acted = [a.ts for a in rest]
            if canonical_action(first.name) != GOTO:
                acted.append(first.ts)
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
        every press of text that is not key names (browser-use ``send_keys``), each value a
        field took as the agent edited it (Backspace, Delete, cursor moves, selections, cut and
        paste replayed; see core.agent_input), each line and sentence of a page it copied text from, the
        name (with and without extension) of every uploaded file and each line and sentence of
        the text it holds (ActionEvent.file_text), the decoded path segments
        and query values of every URL the agent opened, and the decoded query values of every
        GET request (a form the agent submitted, or a navigation a HAR cannot attribute); the
        URL inputs only count as echoed on the site of the URL they came from. Every value a
        form field held in a snapshot (PageSnapshot.field_values) is one too: replaying the
        keyboard misses values made with the mouse (a click that moves the caret, a drag and
        drop, a datalist or autofill choice), and the field shows what it holds. Each is also kept
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
        return tuple(i for i in self._every_input if len(i[0]) >= MIN_PAGE_TEXT)

    @cached_property
    def _every_input(self) -> tuple[tuple[str, Site | None], ...]:
        """`_scoped_inputs` with those shorter than MIN_PAGE_TEXT too."""
        found = {
            (normalize(form), site)
            for text, site in raw_inputs(self.run)
            for form in rendered(text)
        }
        kept = [(t, site) for t, site in found if WORD.search(t)]
        return tuple(sorted(kept, key=lambda i: (-len(i[0]), i[0], str(i[1] or ""))))

    def _scoped(
        self, inputs: tuple[tuple[str, Site | None], ...], url: str | None
    ) -> Iterable[str]:
        site = Site.of_url(url)
        return (
            text
            for text, origin in inputs
            if origin is None or (site is not None and origin.covers(site))
        )

    def _inputs_for(self, url: str | None, *, short_words: bool) -> tuple[str, ...]:
        """The inputs that can be echoed on `url`: typed ones, and URL ones from its site;
        single short words (is_short_word) only with `short_words`."""
        site = Site.of_url(url)
        cached = self._inputs_by_site.get((site, short_words))
        if cached is None:
            cached = tuple(
                dict.fromkeys(
                    text
                    for text in self._scoped(self._scoped_inputs, url)
                    if short_words or not is_short_word(text)
                )
            )
            self._inputs_by_site[site, short_words] = cached
        return cached

    @cached_property
    def without_agent_input(self) -> EvidenceIndex:
        """This evidence with every echo of every agent input (an occurrence of the input or of
        a part of at least MIN_ECHO_PART characters of it, see core.echo.mask) replaced by MASK wherever
        the site can repeat it back: page text, page and action URLs, the URL of every request that
        is not a mutation, and every response body. A predicate that only holds on an echo of
        that input fails on it.

        A mutation's URL and request body are left alone: they are what the agent submitted,
        so a POST carrying the typed value is evidence of the submission, not an echo. Its
        response is masked like any other: a site that answers a POST by repeating the typed
        value proves nothing by doing so.

        Inputs that are a single short word (is_short_word) are not masked here: "Your" typed
        into a name field must not mask "Thank you for your order". Only text that is itself a
        single short word can be produced by one; `without_short_word_input` masks them too.

        Masked page text and non-JSON bodies are normalised; JSON bodies keep their structure,
        with each echoing string (key or value) masked. Evidence without an echo is unchanged.
        """
        return self._masked(short_words=False)

    @cached_property
    def without_short_word_input(self) -> EvidenceIndex:
        """`without_agent_input` that also masks inputs that are a single short word."""
        return self._masked(short_words=True)

    def without_covered(self, needles: tuple[str, ...]) -> EvidenceIndex:
        """This evidence with every occurrence of each of `needles` (normalised) that the agent's
        inputs make word by word masked, in page text and response bodies: inputs of any length,
        single short words and those under MIN_PAGE_TEXT characters included, each where it can
        be echoed (core.echo.mask_covered). The text a predicate looks for, made from several
        inputs none of which is masked alone ("In" and "stock" typed into two fields of a
        preview), is the agent's own text; an occurrence with one word the agent did not put in
        is left alone."""
        key = tuple(sorted({normalize(n) for n in needles}))
        cached = self._covered.get(key)
        if cached is None:
            cached = self._covered[key] = self._covering(key)
        return cached

    def _covering(self, needles: tuple[str, ...]) -> EvidenceIndex:
        if not needles or not self._every_input:
            return self
        run = self.run
        inputs = self._every_input

        def page[S: (PageSnapshot, PageState)](state: S) -> S:
            return covered_page(state, needles, tuple(self._scoped(inputs, state.url)))

        return EvidenceIndex(
            run.model_copy(
                update={
                    "network": [
                        covered_request(n, needles, tuple(self._scoped(inputs, n.url)))
                        for n in run.network
                    ],
                    "snapshots": [page(s) for s in run.snapshots],
                    "initial_state": None if run.initial_state is None else page(run.initial_state),
                    "final_state": None if run.final_state is None else page(run.final_state),
                }
            )
        )

    def _masked(self, *, short_words: bool) -> EvidenceIndex:
        if not any(short_words or not is_short_word(text) for text, _ in self._scoped_inputs):
            return self
        run = self.run

        def inputs(url: str | None) -> tuple[str, ...]:
            return self._inputs_for(url, short_words=short_words)

        return EvidenceIndex(
            run.model_copy(
                update={
                    "actions": [masked_url(a, inputs(a.url)) for a in run.actions],
                    "network": [masked_request(n, inputs(n.url)) for n in run.network],
                    "snapshots": [masked_page(s, inputs(s.url)) for s in run.snapshots],
                    "initial_state": None
                    if run.initial_state is None
                    else masked_page(run.initial_state, inputs(run.initial_state.url)),
                    "final_state": None
                    if run.final_state is None
                    else masked_page(run.final_state, inputs(run.final_state.url)),
                }
            )
        )


def is_short_word(text: str) -> bool:
    """True if `text` is a single word shorter than MIN_ECHO_WORD characters, without a digit:
    a common word ("your", "cart", "paid") that honest page text often contains."""
    words = WORD.findall(text)
    return len(words) == 1 and len(words[0]) < MIN_ECHO_WORD and not _DIGIT.search(words[0])


def is_document_navigation(event: NetworkEvent) -> bool:
    """A page load: a GET for a document, or one answered with HTML when the type is unknown."""
    if event.method.upper() != "GET":
        return False
    if event.resource_type:
        return event.resource_type.lower() == "document"
    return (event.mime_type or "").split(";", 1)[0].strip().lower() == "text/html"
