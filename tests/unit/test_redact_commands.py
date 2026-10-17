from __future__ import annotations

import random
import re

import pytest

from evidence_judge.core.verdict import Support
from evidence_judge.redact import Redactor
from evidence_judge.redact.commands import (
    MAX_COMMAND_WORDS,
    WINDOW_OVERLAP,
    command_secrets,
    is_secret_long_flag,
)
from evidence_judge.sessions.checks import SessionIndex, check
from evidence_judge.sessions.predicates import CommandPredicate
from evidence_judge.sessions.redaction import redact_session
from tests.unit.sessions_helpers import bash, session
from tests.unit.test_perf_ingest import within

KEY = b"unit-test-key"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
NPM_TOKEN = "npm_" + "a1B2c3D4e5" * 3 + "f6G7h8"


@pytest.fixture
def r() -> Redactor:
    return Redactor(KEY)


def red(r: Redactor, text: str) -> str:
    return r.redact_text(text)[0]


@pytest.mark.parametrize(
    ("text", "secret", "kept"),
    [
        ("mysql -pS3cretPass9 -u root shop", "S3cretPass9", "mysql -p[REDACTED:"),
        ("/usr/bin/mysqldump -p'S3cret Pass9' shop", "S3cret Pass9", "mysqldump -p[REDACTED:"),
        ("mysql --password=S3cretPass9 shop", "S3cretPass9", "--password=[REDACTED:"),
        ("curl -u alice:S3cretPass9 https://api.test/x", "S3cretPass9", "-u alice:[REDACTED:"),
        ("curl -ualice:S3cretPass9 https://api.test/x", "S3cretPass9", "-ualice:[REDACTED:"),
        ("curl --user 'alice:S3cret Pass9' https://api.test", "S3cret Pass9", "alice:[REDACTED:"),
        ("curl --user=alice:S3cretPass9 https://api.test", "S3cretPass9", "=alice:[REDACTED:"),
        ("curl --proxy-user bob:Pr0xyPass https://api.test", "Pr0xyPass", "bob:[REDACTED:"),
        ("sshpass -p S3cretPass9 ssh deploy@host", "S3cretPass9", "sshpass -p [REDACTED:"),
        ("sshpass -pS3cretPass9 ssh host", "S3cretPass9", "sshpass -p[REDACTED:"),
        ("docker login -u bob -p S3cretPass9 reg.test", "S3cretPass9", "-u bob -p [REDACTED:"),
        ("docker login --password S3cretPass9 reg.test", "S3cretPass9", "--password [REDACTED:"),
        ("podman login -pS3cretPass9 reg.test", "S3cretPass9", "-p[REDACTED:"),
        ("htpasswd -b .htpasswd alice S3cretPass9", "S3cretPass9", ".htpasswd alice [REDACTED:"),
        ("htpasswd -nbB alice S3cretPass9", "S3cretPass9", "-nbB alice [REDACTED:"),
        ("htpasswd -C 12 -bc .htpasswd alice hunter", "hunter", "alice [REDACTED:"),
        (
            f"npm config set //registry.npmjs.org/:_authToken {NPM_TOKEN}",
            NPM_TOKEN,
            ":_authToken [REDACTED:api_key:",
        ),
        ("npm config set //registry.test/:_auth YWxpY2U6cHc=", "YWxpY2U6cHc=", "_auth [REDACTED:"),
        ("yarn config set npmAuthToken s3cr3t-t0k", "s3cr3t-t0k", "npmAuthToken [REDACTED:"),
        (
            f"aws configure set aws_secret_access_key {AWS_SECRET}",
            AWS_SECRET,
            "aws_secret_access_key [REDACTED:",
        ),
        ("aws configure set aws_session_token FwoGZXIvYXdzE", "FwoGZXIvYXdz", "token [REDACTED:"),
        ("PGPASSWORD=S3cretPass9 psql -h db", "S3cretPass9", "PGPASSWORD=[REDACTED:"),
        ("deploy --token=abcdef123456", "abcdef123456", "--token=[REDACTED:"),
        ("deploy --token abcdef123456 --env prod", "abcdef123456", "--token [REDACTED:"),
        ("vault login --api-key 'k3y with space'", "k3y with space", "--api-key [REDACTED:"),
        ("gh secret set X --client-secret s3cr3tval", "s3cr3tval", "--client-secret [REDACTED:"),
        ("cd app && mysql --password db && run --password Hunter22", "Hunter22", "run --pass"),
        ("curl \\\n  -u alice:Hunter22 https://api.test", "Hunter22", "-u alice:[REDACTED:"),
        ("curl -u \\\n alice:Hunter22 https://api.test", "Hunter22", "alice:[REDACTED:"),
        ("curl \\\r\n -u alice:Hunter22 x\r\n", "Hunter22", "-u alice:[REDACTED:"),
        ("docker login \\\n  --password Hunter22 reg", "Hunter22", "--password [REDACTED:"),
        ("cd app &&\n  curl -u alice:Hunter22 x", "Hunter22", "-u alice:[REDACTED:"),
        ("curl -u alice:Hunter22 x |\n  grep ok", "Hunter22", "-u alice:[REDACTED:"),
        ('curl -u "alice:s3cr3t', "s3cr3t", '-u "alice:[REDACTED:'),
        ("curl -u alice:'unbal https://api.test", "unbal", "-u alice:[REDACTED:"),
        ("curl -u 'alice:s3cr3t pass\nnext", "s3cr3t", "[REDACTED:secret_param:"),
        ('deploy --token "s3cr3t', "s3cr3t", "--token [REDACTED:"),
        ("mysql \\\n --password db && run --password Hunter22", "Hunter22", "run --pass"),
    ],
)
def test_credentials_on_a_command_line_are_redacted(
    r: Redactor, text: str, secret: str, kept: str
) -> None:
    out = red(r, text)
    assert secret not in out
    assert kept in out


def test_the_npm_token_prefix_is_an_api_key(r: Redactor) -> None:
    out = red(r, f"echo {NPM_TOKEN} > .npmrc")
    assert out.startswith("echo [REDACTED:api_key:")


def test_a_url_credential_in_a_command_is_redacted(r: Redactor) -> None:
    out = red(r, "git clone https://alice:ghtok3n99@github.com/a/b.git")
    assert "ghtok3n99" not in out
    assert "alice:[REDACTED:url_password:" in out


@pytest.mark.parametrize(
    "text",
    [
        "mysql -uroot -p shop",
        "mysql -p S3cretPass9",
        "mysql --password mydb",
        "mysql -P 3306 -h db shop",
        "curl -u alice https://api.test/x",
        "curl -u $USER:$PASS https://api.test/x",
        "curl -u alice:$PASS https://api.test/x",
        "docker run -p 8080:80 nginx",
        "docker compose -p proj up",
        "docker login --password-stdin reg.test",
        "docker login -u bob reg.test",
        "htpasswd -c .htpasswd alice",
        "aws configure set region us-east-1",
        "aws s3 ls --profile prod",
        "npm config set registry https://registry.test/",
        "gh auth login --with-token",
        "deploy --token $TOKEN",
        "deploy --no-password now123",
        "deploy --secret ./secret.txt",
        "deploy --token-file tok3n.txt",
        "pass --token to the cli",
        "I used curl to fetch the page and npm test passed",
        "sshpass -e ssh host",
        "curl --token tests/test_x.py::test_a https://api.test",
        "curl --password ./fixtures/pw.txt https://api.test",
        "npm test --password 2 failed",
        "pass --token it's fine",
        "curl -u alice https://api.test\r\n",
    ],
)
def test_commands_without_a_credential_are_left_alone(r: Redactor, text: str) -> None:
    assert red(r, text) == text


def test_equal_command_secrets_get_equal_tokens(r: Redactor) -> None:
    def token(text: str) -> str:
        return red(r, text).rsplit(" ", 1)[-1].split(":", 1)[-1]

    attached = red(r, "mysql -pS3cretPass9").removeprefix("mysql -p")
    assert red(r, "sshpass -p S3cretPass9").endswith(attached)
    assert red(r, "deploy --password=S3cretPass9").endswith(attached)
    assert red(r, "curl -u alice:S3cretPass9").endswith(attached)
    assert red(r, "password S3cretPass9").endswith(attached)
    assert token(f"echo {NPM_TOKEN}") == token(f"npm config set //r/:_authToken {NPM_TOKEN}")


def test_command_redaction_is_idempotent(r: Redactor) -> None:
    text = "mysql -pS3cretPass9 && curl -u a:Pw9 x && docker login -p S3cretPass9 r"
    once = red(r, text)
    assert red(r, once) == once
    assert "S3cretPass9" not in once and "Pw9" not in once


def test_the_line_after_an_unclosed_quote_is_kept(r: Redactor) -> None:
    out = red(r, "curl -u 'alice:s3cr3t pass\nnext line")
    assert out.endswith("\nnext line") and "s3cr3t" not in out


def test_words_past_one_window_are_read_against_the_tool(r: Redactor) -> None:
    flags = [f"--token Tok{i}x9Z" for i in range(MAX_COMMAND_WORDS)]
    out = red(r, "curl " + " ".join(flags))
    assert out.count("--token [REDACTED:secret_param:") == len(flags)
    assert "Tok" not in out


@pytest.mark.parametrize(
    "command",
    [
        "pytest --token tests/test_x.py::test_a",
        "npm test --password 2 failed",
        "curl --token tests/test_auth.py https://api.test",
    ],
)
def test_command_predicates_match_an_honest_command_after_redaction(command: str) -> None:
    run, _ = redact_session(Redactor(KEY), session(bash(command)))
    index = SessionIndex(run)
    assert check(CommandPredicate(contains=command), index).status is Support.SUPPORTED
    pattern = CommandPredicate(regex=re.escape(command) + "$")
    assert check(pattern, index).status is Support.SUPPORTED


@pytest.mark.perf
@pytest.mark.parametrize("tool", ["echo", "curl", "mysql"])
def test_many_secret_flags_redact_in_linear_time(r: Redactor, tool: str) -> None:
    text = tool + " " + " ".join(f"--token Tok{i}x9Z" for i in range(5000))
    with within(2.0):
        out = red(r, text)
    assert (tool == "mysql") is ("Tok1x9Z" in out)


def test_command_secret_spans_follow_the_words() -> None:
    args = " -u alice:pw1 --token t0k"
    spans = [(args[s.start : s.end], s.value, s.prefix) for s in command_secrets("curl", args)]
    assert spans == [("pw1", "pw1", ""), ("t0k", "t0k", "")]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("--password", True),
        ("--token", True),
        ("--api-key", True),
        ("--client-secret", True),
        ("--aws-secret-key", True),
        ("--password-stdin", False),
        ("--token-file", False),
        ("--no-password", False),
        ("--with-token", False),
        ("--key", False),
        ("--public-key", False),
    ],
)
def test_secret_long_flag_names(name: str, expected: bool) -> None:
    assert is_secret_long_flag(name) is expected


@pytest.mark.parametrize(
    "text",
    [
        "grep -rn token src/auth",
        "rg secret src/config.py",
        "cp token tests/test_calc.py",
        "FAILED tests/test_auth.py::test_login - assert token abc123 == None",
        "fixed token_bucket.py; token bucket2 tests now pass",
        "pytest tests/test_token.py::test_token_expired PASSED",
        "ERROR tests/test_secret.py - ImportError",
        "see tests::token_expired",
        "cargo test token::tests::expiry",
        "the token TestTokenExpiry case fails",
        "token test_refresh_flow passed",
        "token parse(raw) returns None",
        "secret load_secret(path) was removed",
        "password ./fixtures/pw.txt",
        "token ~/creds",
        "password ../secrets.env",
        "token config.yaml",
        "auth_token abc123",
        "refresh_token expired",
        "test_token.py passes",
    ],
)
def test_code_references_after_a_secret_word_are_left_alone(r: Redactor, text: str) -> None:
    assert red(r, text) == text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"aws_secret_access_key {AWS_SECRET}", AWS_SECRET),
        ("export GITHUB_TOKEN 9f8e7d6c5b4a3", "9f8e7d6c5b4a3"),
        ("my_api_key AB12cd34ef", "AB12cd34ef"),
        ("db-password hunter2", "hunter2"),
        ("token 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
        ("password hunter2", "hunter2"),
        ("password abc123", "abc123"),
        ("secret s3cr3t.v4lu3", "s3cr3t.v4lu3"),
    ],
)
def test_labels_ending_an_identifier_still_redact(r: Redactor, text: str, secret: str) -> None:
    out = red(r, text)
    assert secret not in out
    assert "[REDACTED:secret_param:" in out


SECRET = "Hunter22xx"
FORMS = [
    ("curl", "curl", "", "-u alice:" + SECRET),
    ("curl", "curl", "", "--user alice:" + SECRET),
    ("curl", "curl", "", "--password " + SECRET),
    ("mysql", "mysql", "", "-p" + SECRET),
    ("docker", "docker login", "", "-p " + SECRET),
    ("sshpass", "sshpass", "", "-p " + SECRET),
    ("htpasswd", "htpasswd -b", "-B", "f user " + SECRET),
]


def _boundaries() -> list[int]:
    step = MAX_COMMAND_WORDS - WINDOW_OVERLAP
    return sorted({n + d for n in (MAX_COMMAND_WORDS, step, 2 * step) for d in range(-3, 3)})


@pytest.mark.parametrize(("tool", "head", "filler", "pair"), FORMS)
def test_a_secret_at_any_word_of_a_long_command_is_redacted(
    r: Redactor, tool: str, head: str, filler: str, pair: str
) -> None:
    rng = random.Random(tool + pair)
    positions = [*_boundaries(), *(rng.randrange(3 * MAX_COMMAND_WORDS) for _ in range(20))]
    for at in positions:
        words = [filler or f"w{rng.randrange(10**6)}" for _ in range(at)]
        text = " ".join([head, *words, pair, filler or "tail"])
        out = red(r, text)
        assert SECRET not in out, (tool, at)
        assert out.count("[REDACTED:") == 1, (tool, at)


@pytest.mark.perf
def test_a_long_command_redacts_in_linear_time(r: Redactor) -> None:
    text = "curl " + " ".join(f"w{i}" for i in range(100_000)) + " -u alice:" + SECRET
    with within(2.0):
        out = red(r, text)
    assert SECRET not in out
