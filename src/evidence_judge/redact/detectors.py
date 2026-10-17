"""What counts as a secret or a personal value: the patterns redaction searches text for, and
the rules that decide whether a match, a field name or a header holds one."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping

AUTH_HEADERS = frozenset({"authorization", "proxy-authorization"})
COOKIE_HEADERS = frozenset({"cookie", "set-cookie", "set-cookie2"})
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
SECRET_INPUT_HINTS = ("password", "passwd", "pwd", "secret", "token", "otp", "cvv", "cvc", "pin")
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
SSN_RE = re.compile(r"(?<![0-9-])([0-9]{3})-([0-9]{2})-([0-9]{4})(?![0-9-])")
_SSN_CONTEXT_RE = re.compile(r"(?<![a-z])ssn(?![a-z])|social security")
_SSN_CONTEXT_CHARS = 40
_SECRET_AUTOCOMPLETE = frozenset({"current-password", "new-password", "one-time-code", "cc-csc"})
INPUT_TAG_RE = re.compile(
    r"<input\b(?:[^<>\"']|\"[^\"]{0,4096}\"|'[^']{0,4096}'){0,4096}>", re.IGNORECASE
)
ATTR_RE = re.compile(r"""([^\s"'<>/=]+)(?:(\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""")
_FIELD_WORD_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
JWT_RE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9\-._~+/]{8,}=*)")
BASIC_RE = re.compile(r"(?i)\b(basic\s+)([A-Za-z0-9+/]{4,}={0,2})(?![A-Za-z0-9+/=])")
_AUTH_SCHEMES = r"(?:basic|bearer|digest|token|negotiate|ntlm)"
AUTH_TEXT_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_-])((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:{_AUTH_SCHEMES}\s+)?)"
    rf"(?!{_AUTH_SCHEMES}\b)([^\s\"'<>,;]+)"
)
AUTH_SCHEME_END_RE = re.compile(rf"(?i){_AUTH_SCHEMES}\s+$")
LABELLED_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_\-.])((?:password|passwd|pwd|passcode|passphrase|token|secret"
    r"|api[ _-]?key)[ \t]+(?:(?:is|was)[ \t]+)?)([^\s\"'<>,;=(\[{/~.][^\s\"'<>,;=]*+)(?!=)"
)
"""``password hunter2`` or ``api key is 9f8e...``: a secret's name and its value with no ``:`` or
``=`` between them."""
_COOKIE_PAIR = r"[!#$%&*+\-.^_`|~0-9A-Za-z]+=[^\s;,\"'<>\\]*"
COOKIE_TEXT_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_\-.])((set-)?cookie2?[\"']?[ \t]*:[ \t]*[\"']?)"
    rf"({_COOKIE_PAIR}(?:;[ \t]*{_COOKIE_PAIR})*)"
)
"""A ``Cookie:`` or ``Set-Cookie:`` line written in free text, up to the end of its pairs."""
URL_IN_TEXT_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>\\`]+")
ESCAPED_URL_RE = re.compile(r"(?i)\b(?:https?|wss?):\\/\\/(?:[^\s\"'<>\\`]|\\/)+")
URL_TRAILING = ".,;:!?)]}"
TEXT_PAIR_RE = re.compile(
    r"(?<![A-Za-z0-9_\-.\[\]])(([A-Za-z][A-Za-z0-9_\-.\[\]]{0,63})[\"']?\s*[:=]\s*([\"']?))"
    r"([^\s\"'<>,;&]{1,256})(?=\3)"
)
"""``password: hunter2``, ``password = 'hunter2'`` and similar pairs written in free text."""
PAIR_EXCLUDED_NAMES = frozenset({"pass", "pw", "ssn"})
"""Names too common in prose ("Boarding pass: ...") or handled by their own rule (SSN)."""
API_KEY_RE = re.compile(
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
JSON_SECRET_RE = re.compile(r'("([A-Za-z0-9_\-.\[\]]{1,64})"\s*:\s*")((?:[^"\\]|\\.)+)(")')
JSON_SECRET_NUMBER_RE = re.compile(
    r'("([A-Za-z0-9_\-.\[\]]{1,64})"\s*:\s*)(-?[0-9]{1,64}(?:\.[0-9]{1,64})?)(?![0-9.eE])'
)
KV_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_\-.\[\]])([A-Za-z0-9_\-.\[\]]{1,64})(=)([^&\s\"'<>#;,]+)"
)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![A-Za-z0-9-])"
)
CARD_RE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
CARD_PREFIX_RE = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[0478]|6)")
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


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def only_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


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


def looks_like_card(text: str, start: int, end: int, digits: str) -> bool:
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


def norm_param(name: str) -> str:
    return name.lower().replace("-", "_").strip("[]")


def is_secret_param(name: str, *, in_url: bool) -> bool:
    n = norm_param(name)
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
    return _PHONE_VALUE_RE.fullmatch(value) is not None and 7 <= len(only_digits(value)) <= 15


def ssn_valid(m: re.Match[str]) -> bool:
    area, group, serial = m.group(1, 2, 3)
    return area not in {"000", "666"} and area[0] != "9" and group != "00" and serial != "0000"


def ssn_in_context(text: str, start: int) -> bool:
    field = _FIELD_BEFORE_RE.search(text, max(0, start - _FIELD_LOOKBACK_CHARS), start)
    if field is not None and _is_secret_field_name(field.group(1)):
        return True
    before = text[max(0, start - _SSN_CONTEXT_CHARS) : start].lower()
    return _SSN_CONTEXT_RE.search(before) is not None


def looks_like_secret(value: str) -> bool:
    """A free-text value that could be a credential: has a digit or symbol, or is long.

    Keeps prose such as "Password: required" or "Authorization: pending" intact.
    """
    return len(value) >= 20 or any(not ch.isalpha() for ch in value)


def looks_like_labelled_secret(value: str) -> bool:
    """A value written after a secret's name without a separator that could be a credential.

    It needs six or more characters and a digit, a symbol other than ``-`` or ``'``, mixed case
    inside the word, or 20+ characters, so prose such as "password field was empty", "token
    expired" or "password-protected" is left alone. URLs are redacted by their own rule.
    """
    if len(value) < 6 or "://" in value:
        return False
    if len(value) >= 20 or any(ch.isdigit() for ch in value):
        return True
    if any(not ch.isalnum() and ch not in "-'" for ch in value):
        return True
    rest = value[1:]
    return any(ch.isupper() for ch in rest) and any(ch.islower() for ch in value)


def is_basic_credential(value: str) -> bool:
    """Whether ``value`` is base64 of ``user:password`` (HTTP Basic credentials)."""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    return ":" in decoded and decoded.isprintable()


def is_secret_input(attrs: Mapping[str, str]) -> bool:
    """Whether an ``<input>`` holds a credential: by type, autocomplete hint, name or id."""
    if attrs.get("type", "").lower() == "password":
        return True
    autocomplete = set(attrs.get("autocomplete", "").lower().split())
    if autocomplete & _SECRET_AUTOCOMPLETE:
        return True
    return any(_is_secret_field_name(attrs.get(a, "")) for a in ("name", "id"))


def is_secret_header(name: str) -> bool:
    n = name.lower()
    return n in _SECRET_HEADERS or any(p in n for p in _SECRET_HEADER_PARTS)


def field_category(name: str, value: str, *, in_url: bool) -> str | None:
    """The category of a named value that is a secret parameter or a phone number, else None."""
    if is_secret_param(name, in_url=in_url):
        return "secret_param"
    if _is_phone(name, value):
        return "phone"
    return None
