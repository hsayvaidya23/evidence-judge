"""What the agent put into a run itself, replayed from its actions: typed text and the values
fields took as it edited them, text it copied from pages, the options it chose in drop-downs,
uploaded file names and file text, the query keys and values of URLs it opened and GET
requests it sent, and the path and fragment of URLs it opened;
and every value a form field held in a snapshot, however it got there."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Iterator
from pathlib import PureWindowsPath
from typing import Final
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

from evidence_judge.core.actions import (
    FILL,
    GOTO,
    KEY_DOWN,
    KEY_UP,
    PRESS,
    SELECT,
    UPLOAD,
    canonical_action,
)
from evidence_judge.core.evidence import ActionEvent, Run
from evidence_judge.core.site import Site
from evidence_judge.ingest._fragment import fragment_text
from evidence_judge.textnorm import normalize

_KEY_NAME = re.compile(
    r"(?:.|shift|control|ctrl|alt|option|meta|command|cmd|controlormeta|enter|return|tab|space"
    r"|escape|esc|backspace|delete|del|insert|home|end|pageup|pagedown|capslock|numlock"
    r"|scrolllock|pause|printscreen|contextmenu|arrow(?:up|down|left|right)|up|down|left|right"
    r"|f\d{1,2}|key[a-z]|digit\d|numpad\w+)",
    re.IGNORECASE,
)
_TAG = re.compile(r"</?[a-z][^<>]*>", re.IGNORECASE)
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
_SHORTCUTS: Final = frozenset("acxvzy")
"""Keys that, with Control or Meta, are editing shortcuts."""
_TYPED_KEY = re.compile(r"key([a-z])|digit(\d)|numpad(\d)")
_FRAGMENT_PARTS = re.compile(r"[/?&=]")
_LINE_BREAK = re.compile(r"\n|(?<=[.!?])\s+")
COPIED_WORDS: Final = 32
"""Longest run of words a copied page contributes as one input."""


def raw_inputs(run: Run) -> Iterator[tuple[str, Site | None]]:
    typing = _Typing(run)
    yield from _action_inputs(run, typing)
    yield from ((text, None) for text in typing.finish())
    for snapshot in run.snapshots:
        yield from ((value, None) for value in snapshot.field_values)
    for event in run.network:
        if event.method.upper() == "GET":
            yield from (
                (v, Site.of_url(event.url)) for v in _url_inputs(event.url, with_path=False)
            )


def _action_inputs(run: Run, typing: _Typing) -> Iterator[tuple[str, Site | None]]:
    """Chosen options, uploaded file names and text, and the URL values of pages the agent
    opened itself, as `typing` replays every action."""
    served = _site_served(run)
    last = -math.inf
    for action in run.actions:
        before, last = last, action.ts
        name = canonical_action(action.name)
        typing.act(action, name)
        if name == UPLOAD:
            yield from ((text, None) for text in _uploaded(action))
        elif name == SELECT and action.value:
            yield from ((text, None) for text in _chosen(action.value))
        elif name == GOTO and (url := action.value or action.url):
            opened = _agent_opened(url, served, before)
            values = _url_inputs(url, with_path=True) if opened else _fragment_inputs(url)
            yield from ((v, Site.of_url(url)) for v in values)


def _uploaded(action: ActionEvent) -> Iterator[str]:
    """The uploaded files' names, and each line and sentence of the text they hold."""
    if action.value:
        yield from _file_names(action.value)
    if action.file_text:
        yield from _lines(action.file_text)


def _chosen(value: str) -> Iterator[str]:
    """The options a select action chose (their values or labels, as recorded): the whole value
    and each of several joined by ", "."""
    yield from dict.fromkeys([value, *value.split(", ")])


def _agent_opened(url: str, served: dict[str, float], before: float) -> bool:
    """A GOTO of `url` opened it, unless the browser had already loaded it, or been redirected
    to it, before the previous action (its fragment aside, which the site never sees)."""
    return served.get(_page_key(url), math.inf) >= before


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

    def press(self, key: str, mods: frozenset[str]) -> None:
        """A key that deletes or moves the cursor (Shift extends the selection); any other key
        ends the edit."""
        extend = "shift" in mods
        match key:
            case "backspace" | "delete":
                unit = "line" if "meta" in mods else "word" if mods & {"ctrl", "alt"} else "char"
                self.delete(backward=key == "backspace", unit=unit)
            case "arrowleft" | "left" | "arrowright" | "right":
                self.move(self.step(right=key.endswith("right"), mods=mods), extend=extend)
            case "home" | "arrowup" | "up" | "pageup":
                self.move(0, extend=extend)
            case "end" | "arrowdown" | "down" | "pagedown":
                self.move(len(self.text), extend=extend)
            case _:
                self.settle()

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
    V (paste). A key held down (KEY_DOWN) is pressed; a modifier held down applies to every key
    pressed until it is released (KEY_UP), and releasing any other key does nothing. The value is
    an input whenever the agent stops editing the field (another key,
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
        self._held: dict[str, str] = {}

    def act(self, action: ActionEvent, name: str) -> None:
        value = action.value
        if name in (KEY_DOWN, KEY_UP) and value is not None:
            if self._hold(value, down=name == KEY_DOWN) or name == KEY_UP:
                return
            name = PRESS
        if name == PRESS and value is not None:
            value = self._with_held(value)
        if name not in (PRESS, FILL) or value is None:
            self._leave(action, name)
            return
        target = self._focus if action.selector is None else action.selector
        self._focus = target
        field = self._fields.setdefault(target, _Field(self._out))
        if name == PRESS and len(value) == 1:
            self._press_character(field, target, value)
            return
        self._flush_keys()
        if name == PRESS and _is_key_names(value):
            for chord in value.split():
                self._key(field, chord, action.ts)
            return
        self._type(field, action, name, value)

    def _hold(self, key: str, *, down: bool) -> bool:
        """Hold or release `key` if it is a modifier; False for any other key."""
        modifier = _MODIFIERS.get(key.lower())
        if modifier is None:
            return False
        if down:
            self._held[modifier] = key
        else:
            self._held.pop(modifier, None)
        return True

    def _with_held(self, value: str) -> str:
        """`value` pressed with the held modifiers: a chord ("ctrl+a"). Shift alone leaves a
        character as typed."""
        held = [m for m in self._held if m != "shift" or len(value) != 1]
        if not held or not _is_key_names(value):
            return value
        key = "Space" if value == " " else value
        return "+".join([*held, key])

    def _type(self, field: _Field, action: ActionEvent, name: str, value: str) -> None:
        if value:
            self._out.append(value)
        if name == FILL and normalize(action.name).replace(" ", "_") in _SETS_VALUE:
            field.set(value)
        else:
            field.type(value)

    def _leave(self, action: ActionEvent, name: str) -> None:
        """An action that types nothing: the agent stops editing, and a GOTO leaves the page."""
        self._flush_keys()
        self._settle()
        if name == GOTO:
            self._fields.clear()
        if action.selector is not None:
            self._focus = action.selector

    def _press_character(self, field: _Field, target: str | None, key: str) -> None:
        if self._keys and target != self._keys_target:
            self._flush_keys()
        self._keys_target = target
        self._keys.append(key)
        field.type(key)

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
        if primary and key in _SHORTCUTS:
            self._shortcut(field, key, ts)
        else:
            field.press(key, mods)

    def _shortcut(self, field: _Field, key: str, ts: float) -> None:
        """Control or Meta with A (select all), C (copy), X (cut), V (paste), Z or Y (undo, redo)."""
        match key:
            case "a":
                field.select_all()
            case "c":
                self._copy(field, ts)
            case "x":
                if cut := field.selected():
                    self._clipboard = cut
                    field.type("")
            case "v":
                self._paste(field, ts)
            case _:
                field.forget()

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


def rendered(text: str) -> tuple[str, ...]:
    """`text`, and how a page that renders it as HTML shows it: character references decoded
    and tags dropped (inline) or turned into a break (block); and, if it has markup, its page
    text as a fragment of the page (ingest._fragment): comments, scripts, styles and what the
    markup hides (``hidden``, ``display: none``) left out."""
    forms = (text, *(html.unescape(_TAG.sub(gap, text)) for gap in ("", " ")))
    return (*forms, fragment_text(text)) if "<" in text else forms


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
    """The decoded query keys and values of `url`; `with_path`, also its path segments and its
    fragment (`_fragment_inputs`)."""
    parts = urlsplit(url)
    values = [text for pair in parse_qsl(parts.query, keep_blank_values=True) for text in pair]
    if with_path:
        values += [unquote(segment) for segment in parts.path.split("/")]
        values += _fragment_inputs(url)
    return values


def _fragment_inputs(url: str) -> list[str]:
    """The decoded fragment of a URL the agent opened, and each part of it between "/", "?",
    "&" and "=": the site never chose it, even on a page it served, and its script can show it
    ("#In%20stock" shown as "Showing: In stock")."""
    fragment = urlsplit(url).fragment
    if not fragment:
        return []
    return [unquote(fragment), *(unquote(part) for part in _FRAGMENT_PARTS.split(fragment))]
