from __future__ import annotations

import contextlib
import json
import time

import pytest

from evidence_judge.checks.errors import InvalidPredicateError, RegexTimeoutError
from evidence_judge.checks.matching import (
    MAX_REGEX_INPUT,
    MAX_REGEX_MATCHES,
    MISSING,
    body_values,
    compare,
    compile_regex,
    contains,
    excerpt,
    normalize,
    parse_body,
    parse_number,
    regex_findall,
    regex_search,
    shows_amount,
    status_matches,
    url_contains,
    url_host,
    url_path,
)
from evidence_judge.core.predicates import MAX_REGEX_LENGTH


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
        ("1 500", 1500.0),
        (f"1{chr(0x2009)}500", 1500.0),
        (f"1{chr(0x202F)}500,50 €", 1500.5),
        ("1'500", 1500.0),
        (f"1{chr(0x2019)}500.25", 1500.25),
        ("1.500,00 €", 1500.0),
        ("€1.500,00", 1500.0),
        ("24,5", 24.5),
        ("24,00 EUR", 24.0),
        ("1500 EUR", 1500.0),
        ("USD 1,500.00", 1500.0),
        ("1,500.00 USD", 1500.0),
        ("Rs. 1500", 1500.0),
        ("1500.00 zł", 1500.0),
        ("$1.5K", 1500.0),
        ("+12", 12.0),
        (f"{chr(0x2212)}$24", -24.0),
        ("0.125", 0.125),
        ("1,234,567", 1234567.0),
        ("1.500", None),
        ("0,125", None),
        ("1,50,000", None),
        ("1 50", None),
        ("1,500 000", None),
        ("1.500.000,5,0", None),
        ("+-3", None),
        ("1e5", None),
        ("1e3", None),
        ("1E3", None),
        (1e3, 1000.0),
        ("NaN", None),
        ("nan", None),
        ("Infinity", None),
        ("-inf", None),
        (float("inf"), None),
        ("24,00 €", 24.0),
        ("1,024.00", 1024.0),
        ("1.024,00", 1024.0),
        ("-24", -24.0),
        ("(24.00)", None),
        ("30.00 USD", 30.0),
        ("$9¹", 9.0),
        ("$9¹ ", 9.0),
        ("24.00€²", None),
        ("24.00²", 24.0),
        ("2²", 2.0),
        ("2₂", 2.0),
        ("2²5", None),
        ("²5", None),
        ("20 m²", None),
        ("½", None),
        ("1½", None),
        ("①", None),
        (f"{chr(0xFF11)}{chr(0xFF12)}", 12.0),
        ("٢٤", 24.0),
        ("-", None),
        ("N/A", None),
        ("", None),
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
        (None, "==", "null", True),
        ("1 500", "<=", 1000, False),
        ("1.500,00 €", ">=", 1000, True),
        ("1'500", ">", 1000, True),
        ("N/A", "!=", 30, True),
    ],
)
def test_compare(observed: object, op: str, expected: object, result: bool) -> None:
    assert compare(observed, op, expected) is result  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("observed", "op", "expected"),
    [
        ("apple", "<", "banana"),
        ("-", "<=", 1000),
        ("N/A", ">=", 1000),
        ("", "<", 1000),
        ("1.500", "<=", 1000),
        (30, "<", "thirty"),
        (None, ">", 0),
    ],
)
def test_ordering_on_non_numbers_is_undecidable(
    observed: object, op: str, expected: object
) -> None:
    assert compare(observed, op, expected) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "spec", "expected"),
    [
        (201, "201", True),
        (200, "201", False),
        (204, "2xx", True),
        (302, "2XX", False),
        (None, "2xx", False),
        (None, None, False),
        (500, None, True),
    ],
)
def test_status_matches(status: int | None, spec: str | None, expected: bool) -> None:
    assert status_matches(status, spec) is expected


@pytest.mark.parametrize("spec", ["success", "200-299", "6xx", "20"])
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
def test_body_values_follow_a_path(path: str, expected: object) -> None:
    obj = {"items": [{"price": 45.0, "sku": "K-1"}], "total": None}
    found = body_values(json.dumps(obj), path, form=False)
    assert found == ([] if expected is MISSING else [expected])


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


@pytest.mark.parametrize(
    ("body", "path", "expected"),
    [
        ('{"price": 20}', "price", [20]),
        ('{"price": 999, "price": 20}', "price", [999, 20]),
        ('{"price": 1, "price": 2, "price": 3}', "price", [1, 2, 3]),
        ("price=999&price=20", "price", ["999", "20"]),
        ('{"items": [{"price": 1, "price": 2}]}', "items.0.price", [1, 2]),
        ('{"a": {"p": 1}, "a": {"p": 2}}', "a.p", [1, 2]),
        ('{"a": {"p": 1}, "a": {"q": 2}}', "a.p", [1]),
        ('{"price": [1, 2]}', "price", [[1, 2]]),
        ('{"price": 20}', "nope", []),
        ("not json", "price", []),
        (None, "price", []),
        ('{"a": 1}', "", [{"a": 1}]),
    ],
)
def test_body_values_reads_every_value_of_a_repeated_key(
    body: str | None, path: str, expected: list[object]
) -> None:
    assert body_values(body, path) == expected


def test_body_values_reads_forms_only_when_asked() -> None:
    assert body_values("price=20", "price", form=False) == []
    assert body_values('{"price": 20}', "price", form=False) == [20]


def test_parse_body_keeps_the_last_value_of_a_repeated_key() -> None:
    assert parse_body("a=1&a=2") == {"a": "2"}
    assert parse_body('{"a": 1, "a": 2}') == {"a": 2}


def test_url_helpers() -> None:
    url = "https://API.Shop.test/api/orders%2Fnew?id=7"
    assert url_contains(url, "/api/ORDERS/new")
    assert url_path(url) == "/api/orders%2Fnew?id=7"
    assert url_path("https://shop.test") == "/"
    assert url_host(url) == "api.shop.test"
    assert url_host("https://shop.test:8443/order") == "shop.test:8443"
    assert url_host("http://shop.test:80/") == "shop.test"
    assert url_host("about:blank") is None


def test_compile_regex_raises_typed_error() -> None:
    assert compile_regex(r"total: (\d+)").search("TOTAL: 3")
    with pytest.raises(InvalidPredicateError):
        compile_regex("(unclosed")


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        (r"(a+)+$", "a" * 9000 + "!"),
        (r"(\w|\d)+$", "a" * 9000 + "!"),
        (r"(x+x+)+y", "x" * 9000),
    ],
    ids=["nested-plus", "alternation", "adjacent-plus"],
)
@pytest.mark.perf
def test_backtracking_regex_times_out_with_a_typed_error(pattern: str, text: str) -> None:
    start = time.perf_counter()
    with pytest.raises(RegexTimeoutError, match="regex timed out"):
        regex_search(compile_regex(pattern), text)
    assert time.perf_counter() - start < 0.2


@pytest.mark.perf
def test_quadratic_patterns_on_long_input_finish_fast() -> None:
    text = "a" * 9000 + "!"
    start = time.perf_counter()
    for pattern in (r".*.*.*=", r"a*a*a*a*b", r"\w*\w*\w*\w*!x", r"[a-z]*[a-z]*[a-z]*1"):
        with contextlib.suppress(RegexTimeoutError):
            assert regex_search(compile_regex(pattern), text) is None
    assert time.perf_counter() - start < 0.2


def test_compile_regex_rejects_long_patterns() -> None:
    with pytest.raises(InvalidPredicateError, match="longer than"):
        compile_regex("a" * (MAX_REGEX_LENGTH + 1))


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        (r"/order/[A-Z]\d+$", "https://shop.test/order/A1"),
        (r"Total:\s*\$([0-9.]+)", "Total: $24.00"),
        (r"(?:www\.)?shop\.test/(cart|checkout)", "https://www.shop.test/checkout"),
        (r"(ab)?c+", "abccc"),
        (r"(\d{3})-(\d{4})", "555-1234"),
        (r"[(]+a+[)]+", "((aa))"),
        (r"\(a+\)+", "(aa))"),
        (r"(?P<id>\d+)/items", "/orders/42/items"),
        (r"a{2}b{1,3}", "aabbb"),
        (r"(ab){2}", "abab"),
        (r"(?:a|b)*c", "ababc"),
        (r"([\w-]+\.)+com", "shop.example.com"),
        (r"(?<=order )\d+", "order 42"),
    ],
)
def test_compile_regex_accepts_ordinary_patterns(pattern: str, text: str) -> None:
    assert regex_search(compile_regex(pattern), text) is not None


def test_regex_search_reads_only_the_bounded_head_and_tail() -> None:
    regex = compile_regex(r"needle (\d+)")
    middle = "x" * MAX_REGEX_INPUT + "needle 1" + "x" * MAX_REGEX_INPUT
    assert regex_search(regex, middle) is None
    tail = "x" * (3 * MAX_REGEX_INPUT) + "needle 2 end"
    match = regex_search(regex, tail)
    assert match is not None
    assert match.group(1) == "2"
    assert regex_search(regex, "needle 3" + "x" * (3 * MAX_REGEX_INPUT)) is not None


def test_regex_findall_reports_whether_it_read_every_match() -> None:
    regex = compile_regex(r"n(\d)")
    found = regex_findall(regex, "n1 n2")
    assert [m.group(1) for m in found.matches] == ["1", "2"]
    assert found.complete
    exact = regex_findall(regex, "n1 " * MAX_REGEX_MATCHES)
    assert len(exact.matches) == MAX_REGEX_MATCHES
    assert exact.complete
    capped = regex_findall(regex, "n1 " * (MAX_REGEX_MATCHES + 1))
    assert len(capped.matches) == MAX_REGEX_MATCHES
    assert not capped.complete
    windowed = regex_findall(regex, "n1" + "x" * (2 * MAX_REGEX_INPUT) + "n2")
    assert [m.group(1) for m in windowed.matches] == ["1", "2"]
    assert not windowed.complete


@pytest.mark.parametrize(
    ("haystack", "needle"),
    [
        ("Total USD 24.00", "$24"),
        ("Total 24 USD", "$24"),
        ("Total $ 24", "$24"),
        ("Total $\N{NO-BREAK SPACE}24", "$24"),
        ("Total US$24", "$24.00"),
        ("Total 24,00 $", "$24"),
        ("Gesamt 1.249,50 €", "€1,249.50"),
        ("Total EUR 1249.50", "€1.249,50"),
        ("Total \N{INDIAN RUPEE SIGN}1,500", "Rs. 1500"),
        ("Price $1.500", "$1.500"),
    ],
)
def test_an_amount_is_found_however_the_page_writes_it(haystack: str, needle: str) -> None:
    assert contains(haystack, needle)


@pytest.mark.parametrize(
    ("haystack", "needle"),
    [
        ("Total €24", "$24"),
        ("Total $24.50", "$24"),
        ("Total $1 024", "$24"),
        ("Total $24,000", "$24"),
        ("Total $1,500", "$1.500"),
        ("open 24 hours", "$24"),
        ("ref pay24 usd", "$24"),
        ("area $2\N{SUPERSCRIPT FOUR}", "$24"),
    ],
)
def test_another_amount_or_currency_is_not_the_amount(haystack: str, needle: str) -> None:
    assert not contains(haystack, needle)


@pytest.mark.parametrize(
    ("haystack", "needle", "found"),
    [
        ("Total $24 100 items in stock", "$24", False),
        ("Total $24 100 items in stock", "$24,100", False),
        ("Total $24 100 items in stock", "$24 100", True),
        ("Total 24 100,00 €", "€24,100", True),
        ("Total $1 024 100", "$1,024,100", True),
        ("Total $24 100.50", "$24,100.50", True),
    ],
)
def test_one_space_between_digit_groups_is_neither_one_amount_nor_two(
    haystack: str, needle: str, found: bool
) -> None:
    assert contains(haystack, needle) is found


@pytest.mark.parametrize(
    ("body", "phrase"),
    [
        ('{"order_id": "1001", "total": 29.5}', "$29.50"),
        ('{"grand_total": 1249.5, "currency": "EUR"}', "€1.249,50"),
        ('{"totalPrice": 29.5}', "$29.50"),
        ('{"amount_cents": 2950, "currency": "usd"}', "$29.50"),
        ('{"order": {"currency": "GBP", "lines": [{"unit_price": 4}]}}', "£4.00"),
        ('[{"subtotal": 24}]', "24 USD"),
    ],
)
def test_a_json_answer_shows_a_price_as_a_bare_number(body: str, phrase: str) -> None:
    assert shows_amount(body, phrase)


@pytest.mark.parametrize(
    ("body", "phrase"),
    [
        ('{"total": 29.5, "currency": "EUR"}', "$29.50"),
        ('{"total": 2950}', "$29.50"),
        ('{"total_cents": 2950.5}', "$29.50"),
        ('{"amount_cents": 4}', "$4.00"),
        ('{"cents": 400}', "$4.00"),
        ('{"order_id": 4, "qty": 4, "item_count": 4, "total_items": 4}', "$4.00"),
        ('{"price_id": 4, "total": true}', "$4.00"),
        ('{"total": "29.50"}', "$29.50"),
        ("<p>Total: 29.50</p>", "$29.50"),
        ('{"total": 29.5}', "29.50"),
        ('{"total": 24100}', "$24 100"),
    ],
)
def test_no_other_number_or_currency_shows_the_price(body: str, phrase: str) -> None:
    assert not shows_amount(body, phrase)
