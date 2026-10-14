from __future__ import annotations

import json

import pytest

from evidence_judge.redact import Redactor

KEY = b"unit-test-key"


@pytest.fixture
def r() -> Redactor:
    return Redactor(KEY)


def red(r: Redactor, text: str) -> str:
    return r.redact_text(text)[0]


SECRET_FIELDS = (
    "cvv",
    "cvc",
    "cvv2",
    "pin",
    "otp",
    "otp_code",
    "pass",
    "pw",
    "passcode",
    "security_code",
    "securityCode",
    "card[cvc]",
    "ssn",
)


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_secret_json_string_fields(r: Redactor, field: str) -> None:
    out = red(r, json.dumps({field: "8271", "item": "kettle"}))
    assert "8271" not in out
    assert '"item": "kettle"' in out
    assert json.loads(out)[field].startswith("[REDACTED:")


@pytest.mark.parametrize("field", ["cvv", "pin", "otp", "security_code"])
def test_secret_json_number_fields_stay_valid_json(r: Redactor, field: str) -> None:
    out = red(r, json.dumps({field: 8271, "qty": 2}))
    assert "8271" not in out
    data = json.loads(out)
    assert data["qty"] == 2
    assert data[field].startswith("[REDACTED:secret_param:")


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_secret_form_fields(r: Redactor, field: str) -> None:
    out = red(r, f"{field}=8271&item=kettle")
    assert "8271" not in out
    assert "item=kettle" in out


@pytest.mark.parametrize("field", ["cvv", "pin", "otp_code", "pw", "security_code"])
def test_secret_query_params(r: Redactor, field: str) -> None:
    out, _ = r.redact_url(f"https://shop.test/pay?{field}=8271&item=kettle")
    assert "8271" not in out
    assert "item=kettle" in out


@pytest.mark.parametrize(
    "text", ['{"shipping": "express"}', "spinner=on", '{"passenger": "Ann Lee"}', "pinned=1"]
)
def test_similar_field_names_are_left_alone(r: Redactor, text: str) -> None:
    assert red(r, text) == text


def test_jsessionid_path_parameter(r: Redactor) -> None:
    out, report = r.redact_url("https://shop.test/cart;jsessionid=A1B2C3D4E5F6?item=kettle")
    assert "A1B2C3D4E5F6" not in out
    assert out.startswith("https://shop.test/cart;jsessionid=[REDACTED:")
    assert out.endswith("?item=kettle")
    assert report.total == 1


@pytest.mark.parametrize(
    "text",
    [
        "SSN: 123-45-6789",
        "social security number 123-45-6789",
        '{"ssn": "123-45-6789"}',
        "ssn=123-45-6789",
    ],
)
def test_us_ssn_with_context(r: Redactor, text: str) -> None:
    out = red(r, text)
    assert "123-45-6789" not in out
    assert "[REDACTED:" in out


@pytest.mark.parametrize(
    "text",
    ["order 123-45-6789 shipped", "part number 123-45-6789", "SSN: 000-45-6789"],
)
def test_ssn_shaped_numbers_without_context_are_kept(r: Redactor, text: str) -> None:
    assert red(r, text) == text


@pytest.mark.parametrize(
    "text",
    [
        '{"phone": "+1 (415) 555-0134"}',
        '{"phoneNumber": "415-555-0134"}',
        '{"mobile": "4155550134"}',
        "telephone=%2B14155550134",
        "tel=415.555.0134",
    ],
)
def test_phone_numbers_in_phone_fields(r: Redactor, text: str) -> None:
    out = red(r, text)
    for fragment in ("555-0134", "5550134", "555.0134"):
        assert fragment not in out
    assert "[REDACTED:phone:" in out


@pytest.mark.parametrize(
    "text",
    [
        "Call us at 415-555-0134",
        '{"order_id": "4155550134"}',
        '{"total": "415.55"}',
        "Order #415-555-0134 confirmed",
    ],
)
def test_phone_shaped_numbers_elsewhere_are_kept(r: Redactor, text: str) -> None:
    assert red(r, text) == text


def test_redaction_is_idempotent_for_new_categories(r: Redactor) -> None:
    text = '{"cvv": 123, "phone": "415-555-0134"} SSN 123-45-6789 /a;jsessionid=XYZ123'
    once = red(r, text)
    assert red(r, once) == once
