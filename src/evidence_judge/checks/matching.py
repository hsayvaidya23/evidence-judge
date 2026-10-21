"""Normalisation and comparison helpers shared by the deterministic checkers."""

from __future__ import annotations

import functools
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal, cast
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

import regex

from evidence_judge.checks.errors import InvalidPredicateError, RegexTimeoutError
from evidence_judge.core.predicates import (
    STATUS_ANY,
    STATUS_NONE,
    STATUS_OK,
    compile_pattern,
    status_spec,
)
from evidence_judge.core.site import Site
from evidence_judge.textnorm import find_phrase, normalize

Op = Literal["==", "!=", "<", "<=", ">", ">=", "contains"]


class _Missing(Enum):
    MISSING = "missing"


MISSING: Final = _Missing.MISSING

_NUMBER = re.compile(
    r"""
    ^(?:(?:[a-z]{3}|rs\.?)\s*)?
    (?P<sign>[+-])?\s*
    [$€£¥₹]?\s*
    (?P<sign2>[+-])?\s*
    (?P<core>\d(?:[\d.,' ]*\d)?)
    \s*(?P<suffix>[kmb])?
    \s*[$€£¥₹]?
    \s*(?:[a-z]{3}|zł)?$
    """,
    re.VERBOSE,
)
_GROUP_SEPARATORS: Final = ",. '"
_DECIMAL_SEPARATORS: Final = ".,"
_SUFFIX_SCALE: Final = {"k": 1e3, "m": 1e6, "b": 1e9}
_CURRENCIES: Final = {
    "$": "usd",
    "us$": "usd",
    "usd": "usd",
    "€": "eur",
    "eur": "eur",
    "£": "gbp",
    "gbp": "gbp",
    "₹": "inr",
    "rs": "inr",
    "inr": "inr",
}
_AMOUNT = r"\d{1,3}(?:[.,' ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
MONEY: Final = re.compile(
    rf"(?:(?<![a-z0-9$])(?P<pre>us\$|usd|eur|gbp|inr|rs\.?|[$€£₹])\s?"
    rf"(?P<pre_amount>{_AMOUNT})"
    rf"|(?<![\w.,])(?P<post_amount>{_AMOUNT})\s?(?P<post>[$€£₹]|(?:usd|eur|gbp|inr)(?![a-z])))"
    r"(?![\d]|[.,]\d)"
)
"""An amount of money in normalised text: a currency symbol or code before or after it."""
_SPACE_GROUPED = re.compile(r"^\d{1,3} \d{3}$")
_TOTAL_KEYS: Final = frozenset(
    {
        "total",
        "grand_total",
        "amount_total",
        "total_amount",
        "total_price",
        "order_total",
        "total_paid",
        "amount_paid",
        "paid",
        "paid_amount",
        "charged",
        "amount_charged",
        "charged_amount",
        "current_total_price",
    }
)
"""Keys that name the total an order was charged, as joined lowercase words; a closed list, so
no part of the price (`shipping_total`, `total_tax`, `total_discounts`) reads as the total."""
_ORDER_KEYS: Final = frozenset(
    {
        "order",
        "orders",
        "checkout",
        "checkout_session",
        "payment",
        "charge",
        "invoice",
        "receipt",
        "data",
        "result",
        "response",
        "purchase",
        "transaction",
        "booking",
        "reservation",
    }
)
"""Keys whose object is the order or a wrapper of it: a total counts only at the root or under
these, so the total of a `delivery`, `vat`, `item`, `cart`, `quote` or `gift_card` never does."""
_PAID_OBJECTS: Final[dict[str, str | None]] = {
    "checkout.session": None,
    "charge": None,
    "payment_intent": "succeeded",
    "invoice": "paid",
}
"""Stripe `object` types whose amount is a payment, with the `status` they must have; an object
of any other type (`refund`, `invoice_item`, `coupon`, `payout`) states no price paid."""
_FAILED_STATES: Final = frozenset(
    {
        "failure",
        "void",
        "voided",
        "refunded",
        "errored",
        "unpaid",
        "not_paid",
        "pending_payment",
        "payment_pending",
        "awaiting_payment",
        "abandoned",
        "draft",
        "incomplete",
        "requires_payment_method",
        "requires_action",
        "requires_confirmation",
    }
)
"""Status values that name a payment that did not go through, besides any `failure_mark`."""
_PATH_WORDS: Final = frozenset(
    {
        "order",
        "orders",
        "checkout",
        "payment",
        "payments",
        "charge",
        "charges",
        "purchase",
        "transaction",
        "transactions",
        "booking",
        "reservation",
        "receipt",
        "outcome",
        "error",
        "failure",
        "decline",
    }
)
"""Words of a key whose object lies on the order's path, where any failure field counts."""
_ORDER_WORDS: Final = frozenset({"order", "orders", "purchase", "booking", "reservation"})
"""Words of a key whose object is the order itself: an `invoice` inside it is a document about
the order, not its payment."""
_ATTEMPT_WORDS: Final = frozenset(
    {"attempt", "attempts", "retries", "tries", "payments", "charges", "transactions"}
)
"""Words of a key whose list holds attempts in order: only the last one says how it ended."""
_ASIDE_WORDS: Final = frozenset(
    {
        "history",
        "previous",
        "prior",
        "past",
        "item",
        "items",
        "line",
        "lines",
        "notification",
        "notifications",
        "event",
        "events",
        "log",
        "logs",
    }
)
"""Words of a key whose value is about other orders, line items or side effects, never this
order's payment."""
_STATUS_WORDS: Final = frozenset({"status", "state", "result", "outcome"})
_SUBJECT_WORDS: Final = frozenset(
    {
        "is",
        "has",
        "was",
        "fully",
        "order",
        "payment",
        "charge",
        "transaction",
        "checkout",
        "purchase",
        "financial",
        "invoice",
    }
)
"""Words a status or flag key may start with and still be about the order's payment
(`payment_status`, `is_paid`, `orderDeclined`); any other (`email_status`, `coupon_expired`)
names something else."""
_SUCCESS_FLAGS: Final = frozenset({"ok", "success", "succeeded", "successful", "paid"})
_FAILURE_FLAGS: Final = frozenset(
    {"declined", "failed", "refunded", "voided", "canceled", "cancelled", "rejected", "denied"}
)
_FAILURE_FIELDS: Final = frozenset({"failure", "failed", "decline", "declined"})
_OUTCOME_WORDS: Final = frozenset({"outcome", "error", "failure", "decline"})
_FAILED_OUTCOMES: Final = frozenset({"issuer_declined", "blocked", "invalid"})
_MESSAGE_WORDS: Final = frozenset({"message", "msg"})
_DECLINE_PHRASE = re.compile(
    r"\b(?:(?:was|were|been|is|are)\s+(?:declined|refused|rejected|denied|cancell?ed)"
    r"|(?:payment|charge|transaction|card|order|purchase|checkout)s?\s+(?:has\s+|have\s+)?"
    r"(?:failed|declined|refused|rejected|denied|unsuccessful"
    r"|not\s+(?:be\s+)?(?:processed|completed|placed|paid))"
    r"|insufficient\s+funds|could\s?n[o']t\s+be\s+(?:processed|completed|charged)"
    r"|unable\s+to\s+(?:process|charge|complete))\b",
    re.IGNORECASE,
)
_LETTER_BEFORE = r"(?<![^\W\d_])"
_LETTER_AFTER = r"(?![^\W\d_])"
_FAILURE_WORD = re.compile(
    _LETTER_BEFORE + r"(?:errors?|err|fail(?:s|ed|ures?)?|declin(?:e|es|ed)|refus(?:al|ed|e|ée?s?)?"
    r"|reject(?:s|ed|ion)?|cancel(?:l?ed|lation)|invalid|denied|unauthori[sz]ed|expired"
    r"|insufficient|try[\s_-]*again|problems?|unable|unsuccessful|timed?[\s_-]*out"
    r"|abgelehnt|fehl(?:er|geschlagen)?|rechaz(?:ad[oa]s?|o)|rifiut(?:at[oaie]|o)"
    r"|[ée]checs?|[ée]chou[ée]e?s?"
    r"|not[\s_-]+(?:be[\s_-]+)?(?:processed|completed|placed))" + _LETTER_AFTER,
    re.IGNORECASE,
)
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
UNSET: Final = frozenset({"", "0", "false", "no", "none", "null", "off"})
"""Values that leave a field unset (`?error=`, `"errors": "none"`)."""
_CURRENCY_WORDS: Final = frozenset({"currency", "code", "iso", "symbol"})
_PAYPAL_AMOUNT: Final = frozenset({"value", "currency_code", "breakdown"})
_PLAIN_DECIMAL = re.compile(r"\d+(?:\.\d{1,2})?")
_MINOR_UNIT_KEYS: Final = frozenset({"cents", "minor", "pence", "paise"})
_STRIPE_CURRENCY = re.compile(r"[a-z]{3}")
_STRIPE_AMOUNT_KEYS: Final = frozenset(
    {"amount", "amount_total", "amount_subtotal", "amount_received", "amount_captured"}
)
_KEY_CASE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_WORD = re.compile(r"[a-z]+")
_INDEX_BRACKETS = re.compile(r"\[(-?\d+)\]")
_TRUNCATION_MARK = re.compile(r"…\[truncated[^\]]{0,60}\]\s*\Z")
_TRUNCATION_TAIL: Final = 80


def is_truncated(body: str | None) -> bool:
    """True if ingest cut this body short and appended its truncation marker."""
    return body is not None and _TRUNCATION_MARK.search(body[-_TRUNCATION_TAIL:]) is not None


def contains(haystack: str | None, needle: str) -> bool:
    """Whether `needle` occurs in `haystack` once both are normalised (`find_text`)."""
    if haystack is None:
        return False
    return find_text(normalize(haystack), normalize(needle)) >= 0


def find_text(text: str, phrase: str) -> int:
    """Where normalised `phrase` first occurs in normalised `text`, or -1.

    A phrase that is an amount of money ('$24', 'EUR 1.249,50', '24 usd') occurs wherever the
    text states the same amount in the same currency, however it writes it: '$24' is in
    'USD 24.00', '$ 24', 'us$24' and '24 $', and 'EUR 1.249,50' in '€1,249.50'. The currency
    must match ('$24' is not in '€24'), and an amount parse_number cannot read unambiguously
    ('$1.500') is matched as text. Any other phrase is matched by textnorm.find_phrase.
    """
    wanted = _money(phrase)
    if wanted is None:
        return find_phrase(text, phrase)
    for found in MONEY.finditer(text):
        if _money(found.group()) == wanted:
            return found.start()
    return -1


def _money(text: str) -> tuple[str, float] | None:
    """The currency and amount `text`, all of it, states, or None."""
    match = MONEY.fullmatch(text)
    if match is None:
        return None
    symbol = match["pre"] or match["post"] or ""
    written = match["pre_amount"] or match["post_amount"] or ""
    if _SPACE_GROUPED.match(written):
        return None
    amount = parse_number(written)
    if amount is None:
        return None
    return _CURRENCIES[symbol.rstrip(". ")], round(amount, 2)


def shows_amount(body: str | None, phrase: str) -> bool:
    """Whether JSON `body` holds the amount of money `phrase` states as the price paid: a number,
    or a string that is only a plain decimal number ('24.00'), under a key that names the
    order's total and nothing else (`total`, `grand_total`, `totalPrice`, `amount_paid`,
    `amount_total`), at the root or under keys that name the order or a wrapper of it
    (`order`, `checkout`, `payment`, `invoice`, `data`, `result`), outside any array (a line
    item). A total under any other object (`shipping`, `delivery`, `vat`, `item`, `cart`,
    `refund`, `gift_card`) never shows it. Minor units count only when the key says so
    (`total_cents`: 2950 is 29.50), or in a Stripe-shaped object, one that names its currency
    in lowercase and has an `object` type or a Stripe amount key (`{"amount": 2950,
    "currency": "usd"}`), whose `object` type, if any, is a payment that went through, or in a `total_money` as Square writes it; a PayPal `amount` is
    `{"value": "29.50", "currency_code": "USD"}`; a bare `amount` shows nothing anywhere else.
    The document must state one total and report no failure on the order's path (json_failure), and every
    currency it names, as a code, a currency object or a formatted amount ('29,50 €'), must be
    the phrase's."""
    wanted = _money(normalize(phrase))
    if wanted is None or body is None:
        return False
    try:
        tree = json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        return False
    currency, amount = wanted
    if json_failure(tree) is not None or any(c != currency for c in _json_currencies(tree)):
        return False
    return _json_totals(tree) == {amount}


def shows_other_amount(texts: list[str], body: str | None, phrase: str) -> bool:
    """Whether the site's answer states one price and it is not the amount `phrase` states: the
    rendered `texts` (as formatted amounts) and JSON `body` (as an order total, shows_amount)
    name exactly one amount of money, in `phrase`'s currency, and another one ('$24.00' on a
    confirmation refutes a claimed '$19.99'). No amount, an amount it cannot read, two amounts
    or another currency refute nothing."""
    wanted = _money(normalize(phrase))
    if wanted is None:
        return False
    stated = {_money(m.group(0)) for t in texts for m in MONEY.finditer(normalize(t))}
    try:
        tree = json.loads(body) if body else None
    except (json.JSONDecodeError, RecursionError):
        tree = None
    if tree is not None and json_failure(tree) is None:
        named = _json_currencies(tree) or {wanted[0]}
        currency = next(iter(named)) if len(named) == 1 else None
        stated |= {(currency, total) if currency else None for total in _json_totals(tree)}
    return (
        len(stated) == 1
        and (only := stated.pop()) is not None
        and only[0] == wanted[0]
        and only[1] != wanted[1]
    )


def _json_nodes(tree: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    stack: list[object] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(cast(list[object], node))
        elif isinstance(node, dict):
            fields = cast(dict[str, object], node)
            found.append(fields)
            stack.extend(fields.values())
    return found


def failure_mark(value: str) -> str | None:
    """The failure word in a status-like value or URL part ('failed', 'DECLINED',
    'payment_refused', 'paymentDeclined', 'abgelehnt'), or None. Words are whole words."""
    found = _FAILURE_WORD.search(_CAMEL.sub(" ", value))
    return found.group() if found else None


def reported(value: object) -> bool:
    """Whether an `errors` or `error` field holds something: not null, empty or unset."""
    if isinstance(value, str):
        return value.strip().casefold() not in UNSET
    if isinstance(value, bool):
        return value
    if isinstance(value, list | dict):
        return bool(cast(list[object] | dict[object, object], value))
    return value is not None


def json_failure(tree: object) -> tuple[str, object] | None:
    """The first field of a JSON tree that reports a payment or order that did not go through, as
    its key and value, or None. Every field on the order's path (the root and objects under keys
    that name the order, its payment or an outcome: `order`, `checkout`, `payment`,
    `payment_intent`, `charge`, `transaction`, `data`, `result`, `outcome`, ...) counts
    (field_failure); a Stripe `charge` there must also have been captured: `"captured": false`
    reports one. Off that path (`customer`, `coupon`, `fulfillment`, `address_validation`, an
    `invoice` inside an order) only an error with content or a message saying a payment was
    declined counts. History, line items, notifications, events and logs (`order_history`,
    `items`, `line_items`, `notifications`) report nothing, and of a list of attempts
    (`payment_attempts`, `transactions`, `charges`) only the last one counts."""
    queue: list[tuple[object, list[str], bool, bool]] = [(tree, [], True, False)]
    for node, parent, path, ordered in queue:
        if isinstance(node, list):
            items = cast(list[object], node)
            if _ATTEMPT_WORDS & set(parent):
                items = items[-1:]
            queue.extend((v, parent, path, ordered) for v in items)
            continue
        if not isinstance(node, dict):
            continue
        fields = cast(dict[str, object], node)
        if path and fields.get("object") == "charge" and fields.get("captured") is False:
            return "captured", False
        for key, value in fields.items():
            if field_failure(key, value, parent) if path else _aside_failure(key, value):
                return key, value
        for key, value in fields.items():
            words = _key_words(key)
            if _ASIDE_WORDS.isdisjoint(words):
                inside = ordered or not _ORDER_WORDS.isdisjoint(words)
                queue.append((value, words, path and _on_path(words, ordered), inside))
    return None


def _on_path(words: list[str], ordered: bool) -> bool:
    joined = "_".join(words)
    if joined == "invoice":
        return not ordered
    return joined in _ORDER_KEYS or not _PATH_WORDS.isdisjoint(words)


def _aside_failure(key: str, value: object) -> bool:
    last = (_key_words(key) or [""])[-1]
    if last in ("error", "errors"):
        return reported(value)
    return last in _MESSAGE_WORDS and isinstance(value, str) and bool(_DECLINE_PHRASE.search(value))


def field_failure(key: str, value: object, parent: list[str] | None = None) -> bool:
    """Whether one JSON field reports a failure, by the words of its key:

    - an `error` or `errors` (`checkoutUserErrors`) that holds something (reported);
    - a status-like field (`status`, `state`, `result`, `outcome`, `payment_status`,
      `financialStatus`) whose value names a failure (failure_mark, or 'unpaid', 'not_paid',
      'pending_payment', 'abandoned', 'draft', 'refunded', 'voided', ...);
    - a success flag (`ok`, `success`, `paid`, `is_paid`, `isSuccess`, `payment_success`) that
      is false, or a failure flag (`declined`, `failed`, `refunded`, `payment_failed`) that is
      true;
    - a `failure_code`, `decline_code` or `failure_message` that is set, or the `type` of an
      `outcome` or error that names a decline (`issuer_declined`, `card_error`);
    - a `message` (`error_message`, `declineMessage`) that says a payment was declined.

    A status or flag key with other leading words (`email_status`, `coupon_expired`) speaks of
    something else, and a count (`failed_attempts: 0`) is no flag."""
    words = _key_words(key)
    if not words:
        return False
    last, subject = words[-1], set(words[:-1]) <= _SUBJECT_WORDS
    if last in ("error", "errors"):
        return reported(value)
    flag = _flag(value)
    if flag is not None:
        return subject and (
            (flag is False and last in _SUCCESS_FLAGS) or (flag and last in _FAILURE_FLAGS)
        )
    if not isinstance(value, str):
        return False
    if last in _STATUS_WORDS and subject:
        return _failed_state(value)
    if _FAILURE_FIELDS & set(words):
        return value.strip().casefold() not in UNSET
    if last == "type" and _OUTCOME_WORDS & {*words, *(parent or [])}:
        return _failed_state(value) or _state(value) in _FAILED_OUTCOMES
    return last in _MESSAGE_WORDS and _DECLINE_PHRASE.search(value) is not None


def _flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and (folded := value.strip().casefold()) in ("true", "false"):
        return folded == "true"
    return None


def _state(value: str) -> str:
    return re.sub(r"[^a-z]+", "_", value.casefold()).strip("_")


def _failed_state(value: str) -> bool:
    return failure_mark(value) is not None or _state(value) in _FAILED_STATES


def _json_currencies(tree: object) -> set[str]:
    """Every currency a JSON tree names, anywhere: under a currency key (`currency`,
    `currency_code`, `currencyIso`, `currency_symbol`) as a string or as an object's `code`, and
    in any string that is a formatted amount ('29,50 €')."""
    found: set[str] = set()
    for fields in _json_nodes(tree):
        for key, value in fields.items():
            if isinstance(value, dict):
                if _is_currency_key(key):
                    inner = cast(dict[str, object], value).values()
                    found.update(c for c in (_currency_of(v) for v in inner) if c)
            elif (code := _currency_code(key, value)) is not None:
                found.add(code)
            elif isinstance(value, str) and (formatted := _money(normalize(value))) is not None:
                found.add(formatted[0])
    return found


def _is_currency_key(key: str) -> bool:
    words = _key_words(key)
    return "currency" in words and words[-1] in _CURRENCY_WORDS


def _currency_code(key: str, value: object) -> str | None:
    return _currency_of(value) if _is_currency_key(key) else None


def _currency_of(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = normalize(value).rstrip(". ")
    return _CURRENCIES.get(code, code) or None


def _json_totals(tree: object) -> set[float]:
    """Every amount a JSON tree states as an order's total, at its root or under keys that name
    the order (`_ORDER_KEYS`), outside arrays and outside Stripe objects that are no payment."""
    found: set[float] = set()
    stack: list[tuple[object, bool]] = [(tree, False)]
    while stack:
        node, money = stack.pop()
        if not isinstance(node, dict):
            continue
        fields = cast(dict[str, object], node)
        if not _paid_object(fields):
            continue
        minor = money or _stripe_minor(fields)
        priced = _has_currency(fields)
        for key, value in fields.items():
            words = _key_words(key)
            amount = _total_amount(words, value, minor, priced)
            if amount is not None:
                found.add(amount)
            elif isinstance(value, dict) and not money:
                child = cast(dict[str, object], value)
                joined = "_".join(words)
                if joined in _ORDER_KEYS:
                    stack.append((child, False))
                elif words[-1:] == ["money"] and "_".join(words[:-1]) in _TOTAL_KEYS:
                    stack.append((child, True))
                elif (joined in _TOTAL_KEYS or words == ["amount"]) and (
                    paypal := _paypal_amount(child)
                ) is not None:
                    found.add(paypal)
    return found


def _paid_object(fields: dict[str, object]) -> bool:
    kind = fields.get("object")
    if not isinstance(kind, str):
        return True
    if kind not in _PAID_OBJECTS:
        return False
    status = _PAID_OBJECTS[kind]
    return status is None or fields.get("status") == status


def _paypal_amount(fields: dict[str, object]) -> float | None:
    if not {"value", "currency_code"} <= fields.keys() <= _PAYPAL_AMOUNT:
        return None
    return _total_amount(["total"], fields["value"], False, True)


def _has_currency(fields: dict[str, object]) -> bool:
    return any(_currency_code(k, v) for k, v in fields.items())


def _stripe_minor(fields: dict[str, object]) -> bool:
    """Whether this object is Stripe-shaped and so states amounts in minor units: it names its
    currency in lowercase, and it has an `object` type or a Stripe amount key (`amount`,
    `amount_total`, ...). A lowercase code alone is no sign: Medusa writes
    `{"total": 2400, "currency_code": "usd"}` for $2,400."""
    stripe = isinstance(fields.get("object"), str) or not _STRIPE_AMOUNT_KEYS.isdisjoint(fields)
    return stripe and any(
        isinstance(v, str) and _STRIPE_CURRENCY.fullmatch(v) and _currency_code(k, v)
        for k, v in fields.items()
    )


def _total_amount(words: list[str], value: object, minor: bool, priced: bool) -> float | None:
    """The amount a JSON number, or plain decimal string, under the key `words` states as the
    order's total, or None."""
    if isinstance(value, str):
        value = float(value) if _PLAIN_DECIMAL.fullmatch(value) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    if len(words) > 1 and words[-1] in _MINOR_UNIT_KEYS:
        stem = words[:-2] if words[-2] == "in" else words[:-1]
        named = "_".join(stem) in _TOTAL_KEYS or (stem == ["amount"] and priced)
        return _from_minor(value) if named else None
    if words == ["amount"]:
        return _from_minor(value) if priced and minor else None
    if "_".join(words) not in _TOTAL_KEYS:
        return None
    return _from_minor(value) if minor else round(float(value), 2)


def _from_minor(value: float) -> float | None:
    return round(value / 100, 2) if float(value).is_integer() else None


def _key_words(key: str) -> list[str]:
    return _KEY_WORD.findall(_KEY_CASE.sub("_", key).casefold())


def excerpt(text: str, needle: str, width: int = 80) -> str:
    """A short window of normalised `text` around the first occurrence of `needle`."""
    norm, want = normalize(text), normalize(needle)
    at = find_text(norm, want)
    if at < 0:
        return norm[:width]
    pad = max(0, (width - len(want)) // 2)
    start, end = max(0, at - pad), min(len(norm), at + len(want) + pad)
    return f"{'…' if start else ''}{norm[start:end]}{'…' if end < len(norm) else ''}"


def parse_number(value: object) -> float | None:
    """Parse a number as written in prices and totals, or None if it is not one (or ambiguous).

    Accepts currency symbols and codes ('$24.00', 'USD 30', '1500 EUR', 'Rs. 1500'), a sign,
    a k/m/b suffix ('1.2k'), and thousands grouped with commas, dots, spaces (including thin
    spaces) or apostrophes ('24,000.50', '1 500', "1'500", '1.500,00 €'). A comma followed by
    one or two digits is a decimal comma ('24,5'). Ambiguous forms such as '1.500' (1.5 or
    1500) and malformed grouping ('1,50,000') are rejected.

    Superscript or subscript marks right after the last digit are a footnote and dropped
    ('$9¹' is 9); anywhere else, or any other character that only folds into a digit ('20 m²',
    '½', '①'), makes the text not a number, so it never reads as more digits ('2²' is not 22).
    Text is never read in scientific notation ('1e3'), as NaN or infinity, or as an
    accounting negative ('(24.00)'): those are not numbers. A JSON number is a number whatever
    its notation.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = _without_footnote(value)
    if text is None:
        return None
    match = _NUMBER.match(normalize(text))
    if match is None or (match["sign"] and match["sign2"]):
        return None
    number = _parse_digits(match["core"])
    if number is None:
        return None
    if match["suffix"]:
        number *= _SUFFIX_SCALE[match["suffix"]]
    return -number if "-" in (match["sign"], match["sign2"]) else number


def _is_mark(char: str) -> bool:
    return unicodedata.decomposition(char).startswith(("<super>", "<sub>"))


def _without_footnote(text: str) -> str | None:
    """`text` without superscript or subscript marks that follow its last digit, or None if any
    character other than a decimal digit would fold into one under NFKC."""
    if text.isascii():
        return text
    end = len(text.rstrip())
    start = end
    while start > 0 and _is_mark(text[start - 1]):
        start -= 1
    if 0 < start < end and text[start - 1].isdecimal():
        text = text[:start]
    if any(not c.isdecimal() and _folds_to_digit(c) for c in text):
        return None
    return text


def _folds_to_digit(char: str) -> bool:
    return any(d.isdigit() for d in unicodedata.normalize("NFKC", char))


def _grouped(digits: str, separator: str) -> str | None:
    """'1,234,567' -> '1234567' when every group after the first has exactly three digits."""
    head, *groups = digits.split(separator)
    if not (1 <= len(head) <= 3 and head.isdigit() and head[0] != "0"):
        return None
    if not groups or any(len(g) != 3 or not g.isdigit() for g in groups):
        return None
    return head + "".join(groups)


def _parse_digits(core: str) -> float | None:
    separators = {c for c in core if not c.isdigit()}
    if not separators:
        return float(core)
    position = max(core.rfind(c) for c in separators)
    last = core[position]
    head, tail = core[:position], core[position + 1 :]
    if len(separators) == 1:
        if core.count(last) > 1 or last not in _DECIMAL_SEPARATORS:
            grouped = _grouped(core, last)
            return None if grouped is None else float(grouped)
        if len(tail) == 3:
            if last == ",":
                grouped = _grouped(core, last)
                return None if grouped is None else float(grouped)
            return float(core) if head == "0" or len(head) > 3 else None
        if last == "," and len(tail) > 2:
            return None
        return float(f"{head}.{tail}")
    if last not in _DECIMAL_SEPARATORS or len(separators) != 2:
        return None
    (group,) = separators - {last}
    grouped = _grouped(head, group)
    return None if grouped is None else float(f"{grouped}.{tail}")


def as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def compare(observed: object, op: Op, expected: object) -> bool | None:
    """Compare numerically when both sides parse as numbers.

    `==` and `!=` fall back to normalised strings; `contains` is a normalised substring test.
    The ordering operators never compare strings: if either side is not a number the result is
    None (undecidable), because '1 500' < '30' as text says nothing about the amounts.
    """
    if op == "contains":
        return normalize(as_text(expected)) in normalize(as_text(observed))
    left, right = parse_number(observed), parse_number(expected)
    if left is not None and right is not None:
        return _ordered(left, op, right)
    if op not in ("==", "!="):
        return None
    return _ordered(normalize(as_text(observed)), op, normalize(as_text(expected)))


def _ordered[T: (float, str)](left: T, op: Op, right: T) -> bool:
    equal = (
        math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
        if isinstance(left, float) and isinstance(right, float)
        else left == right
    )
    match op:
        case "==":
            return equal
        case "!=":
            return not equal
        case "<":
            return left < right and not equal
        case "<=":
            return left < right or equal
        case ">":
            return left > right and not equal
        case ">=":
            return left > right or equal
        case "contains":
            raise InvalidPredicateError("'contains' is not an ordering operator")


def validate_status_spec(spec: str | None) -> str | None:
    """The canonical status spec, or InvalidPredicateError for a malformed one."""
    try:
        return status_spec(spec)
    except ValueError as exc:
        raise InvalidPredicateError(str(exc)) from exc


def status_matches(status: int | None, spec: str | None, *, failed: bool = False) -> bool:
    """True if a request that got `status` (None: no response) satisfies `spec`.

    'ok' is a 2xx or 3xx response to a request that did not fail; 'none' is no response; 'any'
    is anything; None (null) is any response; otherwise an exact code ('201') or class ('2xx').
    """
    value = validate_status_spec(spec)
    if value == STATUS_ANY:
        return True
    if value == STATUS_NONE:
        return status is None
    if status is None:
        return False
    if value is None:
        return True
    if value == STATUS_OK:
        return 200 <= status < 400 and not failed
    if value.endswith("xx"):
        return status // 100 == int(value[0])
    return status == int(value)


def _segments(path: str) -> list[str]:
    normalized = _INDEX_BRACKETS.sub(r".\1", path.strip()).removeprefix("$").strip(".")
    return normalized.split(".") if normalized else []


def _step(current: object, segment: str) -> object:
    if isinstance(current, dict):
        mapping = cast(dict[str, object], current)
        return mapping.get(segment, MISSING)
    if isinstance(current, list):
        items = cast(list[object], current)
        try:
            return items[int(segment)]
        except (ValueError, IndexError):
            return MISSING
    return MISSING


class _Repeated(list[object]):
    """Every value of a key that one JSON object or form body names more than once, in order."""


def _merge_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    merged: dict[str, object] = {}
    for key, value in pairs:
        if key not in merged:
            merged[key] = value
            continue
        prior = merged[key]
        merged[key] = _Repeated([*prior, value] if isinstance(prior, _Repeated) else [prior, value])
    return merged


def _parse(body: str | None, *, form: bool, repeated: bool) -> object:
    if body is None or not body.strip():
        return MISSING
    try:
        return json.loads(body, object_pairs_hook=_merge_pairs if repeated else None)
    except (json.JSONDecodeError, RecursionError):
        pass
    if not form or "=" not in body:
        return MISSING
    pairs = parse_qsl(body.strip(), keep_blank_values=True)
    if not pairs:
        return MISSING
    return _merge_pairs(list[tuple[str, object]](pairs)) if repeated else dict(pairs)


def parse_body(body: str | None) -> object:
    """Parse a JSON or form-encoded body into Python data, or return MISSING.

    A repeated key keeps its last value; body_values reads every one.
    """
    return _parse(body, form=True, repeated=False)


def _step_all(current: object, segment: str) -> list[object]:
    value = _step(current, segment)
    if value is MISSING:
        return []
    return list(value) if isinstance(value, _Repeated) else [value]


def body_values(body: str | None, path: str, *, form: bool = True) -> list[object]:
    """Every value `path` names in a JSON body, or with `form` also in a form-encoded one.

    Where parse_body keeps the last value of a repeated key, this reads them all: 'a=1&a=2' and
    '{"a": 1, "a": 2}' both name two values of 'a', and servers differ on which one they use. A
    body that does not parse, or lacks the path, names none.
    """
    tree = _parse(body, form=form, repeated=True)
    if tree is MISSING:
        return []
    values = [tree]
    for segment in _segments(path):
        values = [found for current in values for found in _step_all(current, segment)]
    return values


def url_contains(url: str, needle: str) -> bool:
    return needle.casefold() in unquote(url).casefold()


def url_location_contains(url: str, needle: str) -> bool:
    """Substring of the scheme, host and path; the query and fragment are ignored."""
    return needle.casefold() in _location(url)


@functools.lru_cache(maxsize=4096)
def _location(url: str) -> str:
    parts = urlsplit(url)
    location = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else parts.path
    return unquote(location).casefold()


def url_path_contains(url: str, needle: str) -> bool:
    return url_contains(urlsplit(url).path, needle)


def url_path_is(url: str, path: str) -> bool:
    """Whether the decoded path of `url` is `path`, ignoring case and a trailing slash."""
    return _bare_path(urlsplit(url).path) == _bare_path(path)


def _bare_path(path: str) -> str:
    return unquote(path).casefold().rstrip("/") or "/"


def url_query_contains(url: str, needle: str) -> bool:
    return needle.casefold() in unquote_plus(urlsplit(url).query).casefold()


def url_path(url: str) -> str:
    """Path plus query of a URL, for compact human-readable reasons ('/api/orders?id=1')."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def url_host(url: str) -> str | None:
    """The site of a URL as a reason names it: its host, and its port when not the default."""
    site = Site.of_url(url)
    return None if site is None else str(site)


MAX_REGEX_MATCHES: Final = 50
MAX_REGEX_INPUT: Final = 10_000
REGEX_TIMEOUT_SECONDS: Final = 0.05


def compile_regex(pattern: str) -> regex.Pattern[str]:
    """Compile an untrusted (spec or LLM) regex with the `regex` engine, case-insensitively.

    Patterns over MAX_REGEX_LENGTH characters are rejected. Backtracking is bounded at match
    time instead (regex_search), so any valid pattern is accepted.
    """
    try:
        return compile_pattern(pattern)
    except ValueError as exc:
        raise InvalidPredicateError(str(exc)) from exc


def regex_search(compiled: regex.Pattern[str], text: str) -> regex.Match[str] | None:
    """Search at most the first and last MAX_REGEX_INPUT characters of `text`.

    Each search may run for REGEX_TIMEOUT_SECONDS; past that it raises RegexTimeoutError, which
    checkers turn into NO_EVIDENCE.
    """
    try:
        if len(text) <= 2 * MAX_REGEX_INPUT:
            return compiled.search(text, timeout=REGEX_TIMEOUT_SECONDS)
        return compiled.search(
            text, 0, MAX_REGEX_INPUT, timeout=REGEX_TIMEOUT_SECONDS
        ) or compiled.search(text, len(text) - MAX_REGEX_INPUT, timeout=REGEX_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        budget = round(REGEX_TIMEOUT_SECONDS * 1000)
        raise RegexTimeoutError(f"regex timed out after {budget} ms: /{compiled.pattern}/") from exc


@dataclass(frozen=True, slots=True)
class RegexMatches:
    """The matches regex_findall read, and whether they are every match in the text."""

    matches: list[regex.Match[str]]
    complete: bool


def regex_findall(compiled: regex.Pattern[str], text: str) -> RegexMatches:
    """Up to MAX_REGEX_MATCHES matches in the same windows and time budget as regex_search.

    The result is incomplete when the text had more matches than that, or was longer than the
    two windows, so its middle was never searched.
    """
    windowed = len(text) > 2 * MAX_REGEX_INPUT
    windows = (
        [(0, MAX_REGEX_INPUT), (len(text) - MAX_REGEX_INPUT, len(text))]
        if windowed
        else [(0, len(text))]
    )
    found: list[regex.Match[str]] = []
    try:
        for start, end in windows:
            for match in compiled.finditer(text, start, end, timeout=REGEX_TIMEOUT_SECONDS):
                if len(found) >= MAX_REGEX_MATCHES:
                    return RegexMatches(found, complete=False)
                found.append(match)
    except TimeoutError as exc:
        budget = round(REGEX_TIMEOUT_SECONDS * 1000)
        raise RegexTimeoutError(f"regex timed out after {budget} ms: /{compiled.pattern}/") from exc
    return RegexMatches(found, complete=not windowed)
