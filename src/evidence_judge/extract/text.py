"""Deterministic text helpers shared by extractors: normalisation, sentences, completion verbs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from evidence_judge.textnorm import PUNCT_MAP, normalize

Origin = Literal["answer", "reasoning"]

_SENTENCE_BOUNDARY = re.compile(
    r"(?<!\bNo\.)(?<!\bno\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\be\.g\.)(?<!\bi\.e\.)"
    r"(?<=[.!?])[\"')\]]*\s+|\n\s*\n|\n(?=\s*(?:[-*•]|\d+[.)])\s)"
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_CLAUSE_BREAK = re.compile(r"[,;:]|\bbut\b|\bhowever\b|\balthough\b|\bthough\b|\bthen\b", re.I)

COMPLETION_VERBS = re.compile(
    r"\b(?:placed|ordered|purchased|bought|paid|checked\s+out|submitted|booked|reserved|"
    r"added|saved|sent|emailed|posted|published|changed|updated|edited|modified|set|"
    r"deleted|removed|cancell?ed|confirmed|completed|finished|created|registered|"
    r"signed\s+(?:in|up)|logged\s+in|logged\s+out|subscribed|uploaded|downloaded|applied|"
    r"clicked|selected|entered|filled|typed|navigated|opened|searched|found|verified|"
    r"replied|scheduled|transferred|accepted|approved|rejected|declined|archived|"
    r"renamed|moved|copied|shared|invited|enabled|disabled|installed|"
    r"(?:was|were|has\s+been|have\s+been|is|got)\s+(?:successfully\s+)?"
    r"(?:placed|submitted|created|saved|sent|booked|confirmed|completed|updated|deleted))\b",
    re.I,
)

_CLAUSE = re.compile(
    r"[;()\[\]]|[,:](?!\d)|\s-+\s|\b(?:and|but|while|whereas|however|although|though|then|so|"
    r"because)\b",
    re.I,
)
_JOINS = frozenset({",", "and"})
_OWN_SUBJECT = re.compile(
    r"^\W*(?:(?i:i|we|you|he|she|it|they|there|the|a|an|my|your|his|her|their|its|our|this|"
    r"that|these|those|each|every|all|some|any|no|nothing|nobody|none)\b|[A-Z0-9#$])"
)
_CODE = re.compile(r"`[^`]*`")
NEGATION = re.compile(
    r"\b(?:not|never|no(?![.\-@])|nothing|nobody|none|zero|neither|nor|without|cannot|"
    r"unable\s+to|fail(?:ed)?\s+to|far\s+from|"
    r"(?:could|did|was|were|have|has|had|is|are|ca|wo|do|does|should|would|must)n'?t)\b|"
    r"\w+n't\b",
    re.I,
)
"""Any word that can reverse what a clause says: 'nothing', 'no one', 'not all', 'n't'..."""
_INTENTION_TAIL = re.compile(
    r"\b(?:will|shall|going\s+to|plan\s+to|planning\s+to|want\s+to|need\s+to|needs\s+to|"
    r"about\s+to|try\s+to|trying\s+to|attempt\s+to|should|would|could|might|may|must|"
    r"let\s+me|i'?ll|if|unless|to\s+be)\b(?:\W+\w+){0,3}\W*$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Sentence:
    text: str
    origin: Origin


def quote_in(quote: str, source: str) -> bool:
    needle = normalize(quote).strip(" .\"'")
    return bool(needle) and needle in normalize(source)


def split_sentences(text: str) -> list[str]:
    parts = (_BULLET.sub("", part).strip() for part in _SENTENCE_BOUNDARY.split(text))
    return [part for part in parts if part]


def sentences(answer: str | None, reasoning: list[str]) -> list[Sentence]:
    out = [Sentence(s, "answer") for s in split_sentences(answer or "")]
    for step in reasoning:
        out.extend(Sentence(s, "reasoning") for s in split_sentences(step))
    return out


def clause_prefix(sentence: str, start: int) -> str:
    before = sentence[:start]
    breaks = list(_CLAUSE_BREAK.finditer(before))
    return before[breaks[-1].end() :] if breaks else before


def clause_before(sentence: str, start: int) -> str:
    """The text from the start of the clause holding `start` up to it."""
    before = sentence[:start]
    breaks = list(_CLAUSE.finditer(_CODE.sub(_blank, before)))
    return before[breaks[-1].end() :] if breaks else before


def is_intention(prefix: str) -> bool:
    return _INTENTION_TAIL.search(prefix) is not None


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group())


def clause_spans(sentence: str) -> list[tuple[int, int, tuple[str, ...]]]:
    """Each clause of `sentence` as (start, end, the separators before it).

    A clause ends at ',', ';', ':', a bracket, a dash or a conjunction ('and', 'but', 'while').
    Separators inside backticks are code, not clause breaks.
    """
    plain = _CODE.sub(_blank, sentence)
    spans: list[tuple[int, int, tuple[str, ...]]] = []
    start, separators = 0, ()
    for brk in [*_CLAUSE.finditer(plain), None]:
        end = brk.start() if brk else len(plain)
        if plain[start:end].strip():
            spans.append((start, end, separators))
            separators = ()
        if brk is not None:
            separators = (*separators, brk.group().strip().casefold())
            start = brk.end()
    return spans


def negated_clauses(
    sentence: str, modelled: Sequence[re.Pattern[str]] = ()
) -> list[tuple[int, int]]:
    """The (start, end) spans of the clauses whose meaning a negation may reverse.

    A clause is negated when it holds a negation word ('nothing was purchased', 'not all tests
    pass') that no `modelled` pattern accounts for, or when it joins a negated clause with ','
    or 'and' and has no subject of its own ('did not open, edit and save it'). Extractors claim
    nothing from a negated clause: a pattern that reads its verb could read it the wrong way
    round. Text in backticks is code, not a negation.
    """
    plain = _CODE.sub(_blank, sentence.translate(PUNCT_MAP))
    negated: list[tuple[int, int]] = []
    previous = False
    for start, end, separators in clause_spans(plain):
        clause = plain[start:end]
        joined = bool(separators) and all(s in _JOINS for s in separators)
        inherits = previous and joined and _OWN_SUBJECT.match(clause) is None
        for pattern in modelled:
            clause = pattern.sub(_blank, clause)
        previous = inherits or NEGATION.search(clause) is not None
        if previous:
            negated.append((start, end))
    return negated


def asserted(sentence: str, start: int) -> bool:
    """True when a verb at `start` is asserted as done, not negated or merely planned."""
    if any(begin <= start < end for begin, end in negated_clauses(sentence)):
        return False
    return not is_intention(clause_prefix(sentence, start).translate(PUNCT_MAP))


def is_claim_like(sentence: str) -> bool:
    return any(asserted(sentence, m.start()) for m in COMPLETION_VERBS.finditer(sentence))
