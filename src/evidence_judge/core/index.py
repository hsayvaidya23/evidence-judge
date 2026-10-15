from __future__ import annotations

import bisect
import functools
import html
import json
import math
import re
from collections.abc import Iterable, Iterator
from functools import cached_property
from pathlib import PureWindowsPath
from typing import Final, cast
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
_DIGIT = re.compile(r"\d")
_TAG = re.compile(r"</?[a-z][^<>]*>", re.IGNORECASE)
_REDACTION_TOKEN = re.compile(TOKEN_RE.pattern, re.IGNORECASE)
MIN_ECHO_WORD: Final = 6
"""Shortest single word without a digit that the agent can echo in every predicate's evidence.
Shorter ones ("your", "cart") are common words of honest page text; they only mask text that is
itself a single short word."""
MIN_ECHO_PART: Final = 8
"""Shortest part of an agent input (from one of its word starts) that counts as an echo of it.
Shorter shared parts are mostly common word stems ("confirm" in "confirmation" and
"confirmed")."""
_TRUNCATION_TAIL = re.compile(r"…\[truncated[^\]]{0,60}\]\s*\Z")
MASK: Final = "\x00"
"""Stands in for agent input in masked evidence; it is never part of a predicate."""


class EvidenceIndex:
    """Read-only lookups over a Run. Checkers query evidence only through this class."""

    def __init__(self, run: Run) -> None:
        self.run = run
        self._normalized: dict[str, str] = {}
        self._inputs_by_site: dict[tuple[Site | None, bool], tuple[str, ...]] = {}

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
        paste replayed; see _Typing), each line and sentence of a page it copied text from, the
        name (with and without extension) of every uploaded file and each line and sentence of
        the text it holds (ActionEvent.file_text), the decoded path segments
        and query values of every URL the agent opened, and the decoded query values of every
        GET request (a form the agent submitted, or a navigation a HAR cannot attribute); the
        URL inputs only count as echoed on the site of the URL they came from. Each is also kept
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

    def _inputs_for(self, url: str | None, *, short_words: bool) -> tuple[str, ...]:
        """The inputs that can be echoed on `url`: typed ones, and URL ones from its site;
        single short words (is_short_word) only with `short_words`."""
        site = Site.of_url(url)
        cached = self._inputs_by_site.get((site, short_words))
        if cached is None:
            cached = tuple(
                dict.fromkeys(
                    text
                    for text, origin in self._scoped_inputs
                    if (origin is None or (site is not None and origin.covers(site)))
                    and (short_words or not is_short_word(text))
                )
            )
            self._inputs_by_site[site, short_words] = cached
        return cached

    @cached_property
    def without_agent_input(self) -> EvidenceIndex:
        """This evidence with every echo of every agent input (an occurrence of the input or of
        a part of at least MIN_ECHO_PART characters of it, see _mask) replaced by MASK wherever
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

    def _masked(self, *, short_words: bool) -> EvidenceIndex:
        if not any(short_words or not is_short_word(text) for text, _ in self._scoped_inputs):
            return self
        run = self.run

        def inputs(url: str | None) -> tuple[str, ...]:
            return self._inputs_for(url, short_words=short_words)

        return EvidenceIndex(
            run.model_copy(
                update={
                    "actions": [_masked_url(a, inputs(a.url)) for a in run.actions],
                    "network": [_masked_request(n, inputs(n.url)) for n in run.network],
                    "snapshots": [_masked_page(s, inputs(s.url)) for s in run.snapshots],
                    "initial_state": None
                    if run.initial_state is None
                    else _masked_page(run.initial_state, inputs(run.initial_state.url)),
                    "final_state": None
                    if run.final_state is None
                    else _masked_page(run.final_state, inputs(run.final_state.url)),
                }
            )
        )


def is_short_word(text: str) -> bool:
    """True if `text` is a single word shorter than MIN_ECHO_WORD characters, without a digit:
    a common word ("your", "cart", "paid") that honest page text often contains."""
    words = WORD.findall(text)
    return len(words) == 1 and len(words[0]) < MIN_ECHO_WORD and not _DIGIT.search(words[0])


def _raw_inputs(run: Run) -> Iterator[tuple[str, Site | None]]:
    served = _site_served(run)
    typing = _Typing(run)
    last = -math.inf
    for action in run.actions:
        before, last = last, action.ts
        name = canonical_action(action.name)
        typing.act(action, name)
        if name == UPLOAD and action.value:
            yield from ((n, None) for n in _file_names(action.value))
        if name == UPLOAD and action.file_text:
            yield from ((line, None) for line in _lines(action.file_text))
        elif (
            name == GOTO
            and (url := action.value or action.url)
            and served.get(_page_key(url), math.inf) >= before
        ):
            yield from ((v, Site.of_url(url)) for v in _url_inputs(url, with_path=True))
    yield from ((text, None) for text in typing.finish())
    for event in run.network:
        if event.method.upper() == "GET":
            yield from (
                (v, Site.of_url(event.url)) for v in _url_inputs(event.url, with_path=False)
            )


class _Field:
    """A text field the agent typed into, replayed: its value, cursor and selection. It starts
    empty; `settle` records the value as an input whenever it changed since the last one."""

    def __init__(self, out: list[str]) -> None:
        self._out = out
        self._shown = ""
        self.text = ""
        self.cursor = 0
        self.anchor: int | None = None

    def settle(self) -> None:
        if self.text != self._shown:
            self._out.append(self.text)
            self._shown = self.text

    def forget(self) -> None:
        """The value can no longer be replayed (undo, a paste of unknown text): start over."""
        self.settle()
        self.text = self._shown = ""
        self.cursor, self.anchor = 0, None

    @property
    def span(self) -> tuple[int, int]:
        anchor = self.cursor if self.anchor is None else self.anchor
        return min(anchor, self.cursor), max(anchor, self.cursor)

    def selected(self) -> str:
        lo, hi = self.span
        return self.text[lo:hi]

    def replace(self, text: str, lo: int, hi: int) -> None:
        if lo < hi:
            self.settle()
        self.text = self.text[:lo] + text + self.text[hi:]
        self.cursor, self.anchor = lo + len(text), None

    def type(self, text: str) -> None:
        self.replace(text, *self.span)

    def set(self, text: str) -> None:
        self.settle()
        self.text, self.cursor, self.anchor = text, len(text), None

    def move(self, to: int, *, extend: bool) -> None:
        if not extend:
            self.anchor = None
        elif self.anchor is None:
            self.anchor = self.cursor
        self.cursor = max(0, min(to, len(self.text)))

    def select_all(self) -> None:
        self.anchor, self.cursor = 0, len(self.text)

    def delete(self, *, backward: bool, unit: str) -> None:
        lo, hi = self.span
        if lo == hi and backward:
            lo = {"char": max(0, lo - 1), "word": _word_left(self.text, lo), "line": 0}[unit]
        elif lo == hi:
            end = len(self.text)
            hi = {"char": min(end, hi + 1), "word": _word_right(self.text, hi), "line": end}[unit]
        self.replace("", lo, hi)

    def step(self, *, right: bool, mods: frozenset[str]) -> int:
        """Where an arrow key moves the cursor: a character, a word (Control, Alt) or to the
        end of the line (Meta)."""
        if "meta" in mods:
            return len(self.text) if right else 0
        if mods & {"ctrl", "alt"}:
            return (_word_right if right else _word_left)(self.text, self.cursor)
        return self.cursor + (1 if right else -1)


class _Typing:
    """Replays the agent's keyboard to find every text it put in a field.

    Each field's value is replayed from `fill` (sets it), insert-style typing and typed
    characters (insert at the cursor, over a selection), Backspace and Delete (a character, a
    word with Control or Alt, the line with Meta), arrows, Home and End (Shift extends a
    selection), and Control or Meta with A (select all), X and C (cut and copy a selection) and
    V (paste). The value is an input whenever the agent stops editing the field (another key,
    action or target) and just before an edit removes text; the raw typed text (each typing
    action's value, each run of single-character presses on one target) is an input too.

    A copy without a selection in a replayed field takes unknown text from the page: every line
    and sentence of the page it was on becomes an input. A paste of unknown text with no copy
    recorded makes every page seen so far one. Undo, redo and a paste of unknown text start the
    field over, since its value can no longer be replayed.
    """

    def __init__(self, run: Run) -> None:
        self._run = run
        self._out: list[str] = []
        self._fields: dict[str | None, _Field] = {}
        self._focus: str | None = None
        self._keys: list[str] = []
        self._keys_target: str | None = None
        self._clipboard: str | None = None
        self._copied = False

    def act(self, action: ActionEvent, name: str) -> None:
        value = action.value
        if name not in (PRESS, FILL) or value is None:
            self._flush_keys()
            self._settle()
            if name == GOTO:
                self._fields.clear()
            if action.selector is not None:
                self._focus = action.selector
            return
        target = self._focus if action.selector is None else action.selector
        self._focus = target
        field = self._fields.setdefault(target, _Field(self._out))
        if name == PRESS and len(value) == 1:
            if self._keys and target != self._keys_target:
                self._flush_keys()
            self._keys_target = target
            self._keys.append(value)
            field.type(value)
            return
        self._flush_keys()
        if name == PRESS and _is_key_names(value):
            for chord in value.split():
                self._key(field, chord, action.ts)
            return
        if value:
            self._out.append(value)
        if name == FILL and normalize(action.name).replace(" ", "_") in _SETS_VALUE:
            field.set(value)
        else:
            field.type(value)

    def finish(self) -> list[str]:
        self._flush_keys()
        self._settle()
        return self._out

    def _flush_keys(self) -> None:
        if self._keys:
            self._out.append("".join(self._keys))
            self._keys.clear()

    def _settle(self) -> None:
        for field in self._fields.values():
            field.settle()

    def _key(self, field: _Field, chord: str, ts: float) -> None:
        mods, key = _chord(chord)
        primary = bool(mods & {"ctrl", "meta"})
        if len(key) == 1 and not primary and "alt" not in mods:
            field.type(key)
            return
        extend = "shift" in mods
        match key:
            case "a" if primary:
                field.select_all()
            case "c" if primary:
                self._copy(field, ts)
            case "x" if primary:
                if cut := field.selected():
                    self._clipboard = cut
                    field.type("")
            case "v" if primary:
                self._paste(field, ts)
            case "z" | "y" if primary:
                field.forget()
            case "backspace" | "delete":
                unit = "line" if "meta" in mods else "word" if mods & {"ctrl", "alt"} else "char"
                field.delete(backward=key == "backspace", unit=unit)
            case "arrowleft" | "left" | "arrowright" | "right":
                field.move(field.step(right=key.endswith("right"), mods=mods), extend=extend)
            case "home" | "arrowup" | "up" | "pageup":
                field.move(0, extend=extend)
            case "end" | "arrowdown" | "down" | "pagedown":
                field.move(len(field.text), extend=extend)
            case _:
                field.settle()

    def _copy(self, field: _Field, ts: float) -> None:
        if selected := field.selected():
            self._clipboard = selected
            return
        self._clipboard = None
        self._copied = True
        self._out += [line for text in _pages_at(self._run, ts) for line in _lines(text)]

    def _paste(self, field: _Field, ts: float) -> None:
        if self._clipboard is not None:
            field.type(self._clipboard)
            return
        if not self._copied:
            self._copied = True
            self._out += [line for text in _pages_before(self._run, ts) for line in _lines(text)]
        field.forget()


_SETS_VALUE: Final = frozenset({"fill", "input", "input_text"})
"""Typing actions that replace a field's value; the others insert at the cursor."""
_MODIFIERS: Final = {
    "shift": "shift",
    "control": "ctrl",
    "ctrl": "ctrl",
    "controlormeta": "ctrl",
    "meta": "meta",
    "command": "meta",
    "cmd": "meta",
    "alt": "alt",
    "option": "alt",
}
_TYPED_KEY = re.compile(r"key([a-z])|digit(\d)|numpad(\d)")
_LINE_BREAK = re.compile(r"\n|(?<=[.!?])\s+")
COPIED_WORDS: Final = 32
"""Longest run of words a copied page contributes as one input."""


def _chord(chord: str) -> tuple[frozenset[str], str]:
    """The modifiers (shift, ctrl, meta, alt) and the lowercased key of a chord ("Control+A");
    a key that types a character ("KeyA", "Digit1", "Space") is that character."""
    if chord.endswith("++"):
        names, key = chord[:-2].split("+"), "+"
    else:
        *names, key = chord.split("+")
    mods = frozenset(_MODIFIERS.get(n.lower(), n.lower()) for n in names if n)
    lowered = key.lower()
    if lowered == "space":
        return mods, " "
    if typed := _TYPED_KEY.fullmatch(lowered):
        return mods, next(g for g in typed.groups() if g)
    return mods, lowered


def _word_left(text: str, at: int) -> int:
    while at > 0 and not text[at - 1].isalnum():
        at -= 1
    while at > 0 and text[at - 1].isalnum():
        at -= 1
    return at


def _word_right(text: str, at: int) -> int:
    while at < len(text) and not text[at].isalnum():
        at += 1
    while at < len(text) and text[at].isalnum():
        at += 1
    return at


def _pages_at(run: Run, ts: float) -> list[str]:
    """The text of the page on screen at `ts`: the last snapshot before it (else the initial
    state), and the next one if no other action ran first (else, at the end, the final state)."""
    snapshots = sorted(run.snapshots, key=lambda s: s.ts)
    before = [s.text for s in snapshots if s.ts <= ts]
    texts = before[-1:] or ([run.initial_state.text] if run.initial_state else [])
    acted = min((a.ts for a in run.actions if a.ts > ts), default=math.inf)
    after = next((s.text for s in snapshots if ts < s.ts < acted), None)
    if after is not None:
        texts.append(after)
    elif acted == math.inf and run.final_state is not None:
        texts.append(run.final_state.text)
    return texts


def _pages_before(run: Run, ts: float) -> list[str]:
    """The text of every page seen up to `ts`, the initial state first."""
    initial = [run.initial_state.text] if run.initial_state else []
    return initial + [s.text for s in run.snapshots if s.ts <= ts] + _pages_at(run, ts)


def _lines(text: str) -> Iterator[str]:
    """Each line and sentence of page text. A long one comes as overlapping runs of
    COPIED_WORDS words, every COPIED_WORDS // 2 words: any part of it up to half that long is
    in one run, and a longer one is covered by the runs it overlaps."""
    for line in _LINE_BREAK.split(text):
        words = line.split()
        stride = COPIED_WORDS // 2
        for at in range(0, max(1, len(words) - stride), stride):
            if run := words[at : at + COPIED_WORDS]:
                yield " ".join(run)


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

    An echo is an occurrence of the whole input or of a part of it at least MIN_ECHO_PART
    characters long that starts where a word of the input starts (the site dropped words, or a
    field's maxlength cut the input short); overlapping echoes become one MASK.
    """
    norm = normalize(text)
    spans = sorted(_echoes(norm, inputs))
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


type _Part = tuple[str, bool, tuple[tuple[int, int], ...]]
"""An input from one of its word starts on: the text, whether it is the whole input, and the
redaction tokens in it."""


class _Echoes:
    """Finds echoes of a set of inputs in text.

    Every part of an input that can be echoed alone starts at a word start and has at least
    MIN_ECHO_PART characters, so its first MIN_ECHO_PART characters (its head) occur wherever
    it does. Heads are indexed once per set of inputs. A text is searched for the heads it
    contains, and each occurrence, from left to right, is extended as far as each part with
    that head matches; a part too short to reach past the echoes found so far is skipped, so
    repetitive text costs little more than plain text. Inputs shorter than MIN_ECHO_PART only
    count as whole.
    """

    SCAN_HEADS: Final = 64
    """Above this many heads, a text's windows are collected once instead of searching for
    each head."""

    def __init__(self, inputs: tuple[str, ...]) -> None:
        self.whole = [value for value in inputs if len(value) < MIN_ECHO_PART]
        parts: dict[str, set[_Part]] = {}
        for value in inputs:
            if len(value) < MIN_ECHO_PART:
                continue
            tokens = [m.span() for m in _REDACTION_TOKEN.finditer(value)]
            for start in _word_starts(value, tokens):
                rest = tuple((s - start, e - start) for s, e in tokens if e > start)
                part = (value[start:], start == 0, rest)
                parts.setdefault(value[start : start + MIN_ECHO_PART], set()).add(part)
        self.heads = {
            head: sorted(found, key=lambda p: (-len(p[0]), p)) for head, found in parts.items()
        }

    def spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for value in self.whole:
            at = text.find(value)
            while at != -1:
                spans.append((at, at + len(value)))
                at = text.find(value, at + 1)
        if len(self.heads) > self.SCAN_HEADS:
            windows = {text[i : i + MIN_ECHO_PART] for i in range(len(text) - MIN_ECHO_PART + 1)}
            found = [head for head in windows if head in self.heads]
        else:
            found = [head for head in self.heads if head in text]
        covered = -1
        for at, head in sorted(_occurrences(text, found)):
            for part in self.heads[head]:
                if at + len(part[0]) <= covered:
                    break
                if (length := _echo_length(text, at, part)) is not None:
                    spans.append((at, at + length))
                    covered = max(covered, at + length)
        return spans


def _occurrences(text: str, heads: list[str]) -> Iterator[tuple[int, str]]:
    for head in heads:
        at = text.find(head)
        while at != -1:
            yield at, head
            at = text.find(head, at + 1)


def _word_starts(value: str, tokens: list[tuple[int, int]]) -> Iterator[int]:
    """Where the parts of `value` that can be echoed alone start: its start, and every word
    start at least MIN_ECHO_PART characters from its end that is not inside a redaction token."""
    yield 0
    for match in WORD.finditer(value, 1, len(value) - MIN_ECHO_PART + 1):
        at = match.start()
        if WORD.match(value, at - 1) is None and not any(s < at < e for s, e in tokens):
            yield at


def _echo_length(text: str, at: int, part: _Part) -> int | None:
    """The length of the echo of `part` at `text[at:]`, or None if it is too short.

    The echo extends as far as `part` matches and is a whole input or at least MIN_ECHO_PART
    characters. A prefix may end anywhere (a maxlength cut the input short); a part after the
    first word ends at a word end (the site dropped words around it). No echo ends inside a
    redaction token: distinct tokens share a long prefix.
    """
    rest, prefix, tokens = part
    end = _match_length(text, at, rest, MIN_ECHO_PART)
    if prefix and end == len(rest):
        return end
    end = next((s for s, e in tokens if s < end < e), end)
    while not prefix and 0 < end < len(rest) and _inside_word(rest, end):
        end -= 1
    return end if end >= MIN_ECHO_PART else None


def _inside_word(value: str, at: int) -> bool:
    """`value[at]` continues the word of `value[at - 1]`."""
    return WORD.fullmatch(value, at - 1, at + 1) is not None


@functools.lru_cache(maxsize=64)
def _echo_index(inputs: tuple[str, ...]) -> _Echoes:
    return _Echoes(inputs)


def _echoes(text: str, inputs: tuple[str, ...]) -> list[tuple[int, int]]:
    """The span of every echo of every input in `text`."""
    return _echo_index(inputs).spans(text)


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


def _masked_request(event: NetworkEvent, inputs: tuple[str, ...]) -> NetworkEvent:
    masked = event if event.is_mutation else _masked_url(event, inputs)
    body = None if event.response_body is None else _masked_body(event.response_body, inputs)
    if body is None:
        return masked
    return masked.model_copy(update={"response_body": body})


def _masked_body(body: str, inputs: tuple[str, ...]) -> str | None:
    """`body` with every echo masked, or None if it has none. JSON keeps its structure."""
    try:
        data: object = json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        data = None
    if isinstance(data, dict | list):
        masked = _masked_json(cast(object, data), inputs)
        return None if masked == data else json.dumps(masked, ensure_ascii=False)
    tail = _TRUNCATION_TAIL.search(body)
    cut = tail.start() if tail else len(body)
    text = _mask(body[:cut], inputs)
    return None if text is None else text + body[cut:]


def _masked_json(data: object, inputs: tuple[str, ...]) -> object:
    match data:
        case str():
            masked = _mask(data, inputs)
            return data if masked is None else masked
        case dict():
            return {
                _mask(key, inputs) or key: _masked_json(value, inputs)
                for key, value in cast(dict[str, object], data).items()
            }
        case list():
            return [_masked_json(v, inputs) for v in cast(list[object], data)]
        case _:
            return data


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
