"""Redactor: replaces every secret and personal value in a run, a text or a model with a
fingerprinted token (see ``evidence_judge.redact``)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from functools import partial
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
from evidence_judge.redact.detectors import (
    API_KEY_RE,
    ATTR_RE,
    AUTH_HEADERS,
    AUTH_TEXT_RE,
    BASIC_RE,
    BEARER_RE,
    CARD_PREFIX_RE,
    CARD_RE,
    COOKIE_HEADERS,
    EMAIL_RE,
    ESCAPED_URL_RE,
    INPUT_TAG_RE,
    JSON_SECRET_NUMBER_RE,
    JSON_SECRET_RE,
    JWT_RE,
    KV_SECRET_RE,
    PAIR_EXCLUDED_NAMES,
    SECRET_INPUT_HINTS,
    SSN_RE,
    TEXT_PAIR_RE,
    URL_IN_TEXT_RE,
    URL_TRAILING,
    field_category,
    is_basic_credential,
    is_secret_header,
    is_secret_input,
    is_secret_param,
    looks_like_card,
    looks_like_secret,
    luhn_ok,
    norm_param,
    only_digits,
    ssn_in_context,
    ssn_valid,
)

_PROCESS_KEY = secrets.token_bytes(32)
TOKEN_RE = re.compile(r"\[REDACTED:[a-z_]+:[0-9a-f]{8}\]")
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


_FINGERPRINT_NORMALISERS: dict[str, Callable[[str], str]] = {
    "email": lambda v: v.strip().casefold(),
    "card": only_digits,
    "phone": only_digits,
}
"""Canonical form per category, so values that evidence matching treats as equal (matching is
case-insensitive; card numbers are compared without separators) get equal tokens. Credentials
stay case-sensitive: two tokens differing only in case are different secrets."""
_TextRepl = Callable[[Counter[str], re.Match[str]], str]


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
        if not (13 <= len(digits) <= 19 and CARD_PREFIX_RE.match(digits) and luhn_ok(digits)):
            return None
        if digits in self._known_cards or looks_like_card(m.string, m.start(), m.end(), digits):
            self._known_cards.add(digits)
            return digits
        return None

    def _learn_cards(self, text: str) -> None:
        for m in CARD_RE.finditer(text):
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
        for pattern, repl in self._text_steps(counts):
            text = self._outside_tokens(text, lambda s, p=pattern, r=repl: p.sub(r, s))
        if "%" in text:
            text = self._outside_tokens(text, lambda s: self._percent_runs(s, counts, form=form))
        return text

    def _text_steps(
        self, counts: Counter[str]
    ) -> list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]]:
        """The free-text substitutions in the order they run, each counting into ``counts``."""
        handlers: tuple[tuple[re.Pattern[str], _TextRepl], ...] = (
            (INPUT_TAG_RE, self._sub_input_tag),
            (ESCAPED_URL_RE, self._sub_escaped_url),
            (URL_IN_TEXT_RE, self._sub_url),
            (JWT_RE, self._sub_jwt),
            (BEARER_RE, self._sub_bearer),
            (BASIC_RE, self._sub_basic),
            (API_KEY_RE, self._sub_api_key),
            (JSON_SECRET_RE, self._sub_json_secret),
            (JSON_SECRET_NUMBER_RE, self._sub_json_number),
            (AUTH_TEXT_RE, self._sub_auth_text),
            (KV_SECRET_RE, self._sub_kv_secret),
            (TEXT_PAIR_RE, self._sub_text_pair),
            (EMAIL_RE, self._sub_email),
            (CARD_RE, self._sub_card),
            (SSN_RE, self._sub_ssn),
        )
        return [(pattern, partial(handler, counts)) for pattern, handler in handlers]

    def _sub_input_tag(self, counts: Counter[str], m: re.Match[str]) -> str:
        return self._input_tag(m.group(0), counts)

    def _sub_jwt(self, counts: Counter[str], m: re.Match[str]) -> str:
        return self._token("jwt", m.group(0), counts)

    def _sub_bearer(self, counts: Counter[str], m: re.Match[str]) -> str:
        return m.group(1) + self._token("bearer", m.group(2), counts)

    def _sub_api_key(self, counts: Counter[str], m: re.Match[str]) -> str:
        return self._token("api_key", m.group(0), counts)

    def _sub_json_secret(self, counts: Counter[str], m: re.Match[str]) -> str:
        category = field_category(m.group(2), m.group(3), in_url=False)
        if category is None:
            return m.group(0)
        return m.group(1) + self._token(category, m.group(3), counts) + m.group(4)

    def _sub_json_number(self, counts: Counter[str], m: re.Match[str]) -> str:
        category = field_category(m.group(2), m.group(3), in_url=False)
        if category is None:
            return m.group(0)
        return f'{m.group(1)}"{self._token(category, m.group(3), counts)}"'

    def _sub_kv_secret(self, counts: Counter[str], m: re.Match[str]) -> str:
        value = unquote_plus(m.group(3))
        category = field_category(m.group(1), value, in_url=False)
        if category is None:
            return m.group(0)
        return m.group(1) + m.group(2) + self._token(category, value, counts)

    def _sub_email(self, counts: Counter[str], m: re.Match[str]) -> str:
        return self._token("email", m.group(0), counts)

    def _sub_card(self, counts: Counter[str], m: re.Match[str]) -> str:
        digits = self._card_digits(m)
        return m.group(0) if digits is None else self._token("card", digits, counts)

    def _sub_ssn(self, counts: Counter[str], m: re.Match[str]) -> str:
        if not ssn_valid(m) or not ssn_in_context(m.string, m.start()):
            return m.group(0)
        return self._token("ssn", m.group(0), counts)

    def _sub_url(self, counts: Counter[str], m: re.Match[str]) -> str:
        return self._url_in_text(m.group(0), counts, escaped=False)

    def _sub_escaped_url(self, counts: Counter[str], m: re.Match[str]) -> str:
        return self._url_in_text(m.group(0), counts, escaped=True)

    def _sub_basic(self, counts: Counter[str], m: re.Match[str]) -> str:
        if not is_basic_credential(m.group(2)):
            return m.group(0)
        return m.group(1) + self._token("auth", m.group(2), counts)

    def _sub_auth_text(self, counts: Counter[str], m: re.Match[str]) -> str:
        if not looks_like_secret(m.group(2)):
            return m.group(0)
        return m.group(1) + self._token("auth", m.group(2), counts)

    def _sub_text_pair(self, counts: Counter[str], m: re.Match[str]) -> str:
        prefix, name, quote_char, value = m.group(1, 2, 3, 4)
        kept = value if quote_char else value.rstrip(URL_TRAILING)
        if (
            not kept
            or norm_param(name) in PAIR_EXCLUDED_NAMES
            or not is_secret_param(name, in_url=False)
            or not looks_like_secret(kept)
        ):
            return m.group(0)
        return prefix + self._token("secret_param", kept, counts) + value[len(kept) :]

    def _url_in_text(self, found: str, counts: Counter[str], *, escaped: bool) -> str:
        """Redact a URL found inside free text as ``_url`` would, leaving it unchanged otherwise."""
        url = found.rstrip(URL_TRAILING)
        tail = found[len(url) :]
        plain = url.replace("\\/", "/") if escaped else url
        local: Counter[str] = Counter()
        redacted = self._url(plain, local)
        if not local:
            return found
        counts.update(local)
        return (redacted.replace("/", "\\/") if escaped else redacted) + tail

    def _input_tag(self, tag: str, counts: Counter[str]) -> str:
        """Redact the ``value`` of a password, OTP or token ``<input>``; keep the rest of the tag."""
        attrs: dict[str, str] = {}
        value: re.Match[str] | None = None
        for m in ATTR_RE.finditer(tag, 6, len(tag) - 1):
            name = m.group(1).lower()
            raw = next((g for g in m.group(3, 4, 5) if g is not None), "")
            attrs.setdefault(name, raw)
            if name == "value" and value is None and m.group(2) is not None:
                value = m
        if value is None or not attrs["value"] or not is_secret_input(attrs):
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
                field_category(unquote_plus(name), unquote_plus(raw), in_url=True)
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
            elif n in AUTH_HEADERS:
                out[name] = self._auth_value(value, counts)
            elif n in COOKIE_HEADERS:
                out[name] = self._cookie_pairs(value, counts, attributes=n != "cookie")
            elif is_secret_header(n):
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
            if any(h in target for h in SECRET_INPUT_HINTS):
                value = self._token("secret_input", value, counts)
            else:
                value = self._text(value, counts)
        return a.model_copy(
            update={
                "selector": None if a.selector is None else self._text(a.selector, counts),
                "value": value,
                "url": None if a.url is None else self._url(a.url, counts),
                "error": None if a.error is None else self._text(a.error, counts),
                "file_text": None if a.file_text is None else self._text(a.file_text, counts),
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
        yield from (t for t in (a.selector, a.value, a.url, a.file_text) if t)
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
