from __future__ import annotations

import pytest

from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.checks.matching import (
    MISSING,
    compare,
    compile_regex,
    contains,
    excerpt,
    host_allowed,
    json_path_get,
    normalize,
    parse_body,
    parse_number,
    status_matches,
    url_contains,
    url_host,
    url_path,
)


def test_normalize_folds_unicode_case_and_whitespace() -> None:
    raw = f"  Thank{chr(0xA0)}YOU\n for   your {chr(0xFF2F)}rder "
    assert normalize(raw) == "thank you for your order"


@pytest.mark.parametrize(
    ("haystack", "needle", "expected"),
    [
        ("Thank you for your order!", "thank YOU for", True),
        ("Thank you", "order", False),
        (None, "x", False),
        ("Straße", "STRASSE", True),
    ],
)
def test_contains(haystack: str | None, needle: str, expected: bool) -> None:
    assert contains(haystack, needle) is expected


def test_excerpt_centres_on_match() -> None:
    text = "a " * 100 + "Order confirmed #123" + " b" * 100
    out = excerpt(text, "order confirmed", width=40)
    assert "order confirmed" in out
    assert out.startswith("…")
    assert out.endswith("…")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$24.00", 24.0),
        ("24,000.50", 24000.5),
        ("1.2k", 1200.0),
        ("USD 30", 30.0),
        ("-3", -3.0),
        ("€ 9.99", 9.99),
        (45, 45.0),
        (45.5, 45.5),
        ("2m", 2_000_000.0),
        ("abc", None),
        ("12 apples", None),
        (True, None),
        (None, None),
        (float("nan"), None),
        ([1], None),
    ],
)
def test_parse_number(raw: object, expected: float | None) -> None:
    assert parse_number(raw) == expected


@pytest.mark.parametrize(
    ("observed", "op", "expected", "result"),
    [
        ("$24.00", "<=", 30, True),
        (45.0, "<=", 30, False),
        ("45", ">", "30", True),
        (30, "==", "30.0", True),
        (30, "!=", "$30", False),
        (29.99, "<", 30, True),
        (29.999999999999, "<", 30, False),
        ("Red Kettle", "==", "red  kettle", True),
        ("Red Kettle", "!=", "blue kettle", True),
        ("Red Kettle", "contains", "KETTLE", True),
        (["a", "b"], "contains", "b", True),
        ("apple", "<", "banana", True),
        (None, "==", "null", True),
    ],
)
def test_compare(observed: object, op: str, expected: object, result: bool) -> None:
    assert compare(observed, op, expected) is result  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "spec", "expected"),
    [
        (201, "201", True),
        (200, "201", False),
        (204, "2xx", True),
        (302, "2XX", False),
        (None, "2xx", False),
        (None, None, True),
        (500, None, True),
    ],
)
def test_status_matches(status: int | None, spec: str | None, expected: bool) -> None:
    assert status_matches(status, spec) is expected


@pytest.mark.parametrize("spec", ["ok", "200-299", "6xx", "20"])
def test_status_matches_rejects_malformed_spec(spec: str) -> None:
    with pytest.raises(InvalidPredicateError):
        status_matches(200, spec)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("items.0.price", 45.0),
        ("items[0].price", 45.0),
        ("$.items.0.sku", "K-1"),
        ("items.-1.price", 45.0),
        ("total", None),
        ("items.1.price", MISSING),
        ("items.x", MISSING),
        ("nope", MISSING),
        ("total.amount", MISSING),
        ("", {"items": [{"price": 45.0, "sku": "K-1"}], "total": None}),
    ],
)
def test_json_path_get(path: str, expected: object) -> None:
    obj = {"items": [{"price": 45.0, "sku": "K-1"}], "total": None}
    assert json_path_get(obj, path) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("a=1&b=two", {"a": "1", "b": "two"}),
        ("not json", MISSING),
        ("", MISSING),
        (None, MISSING),
        ("[1, 2]", [1, 2]),
    ],
)
def test_parse_body(body: str | None, expected: object) -> None:
    assert parse_body(body) == expected


def test_url_helpers() -> None:
    url = "https://API.Shop.test/api/orders%2Fnew?id=7"
    assert url_contains(url, "/api/ORDERS/new")
    assert url_path(url) == "/api/orders%2Fnew?id=7"
    assert url_path("https://shop.test") == "/"
    assert url_host(url) == "api.shop.test"


@pytest.mark.parametrize(
    ("host", "allowed", "expected"),
    [
        ("shop.test", ["shop.test"], True),
        ("api.shop.test", ["shop.test"], True),
        ("evilshop.test", ["shop.test"], False),
        ("tracker.io", ["shop.test"], False),
        (None, ["shop.test"], False),
    ],
)
def test_host_allowed(host: str | None, allowed: list[str], expected: bool) -> None:
    assert host_allowed(host, allowed) is expected


def test_compile_regex_raises_typed_error() -> None:
    assert compile_regex(r"total: (\d+)").search("TOTAL: 3")
    with pytest.raises(InvalidPredicateError):
        compile_regex("(unclosed")
