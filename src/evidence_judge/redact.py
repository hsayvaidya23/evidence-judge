"""Secret and PII redaction for recorded evidence and agent output.

Every sensitive value is replaced by ``[REDACTED:<category>:<fingerprint>]`` where the fingerprint
is the first 8 lowercase hex characters of an HMAC-SHA256 of the value's canonical form (emails
case-folded, card numbers digits only). Equal values therefore produce equal tokens, so a claim
that quotes a redacted value still matches the redacted evidence, while the value itself never
leaves the process. Redacting already-redacted data is a no-op.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import quote, unquote, unquote_plus, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from evidence_judge.config import Settings
from evidence_judge.core.evidence import (
    ActionEvent,
    ConsoleEvent,
    NetworkEvent,
    PageSnapshot,
    PageState,
    Run,
)

_PROCESS_KEY = secrets.token_bytes(32)

TOKEN_RE = re.compile(r"\[REDACTED:[a-z_]+:[0-9a-f]{8}\]")

_AUTH_HEADERS = frozenset({"authorization", "proxy-authorization"})
_COOKIE_HEADERS = frozenset({"cookie", "set-cookie", "set-cookie2"})
_SECRET_HEADERS = frozenset(
    {
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-access-token",
        "x-refresh-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-amz-security-token",
        "x-goog-api-key",
        "x-session-id",
        "x-session-token",
        "ocp-apim-subscription-key",
    }
)
_SECRET_HEADER_PARTS = ("token", "secret", "api-key", "apikey", "password", "session", "auth")

_SECRET_PARAMS = frozenset(
    {
        "token",
        "password",
        "passwd",
        "pwd",
        "secret",
        "apikey",
        "api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "sessionid",
        "session_id",
        "session_token",
        "otp",
        "jsessionid",
        "phpsessid",
        "aspsessionid",
        "x_amz_signature",
        "x_amz_credential",
        "x_amz_security_token",
        "x_goog_signature",
        "x_goog_credential",
    }
)
_SECRET_PARAM_SUFFIXES = (
    "_token",
    "token",
    "_secret",
    "_password",
    "password",
    "_apikey",
    "_api_key",
    "_access_key",
    "_secret_key",
    "_private_key",
)
_AMBIGUOUS_URL_PARAMS = frozenset(
    {"key", "code", "session", "sig", "signature", "auth", "pass", "sid"}
)
_SECRET_INPUT_HINTS = ("password", "passwd", "pwd", "secret", "token", "otp", "cvv", "cvc", "pin")
_SECRET_FIELD_WORDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passcode",
        "passphrase",
        "secret",
        "token",
        "pass",
        "pw",
        "otp",
        "totp",
        "mfa",
        "pin",
        "cvv",
        "cvv2",
        "cvc",
        "cvc2",
        "csc",
        "cvn",
        "2fa",
        "ssn",
        "csrf",
        "xsrf",
        "apikey",
    }
)
_SECRET_FIELD_SUFFIXES = ("password", "token", "secret")
_SECRET_FIELD_PHRASES = (
    "securitycode",
    "verificationcode",
    "onetimecode",
    "onetimepassword",
    "socialsecurity",
    "twofactor",
    "recoverycode",
    "backupcode",
)
"""Secret names spread over several words (``security_code``, ``oneTimeCode``)."""
_PHONE_FIELD_WORDS = frozenset(
    {"phone", "mobile", "tel", "telephone", "cell", "cellphone", "msisdn", "fax"}
)
_PHONE_VALUE_RE = re.compile(r"\+?[0-9 ().\-]{7,24}")
_SSN_RE = re.compile(r"(?<![0-9-])([0-9]{3})-([0-9]{2})-([0-9]{4})(?![0-9-])")
_SSN_CONTEXT_RE = re.compile(r"(?<![a-z])ssn(?![a-z])|social security")
_SSN_CONTEXT_CHARS = 40
_SECRET_AUTOCOMPLETE = frozenset({"current-password", "new-password", "one-time-code", "cc-csc"})
_INPUT_TAG_RE = re.compile(
    r"<input\b(?:[^<>\"']|\"[^\"]{0,4096}\"|'[^']{0,4096}'){0,4096}>", re.IGNORECASE
)
_ATTR_RE = re.compile(r"""([^\s"'<>/=]+)(?:(\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""")
_FIELD_WORD_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9\-._~+/]{8,}=*)")
_BASIC_RE = re.compile(r"(?i)\b(basic\s+)([A-Za-z0-9+/]{4,}={0,2})(?![A-Za-z0-9+/=])")
_AUTH_SCHEMES = r"(?:basic|bearer|digest|token|negotiate|ntlm)"
_AUTH_TEXT_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_-])((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:{_AUTH_SCHEMES}\s+)?)"
    rf"(?!{_AUTH_SCHEMES}\b)([^\s\"'<>,;]+)"
)
_URL_IN_TEXT_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>\\`]+")
_ESCAPED_URL_RE = re.compile(r"(?i)\b(?:https?|wss?):\\/\\/(?:[^\s\"'<>\\`]|\\/)+")
_URL_TRAILING = ".,;:!?)]}"
_TEXT_PAIR_RE = re.compile(
    r"(?<![A-Za-z0-9_\-.\[\]])(([A-Za-z][A-Za-z0-9_\-.\[\]]{0,63})[\"']?\s*[:=]\s*([\"']?))"
    r"([^\s\"'<>,;&]{1,256})(?=\3)"
)
"""``password: hunter2``, ``password = 'hunter2'`` and similar pairs written in free text."""
_PAIR_EXCLUDED_NAMES = frozenset({"pass", "pw", "ssn"})
"""Names too common in prose ("Boarding pass: ...") or handled by their own rule (SSN)."""
_API_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|xox[bpoase]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_\-]{35}"
    r"|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|glpat-[A-Za-z0-9_\-]{20,}"
    r")(?![A-Za-z0-9_-])"
)
_JSON_SECRET_RE = re.compile(r'("([A-Za-z0-9_\-.\[\]]{1,64})"\s*:\s*")((?:[^"\\]|\\.)+)(")')
_JSON_SECRET_NUMBER_RE = re.compile(
    r'("([A-Za-z0-9_\-.\[\]]{1,64})"\s*:\s*)(-?[0-9]{1,64}(?:\.[0-9]{1,64})?)(?![0-9.eE])'
)
_KV_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_\-.\[\]])([A-Za-z0-9_\-.\[\]]{1,64})(=)([^&\s\"'<>#;,]+)"
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![A-Za-z0-9-])"
)
_CARD_RE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
_CARD_PREFIX_RE = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[0478]|6)")
_CARD_FORMATTED_RE = re.compile(
    r"[0-9]{4}([ -])[0-9]{4}\1[0-9]{4}\1[0-9]{4}(?:\1[0-9]{1,3})?|[0-9]{4}([ -])[0-9]{6}\2[0-9]{4,5}"
)
_FIELD_BEFORE_RE = re.compile(r"""([A-Za-z_][A-Za-z0-9_.\-]{0,63})["']?\s*[:=]\s*["']?$""")
_CARD_FIELD_WORDS = frozenset(
    {"cc", "ccnum", "ccnumber", "ccno", "pan", "cardnumber", "cardnum", "creditcard", "debitcard"}
)
_ID_FIELD_WORDS = frozenset(
    {
        "id",
        "order",
        "tracking",
        "ref",
        "reference",
        "invoice",
        "sku",
        "shipment",
        "transaction",
        "txn",
        "confirmation",
        "booking",
    }
)
_CARD_CONTEXT_RE = re.compile(
    r"card|payment|visa|master ?card|amex|american express|credit|debit|(?<![a-z])(?:cc|pan)(?![a-z])"
)
_CARD_CONTEXT_CHARS = 30
_FIELD_LOOKBACK_CHARS = 80
_PCT_RUN_RE = re.compile(r"[A-Za-z0-9._~!$'()*,;:@+%-]+")
_FORM_RE = re.compile(r"[^\s=&]+=[^\s&]*(?:&[^\s=&]*(?:=[^\s&]*)?)*")
_PCT_SAFE = "[]:"

_MODEL_SKIP_KEYS = frozenset(
    {
        "id",
        "kind",
        "op",
        "scope",
        "status",
        "method",
        "url_regex",
        "regex",
        "page_regex",
        "json_path",
    }
)


class RedactionReport(BaseModel):
    """How many values were redacted, per category. Never contains the values themselves."""

    model_config = ConfigDict(extra="forbid")

    counts: dict[str, int] = Field(default_factory=dict[str, int])

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


_FINGERPRINT_NORMALISERS: dict[str, Callable[[str], str]] = {
    "email": lambda v: v.strip().casefold(),
    "card": _digits,
    "phone": _digits,
}
"""Canonical form per category, so values that evidence matching treats as equal (matching is
case-insensitive; card numbers are compared without separators) get equal tokens. Credentials
stay case-sensitive: two tokens differing only in case are different secrets."""


def _field_words(name: str) -> list[str]:
    return [w.lower() for w in _FIELD_WORD_RE.findall(name)]


def _card_by_field(name: str) -> bool | None:
    """True for a card-number field name, False for an order/tracking/reference id, else None."""
    words = _field_words(name)
    if any(w.startswith("card") or w in _CARD_FIELD_WORDS for w in words):
        return True
    if any(w in _ID_FIELD_WORDS for w in words):
        return False
    return None


def _looks_like_card(text: str, start: int, end: int, digits: str) -> bool:
    """Whether a Luhn-valid number at ``text[start:end]`` is a payment card, not an order id.

    A number written in card groups (``4111 1111 1111 1111``) is a card. A bare run of digits is
    a card only when its field name says so (``card_number``, ``pan``, ``cc``) or card words
    (card, payment, visa, cc, ...) appear within a few characters of it; a field named like an
    order, tracking or reference id never makes it one.
    """
    if len(digits) >= 15 and _CARD_FORMATTED_RE.fullmatch(text, start, end):
        return True
    field = _FIELD_BEFORE_RE.search(text, max(0, start - _FIELD_LOOKBACK_CHARS), start)
    if field is not None:
        verdict = _card_by_field(field.group(1))
        if verdict is not None:
            return verdict
    before = text[max(0, start - _CARD_CONTEXT_CHARS) : start]
    after = text[end : end + _CARD_CONTEXT_CHARS]
    return _CARD_CONTEXT_RE.search(f"{before} {after}".lower()) is not None


def _norm_param(name: str) -> str:
    return name.lower().replace("-", "_").strip("[]")


def _is_secret_param(name: str, *, in_url: bool) -> bool:
    n = _norm_param(name)
    if n in _SECRET_PARAMS or n.endswith(_SECRET_PARAM_SUFFIXES) or _is_secret_field_name(name):
        return True
    return in_url and n in _AMBIGUOUS_URL_PARAMS


def _is_secret_field_name(name: str) -> bool:
    words = _field_words(name)
    if any(w in _SECRET_FIELD_WORDS or w.endswith(_SECRET_FIELD_SUFFIXES) for w in words):
        return True
    joined = "".join(words)
    return any(phrase in joined for phrase in _SECRET_FIELD_PHRASES)


def _is_phone(name: str, value: str) -> bool:
    """A phone number in a field named for one (``phone``, ``mobileNumber``, ``tel``)."""
    words = _field_words(name)
    if not any(w in _PHONE_FIELD_WORDS or w.endswith("phone") for w in words):
        return False
    return _PHONE_VALUE_RE.fullmatch(value) is not None and 7 <= len(_digits(value)) <= 15


def _ssn_valid(m: re.Match[str]) -> bool:
    area, group, serial = m.group(1, 2, 3)
    return area not in {"000", "666"} and area[0] != "9" and group != "00" and serial != "0000"


def _ssn_in_context(text: str, start: int) -> bool:
    field = _FIELD_BEFORE_RE.search(text, max(0, start - _FIELD_LOOKBACK_CHARS), start)
    if field is not None and _is_secret_field_name(field.group(1)):
        return True
    before = text[max(0, start - _SSN_CONTEXT_CHARS) : start].lower()
    return _SSN_CONTEXT_RE.search(before) is not None


def _looks_like_secret(value: str) -> bool:
    """A free-text value that could be a credential: has a digit or symbol, or is long.

    Keeps prose such as "Password: required" or "Authorization: pending" intact.
    """
    return len(value) >= 20 or any(not ch.isalpha() for ch in value)


def _is_basic_credential(value: str) -> bool:
    """Whether ``value`` is base64 of ``user:password`` (HTTP Basic credentials)."""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    return ":" in decoded and decoded.isprintable()


def _is_secret_input(attrs: Mapping[str, str]) -> bool:
    """Whether an ``<input>`` holds a credential: by type, autocomplete hint, name or id."""
    if attrs.get("type", "").lower() == "password":
        return True
    autocomplete = set(attrs.get("autocomplete", "").lower().split())
    if autocomplete & _SECRET_AUTOCOMPLETE:
        return True
    return any(_is_secret_field_name(attrs.get(a, "")) for a in ("name", "id"))


def _is_secret_header(name: str) -> bool:
    n = name.lower()
    return n in _SECRET_HEADERS or any(p in n for p in _SECRET_HEADER_PARTS)


class Redactor:
    """Produces redacted copies of runs, text and models.

    A redactor remembers the card numbers it has identified, so a bare repeat of one (an answer
    saying "charged 4111111111111111" after the request sent it as ``card_number``) is redacted
    too. Redact a run before the specs and claims that go with it. Inputs are never modified.
    """

    def __init__(self, key: bytes) -> None:
        if not key:
            raise ValueError("redaction key must not be empty")
        self._key = key
        self._known_cards: set[str] = set()

    @classmethod
    def from_settings(cls, settings: Settings) -> Redactor:
        if settings.redaction_key is not None:
            return cls(settings.redaction_key.get_secret_value().encode("utf-8"))
        return cls(_PROCESS_KEY)

    def fingerprint(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:8]

    def _token(self, category: str, value: str, counts: Counter[str]) -> str:
        if TOKEN_RE.fullmatch(value):
            return value
        counts[category] += 1
        normalise = _FINGERPRINT_NORMALISERS.get(category)
        canonical = normalise(value) if normalise else value
        return f"[REDACTED:{category}:{self.fingerprint(canonical)}]"

    def redact_text(self, text: str) -> tuple[str, RedactionReport]:
        counts: Counter[str] = Counter()
        out = self._text(text, counts)
        return out, RedactionReport(counts=dict(counts))

    def redact_url(self, url: str) -> tuple[str, RedactionReport]:
        counts: Counter[str] = Counter()
        out = self._url(url, counts)
        return out, RedactionReport(counts=dict(counts))

    def redact_headers(self, headers: Mapping[str, str]) -> tuple[dict[str, str], RedactionReport]:
        counts: Counter[str] = Counter()
        out = self._headers(headers, counts)
        return out, RedactionReport(counts=dict(counts))

    def redact_run(self, run: Run) -> tuple[Run, RedactionReport]:
        for text in _run_texts(run):
            self._learn_cards(text)
        counts: Counter[str] = Counter()
        t = self._opt_text_fn(counts)
        redacted = run.model_copy(
            update={
                "task": t(run.task),
                "agent_answer": t(run.agent_answer),
                "agent_reasoning": [self._text(r, counts) for r in run.agent_reasoning],
                "actions": [self._action(a, counts) for a in run.actions],
                "network": [self._network(n, counts) for n in run.network],
                "snapshots": [self._snapshot(s, counts) for s in run.snapshots],
                "console": [self._console(c, counts) for c in run.console],
                "initial_state": self._state(run.initial_state, counts),
                "final_state": self._state(run.final_state, counts),
                "metadata": self._walk(run.metadata, counts, skip_keys=frozenset()),
            }
        )
        return redacted, RedactionReport(counts=dict(counts))

    def redact_model[M: BaseModel](self, model: M) -> tuple[M, RedactionReport]:
        """Redact every free-text string in a claim set, task spec or similar model.

        Structural fields (ids, kinds, operators, methods, regexes, JSON paths) are left intact so
        the model still validates and its predicates still evaluate against redacted evidence.
        """
        counts: Counter[str] = Counter()
        data = self._walk(model.model_dump(mode="json"), counts, skip_keys=_MODEL_SKIP_KEYS)
        return type(model).model_validate(data), RedactionReport(counts=dict(counts))

    def _opt_text_fn(self, counts: Counter[str]) -> Callable[[str | None], str | None]:
        def fn(value: str | None) -> str | None:
            return None if value is None else self._text(value, counts)

        return fn

    def _walk(self, value: Any, counts: Counter[str], *, skip_keys: frozenset[str]) -> Any:
        if isinstance(value, str):
            return self._text(value, counts)
        if isinstance(value, dict):
            items: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
            return {
                k: v if k in skip_keys else self._walk(v, counts, skip_keys=skip_keys)
                for k, v in items.items()
            }
        if isinstance(value, list | tuple):
            seq: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
            return [self._walk(v, counts, skip_keys=skip_keys) for v in seq]
        return value

    def _outside_tokens(self, text: str, fn: Callable[[str], str]) -> str:
        parts: list[str] = []
        pos = 0
        for m in TOKEN_RE.finditer(text):
            parts.append(fn(text[pos : m.start()]))
            parts.append(m.group(0))
            pos = m.end()
        parts.append(fn(text[pos:]))
        return "".join(parts)

    def _card_digits(self, m: re.Match[str]) -> str | None:
        digits = re.sub(r"[ -]", "", m.group(0))
        if not (13 <= len(digits) <= 19 and _CARD_PREFIX_RE.match(digits) and _luhn_ok(digits)):
            return None
        if digits in self._known_cards or _looks_like_card(m.string, m.start(), m.end(), digits):
            self._known_cards.add(digits)
            return digits
        return None

    def _learn_cards(self, text: str) -> None:
        for m in _CARD_RE.finditer(text):
            self._card_digits(m)

    def _percent_runs(self, text: str, counts: Counter[str], *, form: bool) -> str:
        """Redact PII hidden by percent-encoding (``jane%40example.com``) and re-encode it."""
        decode = unquote_plus if form else unquote

        def repl(m: re.Match[str]) -> str:
            raw = m.group(0)
            if "%" not in raw:
                return raw
            decoded = decode(raw)
            if decoded == raw:
                return raw
            redacted = self._text(decoded, counts)
            return raw if redacted == decoded else quote(redacted, safe=_PCT_SAFE)

        return _PCT_RUN_RE.sub(repl, text)

    def _text(self, text: str, counts: Counter[str]) -> str:
        if not text:
            return text
        form = "=" in text and _FORM_RE.fullmatch(text) is not None

        def html_input(m: re.Match[str]) -> str:
            return self._input_tag(m.group(0), counts)

        def jwt(m: re.Match[str]) -> str:
            return self._token("jwt", m.group(0), counts)

        def bearer(m: re.Match[str]) -> str:
            return m.group(1) + self._token("bearer", m.group(2), counts)

        def api_key(m: re.Match[str]) -> str:
            return self._token("api_key", m.group(0), counts)

        def json_secret(m: re.Match[str]) -> str:
            category = self._field_category(m.group(2), m.group(3), in_url=False)
            if category is None:
                return m.group(0)
            return m.group(1) + self._token(category, m.group(3), counts) + m.group(4)

        def json_number(m: re.Match[str]) -> str:
            category = self._field_category(m.group(2), m.group(3), in_url=False)
            if category is None:
                return m.group(0)
            return f'{m.group(1)}"{self._token(category, m.group(3), counts)}"'

        def kv_secret(m: re.Match[str]) -> str:
            value = unquote_plus(m.group(3))
            category = self._field_category(m.group(1), value, in_url=False)
            if category is None:
                return m.group(0)
            return m.group(1) + m.group(2) + self._token(category, value, counts)

        def email(m: re.Match[str]) -> str:
            return self._token("email", m.group(0), counts)

        def card(m: re.Match[str]) -> str:
            digits = self._card_digits(m)
            return m.group(0) if digits is None else self._token("card", digits, counts)

        def ssn(m: re.Match[str]) -> str:
            if not _ssn_valid(m) or not _ssn_in_context(m.string, m.start()):
                return m.group(0)
            return self._token("ssn", m.group(0), counts)

        def url(m: re.Match[str]) -> str:
            return self._url_in_text(m.group(0), counts, escaped=False)

        def escaped_url(m: re.Match[str]) -> str:
            return self._url_in_text(m.group(0), counts, escaped=True)

        def basic(m: re.Match[str]) -> str:
            if not _is_basic_credential(m.group(2)):
                return m.group(0)
            return m.group(1) + self._token("auth", m.group(2), counts)

        def auth_text(m: re.Match[str]) -> str:
            if not _looks_like_secret(m.group(2)):
                return m.group(0)
            return m.group(1) + self._token("auth", m.group(2), counts)

        def text_pair(m: re.Match[str]) -> str:
            prefix, name, quote_char, value = m.group(1, 2, 3, 4)
            kept = value if quote_char else value.rstrip(_URL_TRAILING)
            if (
                not kept
                or _norm_param(name) in _PAIR_EXCLUDED_NAMES
                or not _is_secret_param(name, in_url=False)
                or not _looks_like_secret(kept)
            ):
                return m.group(0)
            return prefix + self._token("secret_param", kept, counts) + value[len(kept) :]

        steps: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
            (_INPUT_TAG_RE, html_input),
            (_ESCAPED_URL_RE, escaped_url),
            (_URL_IN_TEXT_RE, url),
            (_JWT_RE, jwt),
            (_BEARER_RE, bearer),
            (_BASIC_RE, basic),
            (_API_KEY_RE, api_key),
            (_JSON_SECRET_RE, json_secret),
            (_JSON_SECRET_NUMBER_RE, json_number),
            (_AUTH_TEXT_RE, auth_text),
            (_KV_SECRET_RE, kv_secret),
            (_TEXT_PAIR_RE, text_pair),
            (_EMAIL_RE, email),
            (_CARD_RE, card),
            (_SSN_RE, ssn),
        ]
        for pattern, repl in steps:
            text = self._outside_tokens(text, lambda s, p=pattern, r=repl: p.sub(r, s))
        if "%" in text:
            text = self._outside_tokens(text, lambda s: self._percent_runs(s, counts, form=form))
        return text

    def _url_in_text(self, found: str, counts: Counter[str], *, escaped: bool) -> str:
        """Redact a URL found inside free text as ``_url`` would, leaving it unchanged otherwise."""
        url = found.rstrip(_URL_TRAILING)
        tail = found[len(url) :]
        plain = url.replace("\\/", "/") if escaped else url
        local: Counter[str] = Counter()
        redacted = self._url(plain, local)
        if not local:
            return found
        counts.update(local)
        return (redacted.replace("/", "\\/") if escaped else redacted) + tail

    @staticmethod
    def _field_category(name: str, value: str, *, in_url: bool) -> str | None:
        if _is_secret_param(name, in_url=in_url):
            return "secret_param"
        if _is_phone(name, value):
            return "phone"
        return None

    def _input_tag(self, tag: str, counts: Counter[str]) -> str:
        """Redact the ``value`` of a password, OTP or token ``<input>``; keep the rest of the tag."""
        attrs: dict[str, str] = {}
        value: re.Match[str] | None = None
        for m in _ATTR_RE.finditer(tag, 6, len(tag) - 1):
            name = m.group(1).lower()
            raw = next((g for g in m.group(3, 4, 5) if g is not None), "")
            attrs.setdefault(name, raw)
            if name == "value" and value is None and m.group(2) is not None:
                value = m
        if value is None or not attrs["value"] or not _is_secret_input(attrs):
            return tag
        token = self._token("html_secret", attrs["value"], counts)
        replaced = f'{value.group(1)}{value.group(2)}"{token}"'
        return tag[: value.start()] + replaced + tag[value.end() :]

    def _query(self, query: str, counts: Counter[str]) -> str:
        if not query:
            return query
        out: list[str] = []
        for pair in query.split("&"):
            name, sep, raw = pair.partition("=")
            category = (
                self._field_category(unquote_plus(name), unquote_plus(raw), in_url=True)
                if sep and raw
                else None
            )
            if category is not None:
                out.append(f"{name}={self._token(category, unquote_plus(raw), counts)}")
                continue
            if sep and raw:
                decoded = unquote_plus(raw)
                redacted = self._text(decoded, counts)
                if redacted != decoded:
                    raw = quote(redacted, safe="[]:")
            out.append(f"{name}{sep}{raw}")
        return "&".join(out)

    def _url(self, url: str, counts: Counter[str]) -> str:
        if not url:
            return url
        try:
            parts = urlsplit(url)
        except ValueError:
            return self._text(url, counts)
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, _, host = netloc.rpartition("@")
            user, colon, password = userinfo.partition(":")
            if colon and password:
                userinfo = f"{user}:{self._token('url_password', unquote_plus(password), counts)}"
            netloc = f"{userinfo}@{host}"
        path = self._text(parts.path, counts)
        query = self._query(parts.query, counts)
        fragment = self._query(parts.fragment, counts) if "=" in parts.fragment else parts.fragment
        return urlunsplit((parts.scheme, netloc, path, query, fragment))

    def _cookie_pairs(self, value: str, counts: Counter[str], *, attributes: bool) -> str:
        segments = value.split(";")
        out: list[str] = []
        for i, seg in enumerate(segments):
            if attributes and i > 0:
                out.append(seg)
                continue
            name, sep, val = seg.partition("=")
            if sep and val.strip():
                out.append(f"{name}={self._token('cookie', val.strip(), counts)}")
            elif seg.strip() and not sep:
                out.append(self._token("cookie", seg.strip(), counts))
            else:
                out.append(seg)
        return ";".join(out)

    def _auth_value(self, value: str, counts: Counter[str]) -> str:
        redacted = self._text(value, counts)
        if redacted != value:
            return redacted
        scheme, sep, cred = value.partition(" ")
        if sep and cred.strip():
            return f"{scheme} {self._token('auth', cred.strip(), counts)}"
        return self._token("auth", value, counts)

    def _headers(self, headers: Mapping[str, str], counts: Counter[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in headers.items():
            n = name.lower()
            if not value:
                out[name] = value
            elif n in _AUTH_HEADERS:
                out[name] = self._auth_value(value, counts)
            elif n in _COOKIE_HEADERS:
                out[name] = self._cookie_pairs(value, counts, attributes=n != "cookie")
            elif _is_secret_header(n):
                out[name] = self._token("secret_header", value, counts)
            elif n in {"referer", "location", "content-location", "origin"}:
                out[name] = self._url(value, counts)
            else:
                out[name] = self._text(value, counts)
        return out

    def _action(self, a: ActionEvent, counts: Counter[str]) -> ActionEvent:
        value = a.value
        if value:
            target = (a.selector or "").lower()
            if any(h in target for h in _SECRET_INPUT_HINTS):
                value = self._token("secret_input", value, counts)
            else:
                value = self._text(value, counts)
        return a.model_copy(
            update={
                "selector": None if a.selector is None else self._text(a.selector, counts),
                "value": value,
                "url": None if a.url is None else self._url(a.url, counts),
                "error": None if a.error is None else self._text(a.error, counts),
            }
        )

    def _network(self, n: NetworkEvent, counts: Counter[str]) -> NetworkEvent:
        t = self._opt_text_fn(counts)
        return n.model_copy(
            update={
                "url": self._url(n.url, counts),
                "request_headers": self._headers(n.request_headers, counts),
                "request_body": t(n.request_body),
                "response_headers": self._headers(n.response_headers, counts),
                "response_body": t(n.response_body),
            }
        )

    def _snapshot(self, s: PageSnapshot, counts: Counter[str]) -> PageSnapshot:
        t = self._opt_text_fn(counts)
        return s.model_copy(
            update={
                "url": self._url(s.url, counts),
                "title": t(s.title),
                "text": self._text(s.text, counts),
                "html": t(s.html),
            }
        )

    def _console(self, c: ConsoleEvent, counts: Counter[str]) -> ConsoleEvent:
        return c.model_copy(update={"text": self._text(c.text, counts)})

    def _state(self, state: PageState | None, counts: Counter[str]) -> PageState | None:
        if state is None:
            return None
        t = self._opt_text_fn(counts)
        return state.model_copy(
            update={
                "url": self._url(state.url, counts),
                "title": t(state.title),
                "text": self._text(state.text, counts),
            }
        )


def _run_texts(run: Run) -> Iterator[str]:
    """Every free-text field of a run, for the card pre-scan in ``Redactor.redact_run``."""
    yield from (t for t in (run.task, run.agent_answer) if t)
    yield from run.agent_reasoning
    for a in run.actions:
        yield from (t for t in (a.selector, a.value, a.url) if t)
    for n in run.network:
        yield n.url
        yield from (t for t in (n.request_body, n.response_body) if t)
        yield from n.request_headers.values()
        yield from n.response_headers.values()
    for snap in run.snapshots:
        yield from (t for t in (snap.url, snap.title, snap.text, snap.html) if t)
    yield from (c.text for c in run.console)
    for state in (run.initial_state, run.final_state):
        if state is not None:
            yield from (t for t in (state.url, state.title, state.text) if t)
