from __future__ import annotations

import bisect
import math
from collections.abc import Iterable
from functools import cached_property
from urllib.parse import urlsplit

from evidence_judge.core.evidence import (
    ActionEvent,
    Channel,
    NetworkEvent,
    PageSnapshot,
    PageState,
    Run,
    Screenshot,
)
from evidence_judge.textnorm import normalize

FINAL_KEY = "final"
NAVIGATIONS = frozenset({"goto", "navigate", "visit", "open", "go_to_url", "open_tab"})
"""Action names that only open a page; the first other action starts the agent's work."""


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
            acted = [a.ts for a in self.run.actions if a.name.casefold() not in NAVIGATIONS]
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


def is_document_navigation(event: NetworkEvent) -> bool:
    """A page load: a GET for a document, or one answered with HTML when the type is unknown."""
    if event.method.upper() != "GET":
        return False
    if event.resource_type:
        return event.resource_type.lower() == "document"
    return (event.mime_type or "").split(";", 1)[0].strip().lower() == "text/html"
