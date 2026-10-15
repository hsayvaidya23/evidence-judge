"""Echoes of agent input in evidence: where the site repeats back what the agent put in, and
the evidence with every echo masked."""

from __future__ import annotations

import bisect
import functools
import json
import re
from collections.abc import Iterator
from typing import Final, cast
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urlsplit, urlunsplit

from evidence_judge.core.evidence import ActionEvent, NetworkEvent, PageSnapshot, PageState
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
    spans = sorted(_echoes(norm, inputs, folded=folded))
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


class _Folded:
    """Text with only its letters and digits kept (`text`), and where its words start and end
    in it; `original` maps a span of it back to the text it came from."""

    def __init__(self, text: str) -> None:
        self._runs = [m.span() for m in WORD.finditer(text)]
        self.text = "".join(text[s:e] for s, e in self._runs)
        self.starts: list[int] = []
        self.ends: set[int] = set()
        at = 0
        for s, e in self._runs:
            self.starts.append(at)
            at += e - s
            self.ends.add(at)
        self.start_set = frozenset(self.starts)

    def original(self, lo: int, hi: int) -> tuple[int, int]:
        first = bisect.bisect_right(self.starts, lo) - 1
        last = bisect.bisect_right(self.starts, hi - 1) - 1
        return (
            self._runs[first][0] + lo - self.starts[first],
            self._runs[last][0] + hi - self.starts[last],
        )


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
    text = mask(body[:cut], inputs)
    return None if text is None else text + body[cut:]


def _masked_json(data: object, inputs: tuple[str, ...]) -> object:
    match data:
        case str():
            masked = mask(data, inputs)
            return data if masked is None else masked
        case dict():
            return {
                mask(key, inputs) or key: _masked_json(value, inputs)
                for key, value in cast(dict[str, object], data).items()
            }
        case list():
            return [_masked_json(v, inputs) for v in cast(list[object], data)]
        case _:
            return data


def masked_page[S: (PageSnapshot, PageState)](page: S, inputs: tuple[str, ...]) -> S:
    text = mask(page.text, inputs)
    update = {"url": _mask_url(page.url, inputs)}
    if text is not None:
        update["text"] = text
    return page.model_copy(update=update)
