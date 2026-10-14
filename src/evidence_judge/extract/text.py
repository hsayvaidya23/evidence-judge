"""Deterministic text helpers shared by extractors: normalisation, sentences, completion verbs."""

from __future__ import annotations

import re
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

_NEGATION_TAIL = re.compile(
    r"\b(?:not|never|no(?!\.)|n't|cannot|unable\s+to|failed\s+to|fail\s+to|without|"
    r"neither|nor|couldn'?t|didn'?t|wasn'?t|weren'?t|haven'?t|hasn'?t|isn'?t|can'?t|"
    r"won'?t|don'?t|doesn'?t)\b"
    r"(?:\W+\w+){0,3}\W*$",
    re.I,
)
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


def is_negated(prefix: str) -> bool:
    return _NEGATION_TAIL.search(prefix) is not None


def is_intention(prefix: str) -> bool:
    return _INTENTION_TAIL.search(prefix) is not None


def asserted(sentence: str, start: int) -> bool:
    """True when a verb at `start` is asserted as done, not negated or merely planned."""
    prefix = clause_prefix(sentence, start).translate(PUNCT_MAP)
    return not is_negated(prefix) and not is_intention(prefix)


def is_claim_like(sentence: str) -> bool:
    return any(asserted(sentence, m.start()) for m in COMPLETION_VERBS.finditer(sentence))
