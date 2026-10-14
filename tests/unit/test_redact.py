from __future__ import annotations

import re
import time

import pytest
from pydantic import SecretStr

from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.evidence import (
    ActionEvent,
    Channel,
    ConsoleEvent,
    NetworkEvent,
    PageSnapshot,
    PageState,
    Run,
)
from evidence_judge.core.predicates import RequestPredicate
from evidence_judge.core.taskspec import Expectation, TaskSpec
from evidence_judge.redact import TOKEN_RE, RedactionReport, Redactor

KEY = b"unit-test-key"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
OPENAI = "sk-proj-abcdefghijklmnopqrstuvwxyz012345"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
AWS = "AKIAIOSFODNN7EXAMPLE"
SLACK = "xoxb-123456789012-abcdefghijkl"
EMAIL = "alice.smith+test@example.co.uk"
CARD = "4111 1111 1111 1111"
CARD_DIGITS = "4111111111111111"


@pytest.fixture
def r() -> Redactor:
    return Redactor(KEY)


def red(r: Redactor, text: str) -> str:
    return r.redact_text(text)[0]


def test_fingerprint_is_stable_8_hex_and_key_dependent(r: Redactor) -> None:
    fp = r.fingerprint("hunter2")
    assert re.fullmatch(r"[0-9a-f]{8}", fp)
    assert fp == Redactor(KEY).fingerprint("hunter2")
    assert fp != Redactor(b"other-key").fingerprint("hunter2")


def test_empty_key_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        Redactor(b"")


def test_from_settings_uses_configured_key() -> None:
    s = Settings(redaction_key=SecretStr("k1"))
    assert Redactor.from_settings(s).fingerprint("x") == Redactor(b"k1").fingerprint("x")


def test_from_settings_random_key_is_stable_per_process() -> None:
    s = Settings(redaction_key=None)
    assert Redactor.from_settings(s).fingerprint("x") == Redactor.from_settings(s).fingerprint("x")


@pytest.mark.parametrize(
    ("secret", "category"),
    [
        (JWT, "jwt"),
        (OPENAI, "api_key"),
        (GITHUB, "api_key"),
        (AWS, "api_key"),
        (SLACK, "api_key"),
        ("sk-ant-api03-" + "x" * 40, "api_key"),
        ("sk_live_" + "A" * 24, "api_key"),
        ("AIza" + "B" * 35, "api_key"),
        (EMAIL, "email"),
    ],
)
def test_text_secrets_are_replaced(r: Redactor, secret: str, category: str) -> None:
    out, report = r.redact_text(f"before {secret} after")
    assert secret not in out
    assert f"[REDACTED:{category}:{r.fingerprint(secret)}]" in out
    assert out.startswith("before ") and out.endswith(" after")
    assert report.counts == {category: 1}


def test_bearer_token_keeps_scheme(r: Redactor) -> None:
    out = red(r, "Authorization: Bearer abcdef0123456789xyz")
    assert out == f"Authorization: Bearer [REDACTED:bearer:{r.fingerprint('abcdef0123456789xyz')}]"


def test_card_numbers_luhn_validated_and_normalised(r: Redactor) -> None:
    spaced = red(r, f"card {CARD} ok")
    plain = red(r, f"card {CARD_DIGITS} ok")
    assert CARD not in spaced and CARD_DIGITS not in plain
    assert spaced == plain
    assert f"[REDACTED:card:{r.fingerprint(CARD_DIGITS)}]" in plain


@pytest.mark.parametrize(
    "text",
    [
        "order 4111111111111112 placed",
        "ts 1739462400000123",
        "id 1234567890123",
        "price 30.00",
    ],
)
def test_non_card_numbers_untouched(r: Redactor, text: str) -> None:
    assert red(r, text) == text


def test_json_body_secret_fields(r: Redactor) -> None:
    body = '{"username": "bob", "password": "hunter2", "api_key": "abc123", "key": "color"}'
    out = red(r, body)
    assert "hunter2" not in out and "abc123" not in out
    assert '"username": "bob"' in out
    assert '"key": "color"' in out
    assert f'"password": "[REDACTED:secret_param:{r.fingerprint("hunter2")}]"' in out


def test_form_body_secret_fields(r: Redactor) -> None:
    out = red(r, "user=bob&password=hunter2&csrf_token=zzz999&q=kettle")
    assert "hunter2" not in out and "zzz999" not in out
    assert "user=bob" in out and "q=kettle" in out


def test_url_query_params(r: Redactor) -> None:
    url = "https://shop.test/cb?code=AUTH123&state=xyz&key=K9&search=red+kettle&email=a%40b.io"
    out, report = r.redact_url(url)
    for secret in ("AUTH123", "K9", "a%40b.io", "a@b.io"):
        assert secret not in out
    assert "state=xyz" in out and "search=red+kettle" in out
    assert out.startswith("https://shop.test/cb?")
    assert report.counts == {"secret_param": 2, "email": 1}


def test_url_userinfo_and_fragment(r: Redactor) -> None:
    out, _ = r.redact_url("https://bob:s3cret@host.test/p#access_token=tok999&type=bearer")
    assert "s3cret" not in out and "tok999" not in out
    assert "bob:" in out and "type=bearer" in out


def test_headers(r: Redactor) -> None:
    headers = {
        "Authorization": f"Bearer {JWT}",
        "Proxy-Authorization": "Basic dXNlcjpwYXNz",
        "Cookie": "sid=abc123; theme=dark",
        "Set-Cookie": "session=xyz789; Path=/; HttpOnly",
        "X-API-Key": "plainkey",
        "X-Auth-Token": "tkn",
        "Content-Type": "application/json",
        "Referer": "https://a.test/?token=leak",
    }
    out, report = r.redact_headers(headers)
    joined = " ".join(out.values())
    for secret in (JWT, "dXNlcjpwYXNz", "abc123", "dark", "xyz789", "plainkey", "tkn", "leak"):
        assert secret not in joined
    assert out["Authorization"] == f"Bearer [REDACTED:jwt:{r.fingerprint(JWT)}]"
    assert out["Proxy-Authorization"].startswith("Basic [REDACTED:auth:")
    assert out["Cookie"].startswith("sid=[REDACTED:cookie:")
    assert "; Path=/; HttpOnly" in out["Set-Cookie"]
    assert out["Content-Type"] == "application/json"
    assert report.total == 8


def test_same_value_same_token_across_contexts(r: Redactor) -> None:
    in_body = red(r, f'{{"contact": "{EMAIL}"}}')
    in_answer = red(r, f"I emailed {EMAIL} the receipt.")
    token = f"[REDACTED:email:{r.fingerprint(EMAIL)}]"
    assert token in in_body and token in in_answer


SAMPLES = [
    f"Bearer {OPENAI} and {JWT} for {EMAIL} paying {CARD}",
    "https://x.test/a?token=abc&password=p%40ss&next=/home",
    '{"client_secret": "shh", "refresh_token": "r1", "note": "call 555-0100"}',
    f"{GITHUB} {AWS} {SLACK}",
    "plain text with nothing sensitive in it",
    "",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_idempotent(r: Redactor, text: str) -> None:
    once, _ = r.redact_text(text)
    twice, report = r.redact_text(once)
    assert twice == once
    assert report.total == 0


@pytest.mark.parametrize("text", SAMPLES)
def test_tokens_are_well_formed(r: Redactor, text: str) -> None:
    out = red(r, text)
    for m in re.finditer(r"\[REDACTED[^\]]*\]", out):
        assert TOKEN_RE.fullmatch(m.group(0))


def _run() -> Run:
    return Run(
        run_id="r1",
        source="test",
        task=f"Log in as {EMAIL}",
        agent_answer=f"Done. Used card {CARD} and key {OPENAI}.",
        agent_reasoning=[f"Typing password for {EMAIL}"],
        actions=[
            ActionEvent(id="act#1", ts=1, name="fill", selector="#password", value="hunter2"),
            ActionEvent(id="act#2", ts=2, name="fill", selector="#email", value=EMAIL),
            ActionEvent(id="act#3", ts=3, name="goto", url="https://a.test/?session=S1"),
        ],
        network=[
            NetworkEvent(
                id="net#1",
                ts=4,
                method="POST",
                url="https://a.test/api/login?api_key=K1",
                status=200,
                request_headers={"Authorization": "Bearer tok-abcdefghijk"},
                request_body=f'{{"email": "{EMAIL}", "password": "hunter2"}}',
                response_headers={"Set-Cookie": "sid=S2; Path=/"},
                response_body=f'{{"token": "{JWT}"}}',
            )
        ],
        snapshots=[PageSnapshot(id="snap#1", ts=5, url="https://a.test/me", text=f"Hi {EMAIL}")],
        console=[ConsoleEvent(id="con#1", ts=6, level="log", text=f"auth {JWT}")],
        initial_state=PageState(url="https://a.test/login?token=T0"),
        final_state=PageState(url="https://a.test/me", title=EMAIL, text=f"card {CARD_DIGITS}"),
        captured={Channel.NETWORK, Channel.ACTIONS},
        blobs={"shot1": b"\x89PNG"},
        metadata={"user": {"email": EMAIL}, "n": 3},
    )


def test_redact_run_removes_all_secrets(r: Redactor) -> None:
    run = _run()
    out, report = r.redact_run(run)
    dumped = out.model_dump_json()
    for secret in (
        EMAIL,
        CARD,
        CARD_DIGITS,
        OPENAI,
        JWT,
        "hunter2",
        "S1",
        "K1",
        "tok-abcdefghijk",
        "sid=S2",
        "T0",
    ):
        assert secret not in dumped, secret
    assert out.actions[0].value == f"[REDACTED:secret_input:{r.fingerprint('hunter2')}]"
    assert out.blobs == run.blobs
    assert out.captured == run.captured
    assert out.metadata["n"] == 3
    assert report.total > 0
    assert set(report.counts) >= {"email", "card", "api_key", "jwt", "secret_input"}


def test_redact_run_does_not_mutate_input(r: Redactor) -> None:
    run = _run()
    before = run.model_dump_json()
    r.redact_run(run)
    assert run.model_dump_json() == before


def test_redact_run_idempotent(r: Redactor) -> None:
    once, _ = r.redact_run(_run())
    twice, report = r.redact_run(once)
    assert twice.model_dump_json() == once.model_dump_json()
    assert report.total == 0


def test_report_never_contains_values(r: Redactor) -> None:
    _, report = r.redact_run(_run())
    assert isinstance(report, RedactionReport)
    text = report.model_dump_json()
    assert EMAIL not in text and "hunter2" not in text


def test_redact_claims_and_spec_match_redacted_evidence(r: Redactor) -> None:
    claims = ClaimSet(
        claims=[
            Claim(
                id="c1",
                text=f"Sent receipt to {EMAIL}",
                quote=f"I emailed {EMAIL}",
                kind=ClaimKind.ACTION,
                predicates=[RequestPredicate(method="POST", body_contains=[EMAIL])],
            )
        ]
    )
    spec = TaskSpec(
        id="s",
        task="t",
        expect=[
            Expectation(
                id="e1",
                any=[
                    RequestPredicate(
                        method="POST",
                        url_contains=["/api"],
                        url_regex=r"a@b\.c",
                        body_contains=[EMAIL],
                    )
                ],
            )
        ],
    )
    red_claims, creport = r.redact_model(claims)
    red_spec, _ = r.redact_model(spec)
    token = f"[REDACTED:email:{r.fingerprint(EMAIL)}]"
    pred = red_claims.claims[0].predicates[0]
    assert isinstance(pred, RequestPredicate)
    assert pred.body_contains == [token]
    assert pred.method == "POST"
    assert EMAIL not in red_claims.model_dump_json()
    spec_pred = red_spec.expect[0].any[0]
    assert isinstance(spec_pred, RequestPredicate)
    assert spec_pred.url_regex == r"a@b\.c"
    assert spec_pred.body_contains == [token]
    assert creport.counts["email"] == 3
    run, _ = r.redact_run(_run())
    assert run.network[0].request_body is not None
    assert token in run.network[0].request_body


@pytest.mark.parametrize(
    "variant",
    [
        "Jane.Doe@Example.com",
        "jane.doe@example.com",
        "JANE.DOE@EXAMPLE.COM",
        "jAnE.dOe@eXaMpLe.CoM",
    ],
)
def test_email_fingerprint_ignores_case(r: Redactor, variant: str) -> None:
    assert (
        red(r, f"to {variant}.") == f"to [REDACTED:email:{r.fingerprint('jane.doe@example.com')}]."
    )


def test_card_fingerprint_ignores_formatting(r: Redactor) -> None:
    assert (
        red(r, "card 4111-1111-1111-1111")
        == red(r, f"card {CARD}")
        == red(r, f"card {CARD_DIGITS}")
    )


def test_token_fingerprint_stays_case_sensitive(r: Redactor) -> None:
    upper = red(r, "token=AbCdEf123")
    lower = red(r, "token=abcdef123")
    assert upper != lower


def test_redaction_tokens_are_lowercase_hex_and_case_stable(r: Redactor) -> None:
    out = red(r, "mail Jane.Doe@Example.com")
    token = TOKEN_RE.search(out)
    assert token is not None
    assert token.group(0).split(":")[2][:-1] == token.group(0).split(":")[2][:-1].lower()
    assert red(r, out.casefold()) == out.casefold()


@pytest.mark.parametrize(
    "text",
    [
        "Order 4111111111111111 confirmed",
        "tracking 6011000990139424 shipped",
        '{"order_id": "4111111111111111", "status": "ok"}',
        "order_id=4111111111111111&tracking=6011000990139424",
        '{"payment": "visa", "order_id": "4111111111111111"}',
        "shipment ref 5555555555554444",
    ],
)
def test_bare_luhn_valid_ids_are_not_cards(r: Redactor, text: str) -> None:
    assert red(r, text) == text


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "3782 822463 10005",
        "card 4111111111111111",
        "Paid with Visa 4111111111111111.",
        "cc: 4111111111111111",
        '{"card_number": "4111111111111111"}',
        '{"pan":"4111111111111111"}',
        "ccnum=4111111111111111",
        '{"card": {"number": "4111111111111111"}}',
        "4111111111111111 (mastercard ending 1111)",
    ],
)
def test_cards_need_card_formatting_or_card_context(r: Redactor, text: str) -> None:
    out = red(r, text)
    assert "4111111111111111" not in out and "1111 1111" not in out and "822463" not in out
    assert "[REDACTED:card:" in out


def test_card_seen_with_context_is_redacted_everywhere_in_the_run(r: Redactor) -> None:
    run = Run(
        run_id="r",
        source="t",
        agent_answer="Charged 4111111111111111 successfully.",
        network=[
            NetworkEvent(
                id="net#1",
                ts=1,
                method="POST",
                url="https://shop.test/api/pay",
                request_body='{"card_number": "4111111111111111"}',
            )
        ],
        captured={Channel.NETWORK},
    )
    redacted, _ = r.redact_run(run)
    token = f"[REDACTED:card:{r.fingerprint(CARD_DIGITS)}]"
    assert redacted.agent_answer == f"Charged {token} successfully."
    claims = ClaimSet(
        claims=[
            Claim(
                id="c",
                text="charged 4111111111111111",
                quote="Charged 4111111111111111",
                kind=ClaimKind.ACTION,
                predicates=[RequestPredicate(method="POST", body_contains=["4111111111111111"])],
            )
        ]
    )
    red_claims, _ = r.redact_model(claims)
    pred = red_claims.claims[0].predicates[0]
    assert isinstance(pred, RequestPredicate)
    assert pred.body_contains == [token]
    assert red(Redactor(KEY), "Charged 4111111111111111") == "Charged 4111111111111111"


def test_percent_encoded_email_in_form_body(r: Redactor) -> None:
    out = red(r, "email=jane.doe%40example.com&name=Jane+Doe&card=4242424242424242")
    token = f"[REDACTED:email:{r.fingerprint('jane.doe@example.com')}]"
    assert out.startswith(f"email={token}&name=Jane+Doe&card=[REDACTED:card:")
    assert "%40" not in out and "jane" not in out


def test_percent_encoded_email_with_plus_in_form_body(r: Redactor) -> None:
    out = red(r, "to=Jane+Doe+%3Cjane%40example.com%3E&x=1")
    assert "jane" not in out
    assert f"[REDACTED:email:{r.fingerprint('jane@example.com')}]" in out
    assert out.endswith("&x=1")


def test_percent_encoded_email_in_url_path_and_query(r: Redactor) -> None:
    out, report = r.redact_url(
        "https://shop.test/users/Jane.Doe%40Example.com/orders?to=jane.doe%2540example.com#x"
    )
    token = f"[REDACTED:email:{r.fingerprint('jane.doe@example.com')}]"
    assert out == f"https://shop.test/users/{token}/orders?to={token}#x"
    assert report.counts == {"email": 2}


def test_percent_encoded_email_in_free_text(r: Redactor) -> None:
    out = red(r, "redirect to /u/jane.doe%40example.com/profile now")
    token = f"[REDACTED:email:{r.fingerprint('jane.doe@example.com')}]"
    assert out == f"redirect to /u/{token}/profile now"


@pytest.mark.parametrize("text", ["50%25 off", "a%20b%20c", "/p/red%20kettle?q=a%2Fb"])
def test_percent_encoding_without_pii_is_left_alone(r: Redactor, text: str) -> None:
    assert red(r, text) == text


def test_percent_scan_is_linear_on_long_unescaped_runs(r: Redactor) -> None:
    text = "a" * 200_000 + "%zz"
    started = time.perf_counter()
    assert red(r, text) == text
    assert time.perf_counter() - started < 1.0
