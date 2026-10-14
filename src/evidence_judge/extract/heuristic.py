"""Rule-based claim extraction: deterministic, offline, no LLM.

Recognises common completion phrases ("placed the order", "added to cart", "changed the email",
"order #1042", "the price was $24") and maps them to request / page_text / value predicates.
Negated ("could not place") and intended ("will place") phrases are skipped.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import product

from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    RequestPredicate,
    Scope,
    ValuePredicate,
)
from evidence_judge.core.verdict import Usage
from evidence_judge.extract.base import MAX_CLAIMS, MAX_PREDICATES_PER_CLAIM
from evidence_judge.extract.merge import cap_claims, dedupe_claims, renumber
from evidence_judge.extract.text import Sentence, asserted, normalize, sentences

_I = re.IGNORECASE

_TOPICS: dict[str, tuple[str, ...]] = {
    "email": ("email", "account"),
    "e-mail": ("email", "account"),
    "password": ("password", "account"),
    "address": ("address", "account"),
    "profile": ("profile", "account"),
    "username": ("profile", "account"),
    "phone": ("phone", "account"),
    "settings": ("settings", "preferences"),
    "preferences": ("preferences", "settings"),
    "message": ("message",),
    "comment": ("comment",),
    "review": ("review",),
    "post": ("post",),
    "issue": ("issue",),
    "ticket": ("ticket",),
    "event": ("event", "calendar"),
    "meeting": ("event", "calendar"),
    "appointment": ("appointment", "book"),
    "file": ("upload", "file"),
    "document": ("upload", "document"),
    "item": ("item",),
    "product": ("product", "item"),
    "task": ("task",),
    "note": ("note",),
    "subscription": ("subscription",),
    "application": ("application",),
    "form": (),
}
_TOPIC_WORDS = "|".join(re.escape(t) for t in _TOPICS)
_TOPIC_AFTER = re.compile(rf"^[^.;!?]{{0,40}}?\b({_TOPIC_WORDS})s?\b", _I)
_TOPIC_BEFORE = re.compile(rf"\b({_TOPIC_WORDS})s?\b[^.;!?]{{0,30}}$", _I)
_EMAIL_VALUE = re.compile(r"\bto\s+([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", _I)

_CURRENCY = re.escape(chr(0x24) + chr(0x20AC) + chr(0xA3) + chr(0x20B9))
_PRICE = re.compile(
    rf"(?:[{_CURRENCY}]|\b(?:USD|EUR|GBP|INR)\s?)\s?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)",
    _I,
)
_PRICE_FIELD = re.compile(
    r"\b(total|subtotal|price|priced|cost|costs|paid|charged|fee|amount)\b", _I
)
_TOTAL_WORDS = frozenset({"total", "subtotal", "paid", "charged", "amount"})
_PRICE_TASK = re.compile(rf"[{_CURRENCY}]|\b(?:price|cost|cheap|cheapest|under|budget|total)\b", _I)
_REFERENCE = re.compile(
    r"\b(order|confirmation|booking|reservation|reference|ticket|tracking|case|invoice)"
    r"(?:\s+(?:number|no\.?|id|code))?\s*(?:is|was|:)?\s*#?\s*"
    r"(?=[A-Z0-9-]{3,})([A-Z0-9][A-Z0-9-]*\d[A-Z0-9-]*)\b"
    r"|#\s?([A-Z0-9-]*\d[A-Z0-9-]*)\b",
    _I,
)
_DONE = r"(?:was|were|has\s+been|have\s+been|is|got)\s+(?:successfully\s+)?"

PredicateBuilder = Callable[[Sentence, re.Match[str], str | None], list[Predicate]]


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: re.Pattern[str]
    kind: ClaimKind
    outcome: bool
    text: str
    build: PredicateBuilder
    topic_text: str | None = None
    requires_topic: bool = False
    task_words: tuple[str, ...] = ()


def _requests(methods: Sequence[str], urls: Sequence[str], body: list[str]) -> list[Predicate]:
    if not urls:
        built = [RequestPredicate(method=m, body_contains=body) for m in methods]
    else:
        built = [
            RequestPredicate(method=m, url_contains=[u], body_contains=body)
            for m, u in product(methods, urls)
        ]
    return list[Predicate](built[:MAX_PREDICATES_PER_CLAIM])


def _topic(sentence: Sentence, match: re.Match[str]) -> str | None:
    after = _TOPIC_AFTER.search(sentence.text[match.end() :])
    if after:
        return after.group(1).lower()
    before = _TOPIC_BEFORE.search(sentence.text[: match.start()])
    return before.group(1).lower() if before else None


def _email_body(sentence: Sentence) -> list[str]:
    found = _EMAIL_VALUE.search(sentence.text)
    return [found.group(1).rstrip(".")] if found else []


def _fixed(methods: Sequence[str], urls: Sequence[str]) -> PredicateBuilder:
    return lambda _sentence, _match, _topic: _requests(methods, urls, [])


def _topical(methods: Sequence[str], fallback: Sequence[str] = ()) -> PredicateBuilder:
    def build(sentence: Sentence, _match: re.Match[str], topic: str | None) -> list[Predicate]:
        urls = _TOPICS.get(topic, ()) if topic else tuple(fallback)
        return _requests(methods, urls, _email_body(sentence))

    return build


def _cart(_sentence: Sentence, match: re.Match[str], _topic: str | None) -> list[Predicate]:
    noun = match.group(1).lower()
    return _requests(["POST"], ["cart"] if noun == "cart" else ["cart", noun], [])


def _confirmation(
    _sentence: Sentence, _match: re.Match[str], _topic: str | None
) -> list[Predicate]:
    return [PageTextPredicate(contains="confirm", scope=Scope.ANY)]


_RULES: tuple[_Rule, ...] = (
    _Rule(
        re.compile(
            r"\b(?:placed|submitted|completed|confirmed)\s+(?:the\s+|an?\s+|my\s+|your\s+)?order\b"
            r"|\b(?:ordered|purchased|bought|checked\s+out)\b"
            rf"|\border\b[^.;]{{0,30}}?\b{_DONE}(?:placed|submitted|completed)\b",
            _I,
        ),
        ClaimKind.OUTCOME,
        True,
        "The agent placed an order",
        _fixed(["POST"], ["order", "checkout"]),
    ),
    _Rule(
        re.compile(
            r"\badded\b[^.;]{0,60}?\bto\s+(?:the\s+|my\s+|your\s+)?(?:shopping\s+)?"
            r"(cart|basket|bag)\b",
            _I,
        ),
        ClaimKind.ACTION,
        False,
        "The agent added an item to the cart",
        _cart,
        task_words=("cart", "basket", "bag"),
    ),
    _Rule(
        re.compile(rf"\b(?:booked|reserved)\b|\b{_DONE}(?:booked|reserved)\b", _I),
        ClaimKind.OUTCOME,
        True,
        "The agent made a booking",
        _fixed(["POST"], ["book", "reserv"]),
    ),
    _Rule(
        re.compile(r"\b(?:logged|signed)\s+(?:in|into)\b", _I),
        ClaimKind.ACTION,
        False,
        "The agent logged in",
        _fixed(["POST"], ["login", "sign", "session"]),
        task_words=("log in", "login", "sign in", "signin"),
    ),
    _Rule(
        re.compile(r"\b(?:signed\s+up|registered|created\s+(?:an?\s+|the\s+|my\s+)?account)\b", _I),
        ClaimKind.OUTCOME,
        True,
        "The agent created an account",
        _fixed(["POST"], ["register", "signup", "account"]),
    ),
    _Rule(
        re.compile(r"\b(?:changed|updated|edited|modified)\b", _I),
        ClaimKind.OUTCOME,
        True,
        "The agent changed a record",
        _topical(["POST", "PUT", "PATCH"]),
        topic_text="The agent changed the {topic}",
        requires_topic=True,
    ),
    _Rule(
        re.compile(r"\bsaved\b", _I),
        ClaimKind.OUTCOME,
        True,
        "The agent saved changes",
        _topical(["POST", "PUT", "PATCH"]),
        topic_text="The agent saved the {topic}",
    ),
    _Rule(
        re.compile(r"\b(?:deleted|removed|cancell?ed|unsubscribed)\b", _I),
        ClaimKind.OUTCOME,
        True,
        "The agent deleted something",
        _topical(["DELETE", "POST"]),
        topic_text="The agent deleted the {topic}",
        requires_topic=True,
    ),
    _Rule(
        re.compile(r"\b(?:sent|emailed|posted|replied|published)\b", _I),
        ClaimKind.OUTCOME,
        True,
        "The agent sent a message",
        _topical(["POST"], ["send", "message"]),
        topic_text="The agent sent the {topic}",
    ),
    _Rule(
        re.compile(r"\bsubmitted\b(?!\s+(?:the\s+|an?\s+|my\s+|your\s+)?order)", _I),
        ClaimKind.OUTCOME,
        True,
        "The agent submitted a form",
        _topical(["POST"]),
        topic_text="The agent submitted the {topic}",
    ),
    _Rule(
        re.compile(
            r"\b(?:order|booking|reservation|payment|purchase|registration|appointment)\b"
            r"(?:\s+(?:number\s+)?#?\s?[A-Z0-9-]*\d[A-Z0-9-]*)?\s+"
            r"(?:(?:is|was|has\s+been)\s+)?(?:successfully\s+)?confirmed\b"
            r"|\bconfirmation\s+(?:page|message|email|screen)\b",
            _I,
        ),
        ClaimKind.STATE,
        True,
        "A confirmation was shown",
        _confirmation,
    ),
)


class HeuristicExtractor:
    """Deterministic extractor used offline, in CI, and as a fallback when the LLM fails."""

    name = "heuristic"

    def __init__(self, *, max_claims: int = MAX_CLAIMS) -> None:
        self._max_claims = max_claims

    async def extract(
        self, *, task: str | None, answer: str | None, reasoning: list[str]
    ) -> tuple[ClaimSet, Usage]:
        return self.extract_sync(task=task, answer=answer, reasoning=reasoning), Usage()

    def extract_sync(
        self, *, task: str | None, answer: str | None, reasoning: list[str]
    ) -> ClaimSet:
        task_norm = normalize(task or "")
        found: list[Claim] = []
        for sentence in sentences(answer, reasoning):
            found.extend(_rule_claims(sentence, task_norm))
            found.extend(_reference_claims(sentence))
            found.extend(_price_claims(sentence, task_norm))
        unique, _ = dedupe_claims(found, max_predicates=MAX_PREDICATES_PER_CLAIM)
        capped, truncated = cap_claims(unique, self._max_claims)
        return ClaimSet(
            claims=renumber(capped),
            extractor=self.name,
            coverage_note=f"truncated={truncated}",
        )


def _claim(
    sentence: Sentence, text: str, kind: ClaimKind, key: bool, predicates: list[Predicate]
) -> Claim:
    return Claim(
        id="pending",
        text=text,
        quote=sentence.text,
        kind=kind,
        origin=sentence.origin,
        key=key,
        predicates=predicates[:MAX_PREDICATES_PER_CLAIM],
    )


def _first_asserted(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    return next((m for m in pattern.finditer(text) if asserted(text, m.start())), None)


def _rule_claims(sentence: Sentence, task_norm: str) -> list[Claim]:
    claims: list[Claim] = []
    for rule in _RULES:
        match = _first_asserted(rule.pattern, sentence.text)
        if match is None:
            continue
        topic = _topic(sentence, match)
        if rule.requires_topic and topic is None:
            continue
        predicates = rule.build(sentence, match, topic)
        if not predicates:
            continue
        text = rule.topic_text.format(topic=topic) if rule.topic_text and topic else rule.text
        key = rule.outcome or any(word in task_norm for word in rule.task_words)
        claims.append(_claim(sentence, text, rule.kind, key, predicates))
    return claims


def _reference_claims(sentence: Sentence) -> list[Claim]:
    claims: list[Claim] = []
    for match in _REFERENCE.finditer(sentence.text):
        if not asserted(sentence.text, match.start()):
            continue
        label = (match.group(1) or "reference").lower()
        number = match.group(2) or match.group(3)
        predicates: list[Predicate] = [
            PageTextPredicate(contains=number, scope=Scope.ANY),
            RequestPredicate(response_contains=[number]),
        ]
        text = f"The {label} number {number} was shown"
        claims.append(_claim(sentence, text, ClaimKind.STATE, True, predicates))
    return claims


def _price_claims(sentence: Sentence, task_norm: str) -> list[Claim]:
    field_match = _PRICE_FIELD.search(sentence.text)
    if field_match is None:
        return []
    field = "total" if field_match.group(1).lower() in _TOTAL_WORDS else "price"
    key = _PRICE_TASK.search(task_norm) is not None
    claims: list[Claim] = []
    for match in _PRICE.finditer(sentence.text):
        if not asserted(sentence.text, match.start()):
            continue
        raw = match.group(1)
        predicates: list[Predicate] = [
            ValuePredicate(field=field, op="==", value=float(raw.replace(",", ""))),
            PageTextPredicate(contains=raw, scope=Scope.ANY),
        ]
        claims.append(_claim(sentence, f"The {field} was {raw}", ClaimKind.VALUE, key, predicates))
    return claims
