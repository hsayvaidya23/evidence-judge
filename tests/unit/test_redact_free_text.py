from __future__ import annotations

import json

import pytest

from evidence_judge.core.evidence import Channel, ConsoleEvent, PageSnapshot, Run
from evidence_judge.redact import Redactor

KEY = b"unit-test-key"


@pytest.fixture
def r() -> Redactor:
    return Redactor(KEY)


def red(r: Redactor, text: str) -> str:
    return r.redact_text(text)[0]


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (
            "Redirected to https://shop.test/cb?code=oauthcode123&state=s1 after login",
            "oauthcode123",
        ),
        ('{"next": "https://shop.test/cb?code=oauthcode123&state=s1"}', "oauthcode123"),
        ('{"next":"https:\\/\\/shop.test\\/cb?code=oauthcode123&state=s1"}', "oauthcode123"),
        ("GET https://cdn.test/f.pdf?X-Amz-Signature=abcdef0123456789 failed", "abcdef0123456789"),
        ("open https://shop.test/v?sid=s3ss10n&lang=en.", "s3ss10n"),
        ("(see https://files.test/a?sig=Zm9vYmFy&se=2026)", "Zm9vYmFy"),
        ("link: https://api.test/x?signature=deadbeef99", "deadbeef99"),
        ("key in https://maps.test/js?key=k3yv4lu3&v=3", "k3yv4lu3"),
        ("https://x.test/?token=t0k3nv4lu3", "t0k3nv4lu3"),
    ],
)
def test_urls_in_free_text_get_url_redaction(r: Redactor, text: str, secret: str) -> None:
    out = red(r, text)
    assert secret not in out
    assert "[REDACTED:secret_param:" in out


def test_url_in_text_matches_the_same_url_redacted_as_a_url(r: Redactor) -> None:
    url = "https://shop.test/cb?code=oauthcode123&state=s1"
    as_url = r.redact_url(url)[0]
    assert red(r, f"Went to {url}.") == f"Went to {as_url}."
    assert "state=s1" in as_url


def test_escaped_url_keeps_its_escaping(r: Redactor) -> None:
    out = red(r, '{"u":"https:\\/\\/a.test\\/cb?code=c0d3&x=1"}')
    assert out.startswith('{"u":"https:\\/\\/a.test\\/cb?code=[REDACTED:secret_param:')
    assert out.endswith('&x=1"}')
    assert json.loads(out)["u"].startswith("https://a.test/cb?code=[REDACTED:")


@pytest.mark.parametrize(
    "text",
    [
        '{"cvn": "123"}',
        '{"twoFactorCode": "123456"}',
        '{"two_factor_code": "123456"}',
        '{"totp": "123456"}',
        '{"mfa": "123456"}',
        '{"recovery_code": "abcd-efgh"}',
        "recoveryCode=abcd-efgh",
        "cvn=123",
    ],
)
def test_more_secret_field_names(r: Redactor, text: str) -> None:
    out = red(r, text)
    assert "[REDACTED:secret_param:" in out
    for secret in ("123456", "abcd-efgh", '"123"', "=123"):
        assert secret not in out


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("password: hunter2", "hunter2"),
        ("Password = hunter2", "hunter2"),
        ("password=hunter2", "hunter2"),
        ("login ok, password: 'hunter2!' used", "hunter2!"),
        ("api_key: ab12cd34ef", "ab12cd34ef"),
        ("OTP: 482913.", "482913"),
        ("new_password:s3cr3t-pa55", "s3cr3t-pa55"),
    ],
)
def test_free_text_secret_pairs(r: Redactor, text: str, secret: str) -> None:
    out = red(r, text)
    assert secret not in out
    assert "[REDACTED:secret_param:" in out


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("sent header authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("curl -H 'Authorization: Bearer abc.def-ghi_jkl'", "abc.def-ghi_jkl"),
        ("Authorization: Token 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
        ('{"Authorization": "Basic dXNlcjpwYXNz"}', "dXNlcjpwYXNz"),
    ],
)
def test_authorization_credentials_in_free_text(r: Redactor, text: str, secret: str) -> None:
    out = red(r, text)
    assert secret not in out
    assert "[REDACTED:" in out


def test_basic_credentials_match_the_redacted_header(r: Redactor) -> None:
    header = r.redact_headers({"Authorization": "Basic dXNlcjpwYXNz"})[0]["Authorization"]
    assert red(r, "Authorization: Basic dXNlcjpwYXNz") == f"Authorization: {header}"


@pytest.mark.parametrize(
    "text",
    [
        "password reset link sent",
        "We sent a password reset link to your inbox. The token expires in 1 hour.",
        "Password: required",
        "Password: must be at least 8 characters",
        "Your basic plan renews monthly; basic understanding required.",
        "Authorization: pending review",
        "The bearer of this card",
        "Read https://docs.test/guide?section=pricing&lang=en for details.",
        "Boarding pass: ABC123",
        "Order #1042 confirmed. Tracking code: 1Z999AA10123456784",
    ],
)
def test_prose_is_left_alone(r: Redactor, text: str) -> None:
    assert red(r, text) == text


def test_run_texts_get_url_and_pair_redaction(r: Redactor) -> None:
    run = Run(
        run_id="r",
        source="test",
        agent_answer="Done: https://shop.test/cb?code=oauthcode123 (password: hunter2)",
        agent_reasoning=["Logged in with password=hunter2"],
        snapshots=[
            PageSnapshot(
                id="snap#1",
                ts=0,
                url="https://shop.test/",
                text="Reset via https://shop.test/r?token=t0k3nv4lu3",
            )
        ],
        console=[
            ConsoleEvent(id="con#1", ts=0, level="log", text="Authorization: Basic dXNlcjpwYXNz")
        ],
        captured={Channel.SNAPSHOTS, Channel.CONSOLE},
    )

    redacted, report = r.redact_run(run)

    dumped = redacted.model_dump_json()
    for secret in ("oauthcode123", "hunter2", "t0k3nv4lu3", "dXNlcjpwYXNz"):
        assert secret not in dumped
    assert report.total >= 5


def test_redaction_is_idempotent(r: Redactor) -> None:
    text = (
        "https://a.test/cb?code=c0d3&sig=s1g password: hunter2 "
        "Authorization: Basic dXNlcjpwYXNz cvn=123"
    )
    once = red(r, text)
    assert red(r, once) == once


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Logged in with password hunter2 and it worked", "hunter2"),
        ("Password hunter2.", "hunter2"),
        ("the password is hunter2", "hunter2"),
        ("passwd s3cr3t-pa55", "s3cr3t-pa55"),
        ("pwd Tr0ub4dor", "Tr0ub4dor"),
        ("passcode 482913", "482913"),
        ("token 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
        ("api key AB12cd34ef", "AB12cd34ef"),
        ("API_KEY was ab12cd34ef", "ab12cd34ef"),
        ("client secret xYzAbCdEf", "xYzAbCdEf"),
        ("my passphrase correcthorsebatterystaple", "correcthorsebatterystaple"),
    ],
)
def test_labelled_secrets_without_a_separator(r: Redactor, text: str, secret: str) -> None:
    out = red(r, text)
    assert secret not in out
    assert "[REDACTED:secret_param:" in out


def test_a_labelled_secret_matches_the_same_secret_in_a_form(r: Redactor) -> None:
    form = red(r, "password=hunter2")
    assert red(r, "used password hunter2") == "used " + form.replace("=", " ", 1)


@pytest.mark.parametrize(
    "text",
    [
        "the password field was empty",
        "reset your password",
        "Forgot your password? Reset password here.",
        "Use a password manager; the token expired and the secret ingredient is love.",
        "The token EXPIRED at noon.",
        "This page is password-protected.",
        "The secret (hidden) menu",
        "password 1234",
        "Ran pwd /home/agent/repo and pwd ~/work",
        "Paste the token https://docs.test/tokens into the field",
        "Password\n••••••••",
        "Set-Cookie: banner dismissed",
        "Cookie: accepted",
        "Cookie preferences: necessary only",
        "Authorization: Basic required",
    ],
)
def test_prose_around_secret_words_is_left_alone(r: Redactor, text: str) -> None:
    assert red(r, text) == text


@pytest.mark.parametrize(
    ("text", "secrets"),
    [
        ("The response had Cookie: sid=abc123; theme=dark, then", ("abc123", "dark")),
        ("console: Set-Cookie: sid=abc123; Path=/; HttpOnly", ("abc123",)),
        ('{"set-cookie": "sid=abc123; Path=/"}', ("abc123",)),
        ("cookie2: token=zz99", ("zz99",)),
    ],
)
def test_cookie_lines_in_free_text(r: Redactor, text: str, secrets: tuple[str, ...]) -> None:
    out = red(r, text)
    assert "[REDACTED:cookie:" in out
    for secret in secrets:
        assert secret not in out


def test_cookie_lines_in_text_match_the_redacted_headers(r: Redactor) -> None:
    headers = r.redact_headers(
        {"Cookie": "sid=abc123; theme=dark", "Set-Cookie": "sid=abc123; Path=/; HttpOnly"}
    )[0]
    assert red(r, "Cookie: sid=abc123; theme=dark") == f"Cookie: {headers['Cookie']}"
    set_cookie = red(r, "Set-Cookie: sid=abc123; Path=/; HttpOnly")
    assert set_cookie == f"Set-Cookie: {headers['Set-Cookie']}"
    assert set_cookie.endswith("; Path=/; HttpOnly")


def test_authorization_with_a_scheme_redacts_mixed_case_credentials(r: Redactor) -> None:
    out = red(r, "Authorization: Basic Zm9vYmFy")
    assert "Zm9vYmFy" not in out
    assert out.startswith("Authorization: Basic [REDACTED:auth:")


def test_new_free_text_rules_cover_every_run_text(r: Redactor) -> None:
    run = Run(
        run_id="r",
        source="test",
        agent_answer="Signed in with password hunter2; server sent Cookie: sid=s3ss10n",
        agent_reasoning=["The api key is AB12cd34ef"],
        snapshots=[
            PageSnapshot(id="snap#1", ts=0, url="https://shop.test/", text="token 9f8e7d6c5b4a")
        ],
        console=[ConsoleEvent(id="con#1", ts=0, level="log", text="Set-Cookie: auth=t0k3n42")],
        captured={Channel.SNAPSHOTS, Channel.CONSOLE},
    )

    dumped = r.redact_run(run)[0].model_dump_json()

    for secret in ("hunter2", "s3ss10n", "AB12cd34ef", "9f8e7d6c5b4a", "t0k3n42"):
        assert secret not in dumped


def test_new_free_text_rules_are_idempotent(r: Redactor) -> None:
    once = red(r, "password hunter2, Cookie: sid=abc123; x=1 and token 9f8e7d6c5b4a")
    assert red(r, once) == once


@pytest.mark.parametrize(
    "text",
    [
        "<INPUT TYPE=PASSWORD VALUE=x>",
        '<input type=password name=pw value="">',
        "token type=bearer",
    ],
)
def test_attribute_syntax_after_a_secret_word_is_not_a_labelled_secret(
    r: Redactor, text: str
) -> None:
    assert "[REDACTED:secret_param:" not in red(r, text)
