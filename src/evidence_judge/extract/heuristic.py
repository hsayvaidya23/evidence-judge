"""Rule-based claim extraction: deterministic, offline, no LLM.

Recognises common completion phrases ("placed the order", "added to cart", "changed the email",
"changed the display name to Jane Doe", "order #1042", "the price was $24") and maps them to
request / page_text / value predicates.
Intended ("will place") phrases are skipped, and so is every clause a negation word may reverse
("could not place", "nothing was purchased"): rather than guess its direction, the extractor
abstains (`text.negated_clauses`). Predicates too weak to prove a claim
(core.predicates.vacuity_reason: a request without a URL or body criterion, page text of a single
word such as "confirm") are dropped and counted in the coverage note.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import product

from pydantic import ValidationError

from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    RequestPredicate,
    Scope,
    vacuity_reason,
)
from evidence_judge.core.verdict import Usage
from evidence_judge.extract.base import MAX_CLAIMS, MAX_PREDICATES_PER_CLAIM
from evidence_judge.extract.merge import cap_claims, dedupe_claims, renumber
from evidence_judge.extract.text import (
    CURRENCY,
    Sentence,
    amount_text,
    asserted,
    names_purchase,
    normalize,
    paid_amount,
    sentences,
    stated_amounts,
    stated_value,
)

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

_PRICE_FIELD = re.compile(
    r"\b(total|subtotal|price|priced|cost|costs|paid|charged|fee|amount)\b", _I
)
_TOTAL_WORDS = frozenset({"total", "subtotal", "paid", "charged", "amount"})
_TIED_LEAD = re.compile(
    r"\b(?:total(?:l?ed|s)?|subtotal|paid|charged|spent|came\s+to|comes?\s+to|amounted\s+to)\b", _I
)
_PRICE_TASK = re.compile(rf"[{CURRENCY}]|\b(?:price|cost|cheap|cheapest|under|budget|total)\b", _I)
_REFERENCE = re.compile(
    r"\b(order|confirmation|booking|reservation|reference|ticket|tracking|case|invoice)"
    r"(?:\s+(?:number|no\.?|id|code))?\s*(?:is|was|:)?\s*#?\s*"
    r"(?=[A-Z0-9-]{3,})([A-Z0-9][A-Z0-9-]*\d[A-Z0-9-]*)\b"
    r"|#\s?([A-Z0-9-]*\d[A-Z0-9-]*)\b",
    _I,
)
_DONE = r"(?:was|were|has\s+been|have\s+been|is|got)\s+(?:successfully\s+)?"
_SETTING = re.compile(
    r"\b(?:changed|updated|set|edited|modified|renamed)\s+"
    r"(?:the\s+|my\s+|your\s+|his\s+|her\s+|their\s+|its\s+|our\s+)?"
    r"(?P<field>[A-Za-z][\w -]{0,40}?)\s+to\s+"
    r"(?:\"(?P<dq>[^\"]{1,80})\"|'(?P<sq>[^']{1,80})'|"
    r"(?P<bare>[^.;!?,\"]{1,80}?))"
    r"(?=\s*(?:[.;!?,]|$|\s(?:and|but|so|because|then|which|as|on|in|for|from|via|using"
    r"|with|after|before|instead)\b))",
    _I,
)
_VAGUE_VALUE = re.compile(
    r"^(?:the|a|an|my|your|his|her|their|its|our|this|that|these|those|it|them|one|what|"
    r"whatever|something|anything|nothing|same|new|default|be)\b",
    _I,
)
_SECRET_FIELD = re.compile(r"password|passcode|pin\b|card|cvv|cvc|ssn|secret|token|key\b", _I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_VALUE_WORD = re.compile(r"[^\W_]{2,}")
_FROM = re.compile(r"\s+from\s+")
ORDER_WORDS = r"(?:order|checkout|pay|purchase|book|reserv|appointment)"
ORDER_URL = rf"^\w+://[^/?#]*/[^?#]*{ORDER_WORDS}"

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


_CONFIRMED_ID = re.compile(r"#?\s?([A-Z0-9-]*\d[A-Z0-9-]*)", _I)
_CONFIRMED_NOUN = re.compile(
    r"\b(order|booking|reservation|payment|purchase|registration|appointment)\s+"
    r"(?:(?:is|was|has\s+been)\s+)?(?:successfully\s+)?confirmed\b",
    _I,
)


def _confirmation(sentence: Sentence, match: re.Match[str], _topic: str | None) -> list[Predicate]:
    """The confirmed id when the sentence names one, else multi-word confirmation phrases.

    A bare word like "confirm" also labels the checkout page's Confirm button, so it proves
    nothing about the outcome. "The confirmation page said 'Appointment confirmed'" names what
    was confirmed, so its phrases are about the appointment.
    """
    phrase = match.group(0)
    reference = _CONFIRMED_ID.search(phrase)
    if reference is not None and len(reference.group(1)) >= 3:
        return _page_texts([reference.group(1)])
    noun = phrase.split()[0].lower()
    if noun == "confirmation" and (named := _CONFIRMED_NOUN.search(sentence.text)) is not None:
        noun = named.group(1).lower()
    if noun == "confirmation":
        return _page_texts(["thank you for your", "has been confirmed", "is confirmed"])
    return _page_texts([f"{noun} confirmed", f"{noun} is confirmed", f"thank you for your {noun}"])


def _page_texts(needles: Sequence[str]) -> list[Predicate]:
    built: list[Predicate] = []
    for needle in needles:
        try:
            built.append(PageTextPredicate(contains=needle, scope=Scope.ANY))
        except ValidationError:
            continue
    return built


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
        _fixed(["POST"], ["book", "reserv", "appointment"]),
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


@dataclass(slots=True)
class _Tally:
    vacuous: int = 0
    claims: list[Claim] = field(default_factory=list[Claim])


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
        tally = _Tally()
        told = sentences(answer, reasoning)
        bought = any(names_purchase(s.text) for s in told)
        priced = answer_priced(told)
        for sentence in told:
            _rule_claims(sentence, task_norm, tally)
            _setting_claims(sentence, tally)
            _reference_claims(sentence, tally)
            _price_claims(sentence, task_norm, bought, priced, tally)
        unique, _ = dedupe_claims(tally.claims, max_predicates=MAX_PREDICATES_PER_CLAIM)
        capped, truncated = cap_claims(unique, self._max_claims)
        return ClaimSet(
            claims=renumber(capped),
            extractor=self.name,
            coverage_note=f"truncated={truncated}; vacuous_predicates={tally.vacuous}",
        )


def _claim(
    sentence: Sentence,
    text: str,
    kind: ClaimKind,
    key: bool,
    predicates: list[Predicate],
    tally: _Tally,
) -> None:
    """Record a claim, keeping only predicates specific enough to prove it."""
    specific = [p for p in predicates if vacuity_reason(p) is None]
    tally.vacuous += len(predicates) - len(specific)
    tally.claims.append(
        Claim(
            id="pending",
            text=text,
            quote=sentence.text,
            kind=kind,
            origin=sentence.origin,
            key=key,
            predicates=specific[:MAX_PREDICATES_PER_CLAIM],
        )
    )


def _first_asserted(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    return next((m for m in pattern.finditer(text) if asserted(text, m.start())), None)


def _rule_claims(sentence: Sentence, task_norm: str, tally: _Tally) -> None:
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
        _claim(sentence, text, rule.kind, key, predicates, tally)


def _setting_value(match: re.Match[str]) -> str | None:
    """The new value a 'changed the X to Y' claim names, or None when it is too vague to check
    (a pronoun, 'the new one'), a secret ingest redacts, an email (the email rule checks it),
    or has no word of three or more letters ('2', 'on')."""
    field_name = match.group("field").strip()
    quoted_value = match.group("dq") or match.group("sq")
    value = (quoted_value or match.group("bare") or "").strip()
    if not value or _SECRET_FIELD.search(field_name) or _EMAIL.search(value):
        return None
    if quoted_value is None and (_VAGUE_VALUE.match(value) or len(value.split()) > 6):
        return None
    if not any(len(w) >= 3 and not w.isdigit() for w in _VALUE_WORD.findall(value)):
        return None
    return value


def _setting_claims(sentence: Sentence, tally: _Tally) -> None:
    """'changed my display name to Jane Doe' is proven by the mutation that sent the new value.

    Its text on the page proves nothing: the agent typed it, so the page shows it as an echo
    whether or not the change was saved. The claim's predicates are a POST, PUT or PATCH whose
    body contains every word of the value, so a JSON body (`"Jane Doe"`) and a form-encoded one
    (`Jane+Doe`) both match.
    """
    match = _first_asserted(_SETTING, sentence.text)
    if match is None or (value := _setting_value(match)) is None:
        return
    words = list(dict.fromkeys(_VALUE_WORD.findall(value)))
    field_name = _FROM.split(" ".join(match.group("field").split()).lower())[0]
    predicates = _requests(["POST", "PUT", "PATCH"], [], words)
    text = f"The agent set the {field_name} to {value}"
    _claim(sentence, text, ClaimKind.OUTCOME, True, predicates, tally)


def _reference_claims(sentence: Sentence, tally: _Tally) -> None:
    for match in _REFERENCE.finditer(sentence.text):
        if not asserted(sentence.text, match.start()):
            continue
        label = (match.group(1) or "reference").lower()
        number = match.group(2) or match.group(3)
        predicates = _page_texts([number])
        if len(number) >= 2:
            predicates += [
                RequestPredicate(method=method, response_contains=[number])
                for method in ("POST", "GET")
            ]
        text = f"The {label} number {number} was shown"
        _claim(sentence, text, ClaimKind.STATE, True, predicates, tally)


def _price_claims(
    sentence: Sentence, task_norm: str, bought: bool, priced: bool, tally: _Tally
) -> None:
    """'The price was $24', 'total $1,249.50', a value a sentence states ('It was $4.00', 'the
    balance is $1,284.50') and, in a sentence naming no price field, what a purchase cost:
    'bought it for $24.00', 'ordered at $24.00', 'the kettle ($24.00)'.

    In a run whose answer or reasoning asserts a purchase (`bought`), every such amount is what
    the purchase cost, wherever it is told ('Total: $4.00' in a reasoning step and 'Bought it.'
    as the answer), so only what the site answered the order with proves it (`order_results`):
    any page of the run could list the same amount for another item. Otherwise a value stated
    without a price word is read from the answer only. A price a reasoning step read off a
    listing (`listed_amount`) once the answer states the price paid (`priced`) is page text.
    What a purchase cost is part of the purchase, so it is key whatever the task says.
    """
    field_match = _PRICE_FIELD.search(sentence.text)
    name = "total" if field_match and field_match.group(1).lower() in _TOTAL_WORDS else "price"
    key = _PRICE_TASK.search(task_norm) is not None
    purchase = bought or names_purchase(sentence.text)
    for match, paid in priced_amounts(sentence, purchase=purchase):
        amount = match.group(0).strip()
        ordered = purchase and not listed_amount(sentence, match, priced)
        _claim(
            sentence,
            f"The {name if paid else 'amount'} was {amount_text(match)}",
            ClaimKind.VALUE,
            key or ordered,
            order_results([amount]) if ordered else _page_texts([amount]),
            tally,
        )


def answer_priced(told: list[Sentence]) -> bool:
    """Whether the answer states what a purchase cost, so the order is checked against it."""
    return any(s.origin == "answer" and priced_amounts(s, purchase=True) for s in told)


def listed_amount(sentence: Sentence, match: re.Match[str], priced: bool) -> bool:
    """Whether `match` is a price a reasoning step read off a listing, not what was paid: the
    answer states the price paid (`priced`), and the step names no purchase and ties no total
    or payment word before the amount ('The Blue Kettle costs $19.00 and the Red Kettle $24.00; I
    will buy the red one.'). 'Total: $29.00' or 'paid $29.00' in a step is what was paid."""
    if not priced or sentence.origin != "reasoning" or names_purchase(sentence.text):
        return False
    if paid_amount(sentence.text, match):
        return False
    return _TIED_LEAD.search(sentence.text, 0, match.start()) is None


def priced_amounts(sentence: Sentence, *, purchase: bool) -> list[tuple[re.Match[str], bool]]:
    """The amounts `sentence` states as a price, each with whether a price word or a purchase
    names it (`paid`): a value stated without one ('It was $4.00') counts in the answer, or
    anywhere in a run that asserts a `purchase`."""
    field = _PRICE_FIELD.search(sentence.text) is not None
    stated = purchase or sentence.origin == "answer"
    out: list[tuple[re.Match[str], bool]] = []
    for match in stated_amounts(sentence.text):
        paid = field or paid_amount(sentence.text, match)
        if paid or (stated and stated_value(sentence.text, match)):
            out.append((match, paid))
    return out


def order_results(amounts: list[str]) -> list[Predicate]:
    """A POST to an order, checkout, payment or booking URL (the requests the purchase and
    booking claims point at) that the site answered with `amounts`: in its response (as text,
    or as a number under a price key of a JSON answer) or on the page it led to before the
    agent acted again (checks.request.site_result)."""
    return [RequestPredicate(method="POST", url_regex=ORDER_URL, result_contains=amounts)]
