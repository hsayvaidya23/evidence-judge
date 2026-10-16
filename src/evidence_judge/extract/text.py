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
_DO_SUPPORT = re.compile(
    r"\b(?:(?:do|does|did|can|could|will|would|should|must)\s+not|cannot|unable\s+to|"
    r"fail(?:ed)?\s+to|(?:do|does|did|ca|could|wo|would|should|must)n'?t)\b",
    re.I,
)
"""A negated auxiliary: the verbs it reverses are bare ('did not open, edit and save it')."""
_NEGATED_NOUN = re.compile(r"^\W*(?:no|nothing|nobody|none|zero|neither)\b", re.I)
_PASSIVE = re.compile(r"\b(?:was|were|is|are|been|being|be|got|gets)\b", re.I)
_FINITE_PAST = re.compile(
    r"^\W*(?:\w*[^\We]ed|bought|paid|got|made|went|saw|found|sent|left|gave|took|came|"
    r"brought|chose|kept|knew|lost|met|ran|said|sold|spent|told|thought|won|wrote)\b",
    re.I,
)
"""A verb only a finite past can be: '-ed' or a common irregular past unlike its bare form."""
_NEGATED_PARTICIPLE = re.compile(r"^\W*not\s+(?!\w*thing\b)\w+ing\b", re.I)
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
    pass') that no `modelled` pattern accounts for, or when it continues a negated verb with ','
    or 'and' and has no subject of its own: 'never logged in, added it to the cart or placed an
    order', 'did not open, edit and save it'. A clause after a negated noun ('no problems,
    placed the order') or a finite verb after a negated auxiliary or participle ('didn't see
    any errors and bought it', 'Not wanting to overpay, compared prices') says something of its
    own. Extractors claim nothing from a negated clause: a
    pattern that reads its verb could read it the wrong way round. Text in backticks is code,
    not a negation.
    """
    plain = _CODE.sub(_blank, sentence.translate(PUNCT_MAP))
    negated: list[tuple[int, int]] = []
    carries: Literal["any", "bare"] | None = None
    for start, end, separators in clause_spans(plain):
        clause = plain[start:end]
        joined = bool(separators) and all(s in _JOINS for s in separators)
        inherits = (
            carries is not None
            and joined
            and _OWN_SUBJECT.match(clause) is None
            and (carries == "any" or _FINITE_PAST.match(clause) is None)
        )
        for pattern in modelled:
            clause = pattern.sub(_blank, clause)
        if inherits:
            negated.append((start, end))
        elif NEGATION.search(clause) is not None:
            negated.append((start, end))
            carries = _carried(clause)
        else:
            carries = None
    return negated


def _carried(clause: str) -> Literal["any", "bare"] | None:
    """What a negated clause passes on to the subjectless clauses joined after it: nothing
    after a negated noun ('no problems'), unless a passive verb continues ('nothing was
    changed, deleted'); bare verbs after a negated auxiliary ('did not open') or a negated
    participle, which modifies the main clause without negating it ('Not wanting to overpay,
    compared prices'); any verb after 'never' or 'not' ('never logged in')."""
    if _NEGATED_NOUN.match(clause):
        return "any" if _PASSIVE.search(clause) else None
    if _DO_SUPPORT.search(clause) or _NEGATED_PARTICIPLE.match(clause):
        return "bare"
    return "any"


def asserted(sentence: str, start: int) -> bool:
    """True when a verb at `start` is asserted as done, not negated or merely planned."""
    if any(begin <= start < end for begin, end in negated_clauses(sentence)):
        return False
    return not is_intention(clause_prefix(sentence, start).translate(PUNCT_MAP))


CURRENCY = re.escape(chr(0x24) + chr(0x20AC) + chr(0xA3) + chr(0x20B9))
_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:\.\d+|,\d{1,2})?"
PRICE = re.compile(
    rf"(?:[{CURRENCY}]|\b(?:USD|EUR|GBP|INR)\s?)\s?({_AMOUNT})(?!\d)"
    rf"|(?<![\w.,])({_AMOUNT})\s?(?:[{CURRENCY}](?!\s?\d)|(?:USD|EUR|GBP|INR)\b)",
    re.I,
)
"""An amount with a currency symbol or code before it ('$24.00', 'EUR 19', '€1.249,50') or
after it ('24 USD', '1.249,50 €'); `amount_text` is its number."""


def amount_text(match: re.Match[str]) -> str:
    """The number a PRICE match states, as written ('1.249,50')."""
    return match.group(1) or match.group(2)


PRICE_BOUND = re.compile(
    r"\b(?:limit|budget|under|below|max(?:imum)?|up\s+to|at\s+most|less\s+than|over|above|"
    r"shipping|delivery|postage|tax(?:es)?|vat|fees?|tip)\b(?:\W+\w+){0,2}\W*$",
    re.I,
)
"""'under $30', '(shipping $0.00)', 'limit $30.00': a bound or a surcharge, not the price."""
_DIFFERENCE = re.compile(
    r"\W{0,3}(?:under|below|over|above|less|more|off|cheaper|short|left|remaining|saved|back|"
    r"budget|limit)\b",
    re.I,
)
"""'$6 under the budget', '$5 off', '$3 left': a difference or a bound, not a price."""
_PAID_LEAD = re.compile(r"\b(?:for|at|paid|spent|costing)\s+(?:only\s+|just\s+)?$", re.I)
_PURCHASE = re.compile(
    r"\b(?:bought|purchased|ordered|orders?|paid|spent|booked|reserved|rented|checked\s+out|"
    r"placed|got)\b",
    re.I,
)
_COPULA = re.compile(
    r"\b(?:is|was|were|are|be|been|came\s+to|comes?\s+to|totall?(?:ed|s)|amounted\s+to)\s+"
    r"(?:only\s+|just\s+|exactly\s+)?$",
    re.I,
)
_EXTRAS = re.compile(
    r"\W{0,3}(?:including|incl\.?|plus|with|after|before|excluding|excl\.?)\s+(?:the\s+)?"
    r"(?:tax(?:es)?|vat|shipping|delivery|postage|fees?|tip)\b",
    re.I,
)


def stated_amounts(sentence: str) -> list[re.Match[str]]:
    """The currency amounts `sentence` states as what something cost: not in a negated or
    planned clause, not a bound or surcharge ('under $30', 'shipping $5.00', but not the $4.00
    of 'under $30 for $4.00') and not a difference ('$6 under the budget', '$5 off')."""
    return [
        m
        for m in PRICE.finditer(sentence)
        if asserted(sentence, m.start())
        and not _bounded(clause_before(sentence, m.start()))
        and _DIFFERENCE.match(sentence, m.end()) is None
    ]


def _bounded(before: str) -> bool:
    """Whether a bound or surcharge word leads up to the amount after `before`, with no other
    amount in between ('under $30 for $4.00' bounds $30, not $4.00)."""
    bound = PRICE_BOUND.search(before)
    return bound is not None and PRICE.search(before, bound.start()) is None


def paid_amount(sentence: str, amount: re.Match[str]) -> bool:
    """Whether `amount` is what a purchase cost though no price word names it: 'bought it for
    $24.00', 'ordered at $24.00', or an amount in brackets of its own ('the kettle ($24.00)').
    One with extras added ('for $26.40 including tax') is not the price."""
    if _EXTRAS.match(sentence, amount.end()):
        return False
    before, after = sentence[: amount.start()].rstrip(), sentence[amount.end() :].lstrip()
    if before.endswith("(") and after.startswith(")"):
        return True
    return _PAID_LEAD.search(before + " ") is not None and _PURCHASE.search(sentence) is not None


def names_purchase(sentence: str) -> bool:
    """Whether `sentence` asserts a purchase ('bought', 'ordered', 'paid', 'order #1001'), so an
    amount it states is what that purchase cost."""
    return any(asserted(sentence, m.start()) for m in _PURCHASE.finditer(sentence))


def stated_value(sentence: str, amount: re.Match[str]) -> bool:
    """Whether `amount` completes a copula or a total ('It was $4.00', 'the balance is
    $1,284.50', 'it came to $26.40'): a value the sentence states, whatever names it."""
    return _COPULA.search(sentence[: amount.start()]) is not None


def is_claim_like(sentence: str) -> bool:
    """Whether `sentence` asserts a completed action, or states a value ('It was $4.00')."""
    if any(asserted(sentence, m.start()) for m in COMPLETION_VERBS.finditer(sentence)):
        return True
    return any(stated_value(sentence, m) for m in stated_amounts(sentence))
