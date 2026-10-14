"""Normalisation and comparison helpers shared by the deterministic checkers."""

from __future__ import annotations

import json
import math
import re
from enum import Enum
from typing import Final, Literal, cast
from urllib.parse import parse_qsl, unquote, urlsplit

from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.textnorm import normalize

Op = Literal["==", "!=", "<", "<=", ">", ">=", "contains"]


class _Missing(Enum):
    MISSING = "missing"


MISSING: Final = _Missing.MISSING

_NUMBER = re.compile(
    r"""
    ^(?:[a-z]{3}\s*)?
    [$€£¥₹]?\s*
    (?P<sign>[+-])?\s*
    [$€£¥₹]?\s*
    (?P<int>\d{1,3}(?:,\d{3})+|\d+)
    (?P<frac>\.\d+)?
    \s*(?P<suffix>[kmb])?
    \s*(?:[a-z]{3})?$
    """,
    re.VERBOSE,
)
_SUFFIX_SCALE: Final = {"k": 1e3, "m": 1e6, "b": 1e9}
_STATUS_CLASS = re.compile(r"^([1-5])xx$")
_STATUS_EXACT = re.compile(r"^[1-5]\d\d$")
_INDEX_BRACKETS = re.compile(r"\[(-?\d+)\]")
_TRUNCATION_MARK = re.compile(r"…\[truncated[^\]]{0,60}\]\s*\Z")
_TRUNCATION_TAIL: Final = 80


def is_truncated(body: str | None) -> bool:
    """True if ingest cut this body short and appended its truncation marker."""
    return body is not None and _TRUNCATION_MARK.search(body[-_TRUNCATION_TAIL:]) is not None


def contains(haystack: str | None, needle: str) -> bool:
    if haystack is None:
        return False
    return normalize(needle) in normalize(haystack)


def excerpt(text: str, needle: str, width: int = 80) -> str:
    """A short window of normalised `text` around the first occurrence of `needle`."""
    norm, want = normalize(text), normalize(needle)
    at = norm.find(want)
    if at < 0:
        return norm[:width]
    pad = max(0, (width - len(want)) // 2)
    start, end = max(0, at - pad), min(len(norm), at + len(want) + pad)
    return f"{'…' if start else ''}{norm[start:end]}{'…' if end < len(norm) else ''}"


def parse_number(value: object) -> float | None:
    """Parse '$24.00', '24,000.50', '1.2k', 'USD 30' or a JSON number; None if not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    match = _NUMBER.match(normalize(value))
    if match is None:
        return None
    number = float(match["int"].replace(",", "") + (match["frac"] or ""))
    if match["suffix"]:
        number *= _SUFFIX_SCALE[match["suffix"]]
    return -number if match["sign"] == "-" else number


def as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def compare(observed: object, op: Op, expected: object) -> bool:
    """Compare numerically when both sides parse as numbers, else as normalised strings."""
    if op == "contains":
        return normalize(as_text(expected)) in normalize(as_text(observed))
    left, right = parse_number(observed), parse_number(expected)
    if left is not None and right is not None:
        return _ordered(left, op, right)
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


def validate_status_spec(spec: str | None) -> None:
    if spec is None:
        return
    value = spec.strip().lower()
    if not (_STATUS_CLASS.match(value) or _STATUS_EXACT.match(value)):
        raise InvalidPredicateError(
            f"status {spec!r} is neither an exact code nor a class like 2xx"
        )


def status_matches(status: int | None, spec: str | None) -> bool:
    """True if `status` satisfies '201', '2xx' or None (any status, including none)."""
    if spec is None:
        return True
    validate_status_spec(spec)
    if status is None:
        return False
    value = spec.strip().lower()
    if klass := _STATUS_CLASS.match(value):
        return status // 100 == int(klass[1])
    return status == int(value)


def json_path_get(obj: object, path: str) -> object:
    """Follow a dotted path ('items.0.price', 'items[0].price', '$.total') or return MISSING."""
    normalized = _INDEX_BRACKETS.sub(r".\1", path.strip()).removeprefix("$").strip(".")
    if not normalized:
        return obj
    current: object = obj
    for segment in normalized.split("."):
        current = _step(current, segment)
        if current is MISSING:
            return MISSING
    return current


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


def parse_body(body: str | None) -> object:
    """Parse a JSON or form-encoded body into Python data, or return MISSING."""
    if body is None or not body.strip():
        return MISSING
    try:
        return json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        pass
    if "=" not in body:
        return MISSING
    pairs = parse_qsl(body.strip(), keep_blank_values=True)
    return dict(pairs) if pairs else MISSING


def url_contains(url: str, needle: str) -> bool:
    return needle.casefold() in unquote(url).casefold()


def url_path(url: str) -> str:
    """Path plus query of a URL, for compact human-readable reasons ('/api/orders?id=1')."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def url_host(url: str) -> str | None:
    return urlsplit(url).hostname


def host_allowed(host: str | None, allowed: list[str]) -> bool:
    """Exact host or any subdomain of an allowed host ('shop.test' covers 'api.shop.test')."""
    if host is None:
        return False
    host = host.casefold().rstrip(".")
    for entry in allowed:
        base = entry.casefold().strip().rstrip(".")
        if host == base or host.endswith(f".{base}"):
            return True
    return False


def compile_regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise InvalidPredicateError(f"invalid regex {pattern!r}: {exc}") from exc
