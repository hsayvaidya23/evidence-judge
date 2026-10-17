"""Echoes of agent input in evidence: where the site repeats back what the agent put in, and
the evidence with every echo masked."""

from __future__ import annotations

import bisect
import difflib
import functools
import json
import re
import unicodedata
from collections.abc import Callable, Iterator
from typing import Final, cast
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urlsplit, urlunsplit

from evidence_judge.core.actions import FILL, PRESS, canonical_action
from evidence_judge.core.evidence import (
    ActionEvent,
    NetworkEvent,
    PageSnapshot,
    PageState,
    Run,
)
from evidence_judge.core.predicates import MIN_PAGE_TEXT, WORD
from evidence_judge.redact import TOKEN_RE
from evidence_judge.textnorm import normalize

_REDACTION_TOKEN = re.compile(TOKEN_RE.pattern, re.IGNORECASE)
MIN_ECHO_PART: Final = 8
"""Shortest part of an agent input (from one of its word starts) that counts as an echo of it.
Shorter shared parts are mostly common word stems ("confirm" in "confirmation" and
"confirmed")."""
_TRUNCATION_TAIL = re.compile(r"…\[truncated[^\]]{0,60}\]\s*\Z")
MASK: Final = "\x00"
"""Stands in for agent input in masked evidence; it is never part of a predicate."""


def mask(text: str, inputs: tuple[str, ...], *, folded: bool = True) -> str | None:
    """`text` normalised with every echo of an input replaced by MASK, or None if it has none.

    An echo is an occurrence of the whole input or of a part of it at least MIN_ECHO_PART
    characters long that starts where a word of the input starts (the site dropped words, or a
    field's maxlength cut the input short); overlapping echoes become one MASK. Each is also
    matched folded (see _FoldedEchoes) unless `folded` is False: "instock" and "in-stock" echo
    as "In stock".
    """
    norm = normalize(text)
    return _replaced(norm, _echoes(norm, inputs, folded=folded))


def mask_covered(text: str, needles: tuple[str, ...], inputs: tuple[str, ...]) -> str | None:
    """`text` normalised with every occurrence of a needle that the agent's inputs make word by
    word replaced by MASK, or None if there is none.

    A needle's occurrence is covered when every word of it lies in the echoes of the inputs
    (`mask`'s echoes, exact or folded, of inputs of any length, and every folded occurrence of an
    input too short for them): "In" typed in one field and
    "stock" in another make "In stock" between them. One word the agent did not put in leaves the
    occurrence alone, so "Red Kettle" searched for does not cover "Thank you for your order".
    """
    norm = normalize(text)
    found = [(at, at + len(n)) for at, n in _occurrences(norm, [*dict.fromkeys(needles)]) if n]
    if not found:
        return None
    covered = _union(_cover_spans(norm, inputs))
    return _replaced(
        norm,
        [
            span
            for span in found
            if _words_covered(norm, span, covered) or _near_input(norm[slice(*span)], inputs)
        ],
    )


NEAR_EDITS: Final = 2
"""Most letter edits (an insertion, deletion or swap of two neighbours; a substitution counts
as two) that turn the letters of an input into a text the site derived from it; one for a text
of at most SHORT_NEAR letters. A substitution costs two so that "restock" typed is not "In
stock" shown."""
SHORT_NEAR: Final = 4


def _near_input(occurrence: str, inputs: tuple[str, ...]) -> bool:
    """`occurrence` is what the site made of an input by changing a few of its letters: its
    letters (`_Folded`) are at most NEAR_EDITS edits from the letters of one input, read forwards
    or backwards from one of its word starts, with words joined or split, a letter added or
    dropped: "instok" typed and "Did you mean: In stock?" shown, "Ins stocks" typed and "Showing
    results for In stock" shown, "kcots ni" typed and the page turning it round. Inputs with a
    redaction token never count: its fingerprint is random, not letters the agent typed, so
    "[REDACTED:secret_input:f1140042]" is not near "$24.00"."""
    folded = _Folded(occurrence).text
    return bool(folded) and any(
        _near(folded, value) for value in inputs if not _REDACTION_TOKEN.search(value)
    )


@functools.lru_cache(maxsize=4096)
def _near(folded: str, value: str) -> bool:
    edits = 1 if len(folded) <= SHORT_NEAR else NEAR_EDITS
    forms = (_Folded(value), _Folded(value[::-1]))
    if len(forms[0].text) < len(folded) - 1:
        return False
    return any(_part_edits(folded, form.text, form.starts) <= edits for form in forms)


def _part_edits(text: str, value: str, starts: list[int]) -> int:
    """The fewest edits that turn `text` into a part of `value` that starts at one of `starts`
    (its word starts)."""
    best = len(text)
    for start in starts:
        best = min(best, _prefix_edits(text, value[start : start + len(text) + NEAR_EDITS]))
        if best == 0:
            break
    return best


def _prefix_edits(text: str, value: str) -> int:
    """The fewest edits (optimal string alignment, a substitution counting as two, NEAR_EDITS)
    that turn `text` into a prefix of `value` at
    most one letter shorter than it: the site adds at most one letter to what the agent typed, so
    "stock" typed does not make "In stock"."""
    rows = [list(range(len(value) + 1))]
    for i, char in enumerate(text, 1):
        row = [i] + [0] * len(value)
        for j in range(1, len(value) + 1):
            row[j] = min(
                rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + 2 * (char != value[j - 1])
            )
            if i > 1 and j > 1 and char == value[j - 2] and text[i - 2] == value[j - 1]:
                row[j] = min(row[j], rows[-2][j - 2] + 1)
        rows.append(row)
    return min(rows[-1][max(0, len(text) - 1) :], default=len(text))


def _replaced(norm: str, spans: list[tuple[int, int]]) -> str | None:
    """`norm` with `spans` replaced by MASK (overlapping ones by one), or None if there are none."""
    if not spans:
        return None
    parts: list[str] = []
    end = 0
    for start, stop in sorted(spans):
        if start < end:
            end = max(end, stop)
            continue
        parts += [norm[end:start], MASK]
        end = stop
    parts.append(norm[end:])
    return "".join(parts)


def _cover_spans(norm: str, inputs: tuple[str, ...]) -> list[tuple[int, int]]:
    """Every echo of `inputs` in `norm`, and every occurrence, folded, of an input that folds to
    fewer than MIN_PAGE_TEXT characters: "I" and "n stock" make "In stock" between them. A short
    input inside another word covers only its own letters, so it never covers a word alone."""
    spans = _echoes(norm, tuple(i for i in inputs if len(i) >= MIN_PAGE_TEXT), folded=True)
    short = {f for i in inputs if 0 < len(f := _Folded(i).text) < MIN_PAGE_TEXT}
    if short:
        folded = _Folded(norm)
        spans += [
            folded.original(at, at + len(value))
            for at, value in _occurrences(folded.text, sorted(short))
        ]
    return spans


def _union(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def _words_covered(norm: str, span: tuple[int, int], covered: list[tuple[int, int]]) -> bool:
    """Every word of `norm[span]` (as far as it is inside the span) lies in one of `covered`."""
    words = [m.span() for m in WORD.finditer(norm, *span)]
    starts = [start for start, _ in covered]
    for start, stop in words:
        at = bisect.bisect_right(starts, start) - 1
        if at < 0 or covered[at][1] < stop:
            return False
    return bool(words)


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
        spans = [(at, at + len(value)) for at, value in _occurrences(text, self.whole)]
        covered = -1
        for at, head in sorted(_occurrences(text, self._heads_in(text))):
            for part in self.heads[head]:
                if at + len(part[0]) <= covered:
                    break
                if (length := _echo_length(text, at, part)) is not None:
                    spans.append((at, at + length))
                    covered = max(covered, at + length)
        return spans

    def _heads_in(self, text: str) -> list[str]:
        if len(self.heads) > self.SCAN_HEADS:
            windows = {text[i : i + MIN_ECHO_PART] for i in range(len(text) - MIN_ECHO_PART + 1)}
            return [head for head in windows if head in self.heads]
        return [head for head in self.heads if head in text]


_LOOKALIKES: Final = str.maketrans("013457@$l" + chr(0x131), "oieastasii")
"""Characters a site or an agent writes for a letter ("1n 5t0ck", "$ale", a Turkish dotless
i), and the letter each folds to: "l" and "1" both fold to "i", as the dotless i does."""


@functools.cache
def _fold_char(char: str) -> str | None:
    """The folded form of `char`: its letters and digits without accents, case or look-alikes;
    "" for a combining mark (it continues a word), None for a character that ends one."""
    if unicodedata.combining(char):
        return ""
    plain = "".join(
        c for c in unicodedata.normalize("NFKD", char.casefold()) if not unicodedata.combining(c)
    )
    return "".join(c for c in plain.translate(_LOOKALIKES) if c.isalnum()) or None


class _Folded:
    """Text with only its letters and digits kept, without accents, case or look-alikes
    (`_fold_char`: "İñ ştöçk" and "1n 5t0ck" fold as "In stock" does), and where its words
    start and end in it; `original` maps a span of it back to the text it came from."""

    def __init__(self, text: str) -> None:
        self.starts: list[int] = []
        self.ends: set[int] = set()
        self._from: list[int] = []
        self._to: list[int] = []
        if text.isascii():
            mapped = text.lower().translate(_LOOKALIKES)
            runs = [m.span() for m in WORD.finditer(mapped)]
            self.text = "".join(mapped[s:e] for s, e in runs)
            for s, e in runs:
                self.starts.append(len(self._from))
                self._from += range(s, e)
                self._to += range(s + 1, e + 1)
                self.ends.add(len(self._from))
        else:
            self.text = self._fold(text)
        self.start_set = frozenset(self.starts)

    def _fold(self, text: str) -> str:
        chars: list[str] = []
        inside = False
        for at, char in enumerate(text):
            folded = _fold_char(char)
            if folded is None:
                if inside:
                    self.ends.add(len(chars))
                inside = False
            elif not folded:
                if inside:
                    self._to[-1] = at + 1
            else:
                if not inside:
                    self.starts.append(len(chars))
                    inside = True
                for c in folded:
                    chars.append(c)
                    self._from.append(at)
                    self._to.append(at + 1)
        if inside:
            self.ends.add(len(chars))
        return "".join(chars)

    def original(self, lo: int, hi: int) -> tuple[int, int]:
        return self._from[lo], self._to[hi - 1]


type _FoldedPart = tuple[str, bool, frozenset[int]]
"""A folded input from one of its word starts on: the text, whether it is from the first word,
and where its words end in it."""


class _FoldedEchoes:
    """Finds echoes of a set of inputs that differ from them only in case, spaces and
    punctuation: "instock" typed and "Did you mean: In stock?" shown, "in-stock" in a URL and
    "Showing: In stock" on its page.

    Inputs and text are folded (letters and digits kept). A folded echo starts at a word start
    and ends at a word end of the text, so typing "tin" does not mask "got in". Like an echo
    (_Echoes), it is a whole input, or a part of at least MIN_ECHO_PART folded characters from
    one of the input's word starts: a part from the first word may end anywhere in the input, a
    later part ends at one of its word ends. Inputs with a redaction token, or that fold to
    fewer than MIN_PAGE_TEXT characters, are matched exactly only.
    """

    def __init__(self, inputs: tuple[str, ...]) -> None:
        whole: set[str] = set()
        parts: dict[str, set[_FoldedPart]] = {}
        for value in inputs:
            if _REDACTION_TOKEN.search(value):
                continue
            folded = _Folded(value)
            if len(folded.text) < MIN_PAGE_TEXT:
                continue
            if len(folded.text) < MIN_ECHO_PART:
                whole.add(folded.text)
                continue
            for index, start in enumerate(folded.starts):
                rest = folded.text[start:]
                if len(rest) < MIN_ECHO_PART:
                    break
                ends = frozenset(e - start for e in folded.ends if e > start)
                parts.setdefault(rest[:MIN_ECHO_PART], set()).add((rest, index == 0, ends))
        self.whole = sorted(whole)
        self.heads = {
            head: sorted(found, key=lambda p: (-len(p[0]), p[0])) for head, found in parts.items()
        }

    def spans(self, text: str) -> list[tuple[int, int]]:
        if not self.whole and not self.heads:
            return []
        folded = _Folded(text)
        found = [
            (at, at + len(value))
            for at, value in _occurrences(folded.text, self.whole)
            if at in folded.start_set and at + len(value) in folded.ends
        ]
        heads = [head for head in self.heads if head in folded.text]
        covered = -1
        for at, head in sorted(_occurrences(folded.text, heads)):
            if at not in folded.start_set:
                continue
            for rest, first, ends in self.heads[head]:
                if at + len(rest) <= covered:
                    break
                if (end := _folded_end(folded, at, rest, first, ends)) is not None:
                    found.append((at, end))
                    covered = max(covered, end)
        return [folded.original(lo, hi) for lo, hi in found]


def _folded_end(
    folded: _Folded, at: int, rest: str, first: bool, ends: frozenset[int]
) -> int | None:
    """Where the folded echo of `rest` at `folded.text[at:]` ends, or None if it is too short:
    the furthest word end of the text it matches to that is also a word end of `rest`, unless
    `rest` is from the input's first word."""
    length = _match_length(folded.text, at, rest, MIN_ECHO_PART)
    for end in range(length, MIN_ECHO_PART - 1, -1):
        if at + end in folded.ends and (first or end in ends):
            return at + end
    return None


def _occurrences(text: str, heads: list[str]) -> Iterator[tuple[int, str]]:
    """Where each of `heads` occurs in `text`, overlapping occurrences included."""
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
def _echo_index(inputs: tuple[str, ...]) -> tuple[_Echoes, _FoldedEchoes]:
    return _Echoes(inputs), _FoldedEchoes(inputs)


def _echoes(text: str, inputs: tuple[str, ...], *, folded: bool) -> list[tuple[int, int]]:
    """The span of every echo of every input in `text`, exact or (with `folded`) folded."""
    exact, fold = _echo_index(inputs)
    return exact.spans(text) + (fold.spans(text) if folded else [])


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
    """`url` with every exact echo masked. Folded echoes are left: a site's slug of what the
    agent searched for ("Red Kettle" typed, `/products/red-kettle` opened) is where an honest
    search leads."""
    parts = urlsplit(url)
    path = mask(unquote(parts.path), inputs, folded=False)
    query = mask(unquote_plus(parts.query), inputs, folded=False)
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


def masked_url[E: (ActionEvent, NetworkEvent)](event: E, inputs: tuple[str, ...]) -> E:
    if event.url is None:
        return event
    return event.model_copy(update={"url": _mask_url(event.url, inputs)})


def masked_request(event: NetworkEvent, inputs: tuple[str, ...]) -> NetworkEvent:
    masked = event if event.is_mutation else masked_url(event, inputs)
    return _with_body(masked, event.response_body, lambda text: mask(text, inputs))


def covered_request(
    event: NetworkEvent, needles: tuple[str, ...], inputs: tuple[str, ...]
) -> NetworkEvent:
    """`event` with every occurrence of a needle its response body holds that the agent's
    inputs make word by word (`mask_covered`) masked."""
    return _with_body(event, event.response_body, lambda text: mask_covered(text, needles, inputs))


def _with_body(event: NetworkEvent, body: str | None, masker: _Masker) -> NetworkEvent:
    masked = None if body is None else _masked_body(body, masker)
    return event if masked is None else event.model_copy(update={"response_body": masked})


type _Masker = Callable[[str], str | None]
"""Text with what it masks replaced by MASK, or None if there is nothing to mask."""


def _masked_body(body: str, masker: _Masker) -> str | None:
    """`body` masked, or None if nothing in it is. JSON keeps its structure."""
    try:
        data: object = json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        data = None
    if isinstance(data, dict | list):
        masked = _masked_json(cast(object, data), masker)
        return None if masked == data else json.dumps(masked, ensure_ascii=False)
    tail = _TRUNCATION_TAIL.search(body)
    cut = tail.start() if tail else len(body)
    text = masker(body[:cut])
    return None if text is None else text + body[cut:]


def _masked_json(data: object, masker: _Masker) -> object:
    match data:
        case str():
            masked = masker(data)
            return data if masked is None else masked
        case dict():
            return {
                masker(key) or key: _masked_json(value, masker)
                for key, value in cast(dict[str, object], data).items()
            }
        case list():
            return [_masked_json(v, masker) for v in cast(list[object], data)]
        case _:
            return data


def masked_page[S: (PageSnapshot, PageState)](page: S, inputs: tuple[str, ...]) -> S:
    text = mask(page.text, inputs)
    update = {"url": _mask_url(page.url, inputs)}
    if text is not None:
        update["text"] = text
    return page.model_copy(update=update)


def covered_page[S: (PageSnapshot, PageState)](
    page: S, needles: tuple[str, ...], inputs: tuple[str, ...]
) -> S:
    """`page` with every occurrence of a needle in its text that the agent's inputs make word by
    word (`mask_covered`) masked."""
    text = mask_covered(page.text, needles, inputs)
    return page if text is None else page.model_copy(update={"text": text})


def query_derived(run: Run) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """The phrases of each page state's text the site may have made from what the agent
    searched for, by snapshot id, and those of the final state.

    A page can translate, correct or look up what the agent typed ("disponible" typed,
    "Results for: In stock" shown), so no edit distance ties its text to the input. Text is
    query-derived when it first shows on a page right after the agent typed into it (a live
    search: every phrase the state before it on the same page, same URL path, did not show), or
    on a page a form the agent typed into sent it to, whose URL query carries what it typed (a
    submitted search: all of it). It stays query-derived on later states of that page. Text
    that appears after a click, or on a page the agent opened from the results, is the site's
    own: an honest search proves the product's stock on the product page, not on the results.
    """
    typed = {
        folded
        for a in run.actions
        if canonical_action(a.name) == FILL
        and a.value
        and len(folded := _Folded(normalize(a.value)).text) >= MIN_PAGE_TEXT
    }
    if not typed:
        return {}, ()
    by_call = {a.call_id: a for a in run.actions if a.call_id}
    starts = [a.ts for a in run.actions]
    fills = [a for a in run.actions if canonical_action(a.name) == FILL]
    found: dict[str, tuple[str, ...]] = {}
    page: str | None = None
    before: list[str] | None = None
    phrases: list[str] = []
    for snapshot in run.snapshots:
        key = _page_key(snapshot.url)
        words = normalize(snapshot.text).split()
        left = page
        if key != page:
            page, before, phrases = key, None, []
        cause = by_call.get(snapshot.call_id or "")
        if cause is None and (at := bisect.bisect_right(starts, snapshot.ts)) > 0:
            cause = run.actions[at - 1]
        name = "" if cause is None else canonical_action(cause.name)
        if before is not None:
            live = name in (FILL, PRESS)
        else:
            live = _submitted(fills, snapshot.ts, left) and _carries(snapshot.url, typed)
        if live:
            phrases += _added(before or [], words)
        if phrases:
            found[snapshot.id] = tuple(dict.fromkeys(phrases))
        before = words
    final = run.final_state
    if final is None:
        return found, ()
    if final.snapshot_id is not None or _page_key(final.url) != page:
        return found, found.get(final.snapshot_id or "", ())
    return found, tuple(dict.fromkeys(phrases))


def masked_derived[S: (PageSnapshot, PageState)](page: S, phrases: tuple[str, ...]) -> S:
    """`page` with every occurrence of each of `phrases` in its text replaced by MASK."""
    if not phrases:
        return page
    norm = normalize(page.text)
    text = _replaced(norm, [(at, at + len(p)) for at, p in _occurrences(norm, list(phrases))])
    return page if text is None else page.model_copy(update={"text": text})


def _page_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _submitted(fills: list[ActionEvent], ts: float, left: str | None) -> bool:
    """The agent's last typing before `ts` was on the page it left (`left`): the page loaded
    is what a form it typed into sent it to, not a link it followed from a results page."""
    last = next((a for a in reversed(fills) if a.ts <= ts), None)
    return last is not None and last.url is not None and _page_key(last.url) == left


def _carries(url: str, typed: set[str]) -> bool:
    """`url`'s query or fragment holds something the agent typed."""
    parts = urlsplit(url)
    folded = _Folded(unquote_plus(f"{parts.query} {parts.fragment}")).text
    return any(value in folded for value in typed)


def _added(before: list[str], after: list[str]) -> list[str]:
    """The runs of words of `after` that `before` does not have where `after` has them."""
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    return [
        " ".join(after[j1:j2])
        for op, _, _, j1, j2 in matcher.get_opcodes()
        if op in ("insert", "replace")
    ]
