"""Secret and PII redaction for recorded evidence and agent output.

Every sensitive value is replaced by ``[REDACTED:<category>:<fingerprint>]`` where the fingerprint
is the first 8 hex characters of an HMAC-SHA256 of the value. Equal values therefore produce equal
tokens, so a claim that quotes a redacted value still matches the redacted evidence, while the
value itself never leaves the process. Redacting already-redacted data is a no-op.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

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
        "x_amz_signature",
        "x_amz_credential",
        "x_amz_security_token",
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
        "otp",
        "totp",
        "mfa",
        "pin",
        "cvv",
        "cvc",
        "csc",
        "csrf",
        "xsrf",
        "apikey",
    }
)
_SECRET_FIELD_SUFFIXES = ("password", "token", "secret")
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
_JSON_SECRET_RE = re.compile(r'("([A-Za-z0-9_\-.]{1,64})"\s*:\s*")((?:[^"\\]|\\.)+)(")')
_KV_SECRET_RE = re.compile(r"(?<![A-Za-z0-9_\-.])([A-Za-z0-9_\-.]{1,64})(=)([^&\s\"'<>#;,]+)")
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![A-Za-z0-9-])"
)
_CARD_RE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
_CARD_PREFIX_RE = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[0478]|6)")
_USERINFO_RE = re.compile(r"(?<=://)([^/@\s:]+):([^/@\s]+)@")

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


def _norm_param(name: str) -> str:
    return name.lower().replace("-", "_").strip("[]")


def _is_secret_param(name: str, *, in_url: bool) -> bool:
    n = _norm_param(name)
    if n in _SECRET_PARAMS or n.endswith(_SECRET_PARAM_SUFFIXES):
        return True
    return in_url and n in _AMBIGUOUS_URL_PARAMS


def _is_secret_field_name(name: str) -> bool:
    words = [w.lower() for w in _FIELD_WORD_RE.findall(name)]
    return any(w in _SECRET_FIELD_WORDS or w.endswith(_SECRET_FIELD_SUFFIXES) for w in words)


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
    """Produces redacted copies of runs, text and models. Thread-safe and side-effect free."""

    def __init__(self, key: bytes) -> None:
        if not key:
            raise ValueError("redaction key must not be empty")
        self._key = key

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
        return f"[REDACTED:{category}:{self.fingerprint(value)}]"

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

    def _text(self, text: str, counts: Counter[str]) -> str:
        if not text:
            return text

        def html_input(m: re.Match[str]) -> str:
            return self._input_tag(m.group(0), counts)

        def jwt(m: re.Match[str]) -> str:
            return self._token("jwt", m.group(0), counts)

        def bearer(m: re.Match[str]) -> str:
            return m.group(1) + self._token("bearer", m.group(2), counts)

        def api_key(m: re.Match[str]) -> str:
            return self._token("api_key", m.group(0), counts)

        def json_secret(m: re.Match[str]) -> str:
            if not _is_secret_param(m.group(2), in_url=False):
                return m.group(0)
            return m.group(1) + self._token("secret_param", m.group(3), counts) + m.group(4)

        def kv_secret(m: re.Match[str]) -> str:
            if not _is_secret_param(m.group(1), in_url=False):
                return m.group(0)
            return (
                m.group(1)
                + m.group(2)
                + self._token("secret_param", unquote_plus(m.group(3)), counts)
            )

        def email(m: re.Match[str]) -> str:
            return self._token("email", m.group(0), counts)

        def card(m: re.Match[str]) -> str:
            digits = re.sub(r"[ -]", "", m.group(0))
            if 13 <= len(digits) <= 19 and _CARD_PREFIX_RE.match(digits) and _luhn_ok(digits):
                return self._token("card", digits, counts)
            return m.group(0)

        steps: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
            (_INPUT_TAG_RE, html_input),
            (_JWT_RE, jwt),
            (_BEARER_RE, bearer),
            (_API_KEY_RE, api_key),
            (_JSON_SECRET_RE, json_secret),
            (_KV_SECRET_RE, kv_secret),
            (_EMAIL_RE, email),
            (_CARD_RE, card),
        ]
        for pattern, repl in steps:
            text = self._outside_tokens(text, lambda s, p=pattern, r=repl: p.sub(r, s))
        return text

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
            if sep and raw and _is_secret_param(unquote_plus(name), in_url=True):
                out.append(f"{name}={self._token('secret_param', unquote_plus(raw), counts)}")
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
