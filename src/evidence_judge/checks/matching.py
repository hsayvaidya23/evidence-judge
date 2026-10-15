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
from evidence_judge.textnorm import normalize

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


def json_path_get(obj: object, path: str) -> object:
    """Follow a dotted path ('items.0.price', 'items[0].price', '$.total') or return MISSING."""
    current: object = obj
    for segment in _segments(path):
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
