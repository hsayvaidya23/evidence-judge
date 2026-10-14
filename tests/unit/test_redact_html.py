from __future__ import annotations

import pytest

from evidence_judge.core.evidence import NetworkEvent, PageSnapshot, Run
from evidence_judge.redact import TOKEN_RE, Redactor

SECRET = "hunter2-Correct-Horse"


@pytest.fixture
def r() -> Redactor:
    return Redactor(b"unit-test-key")


def redact(r: Redactor, html: str) -> tuple[str, dict[str, int]]:
    out, report = r.redact_text(html)
    return out, report.counts


@pytest.mark.parametrize(
    "html",
    [
        f'<input type="password" name="pw" value="{SECRET}">',
        f"<input value='{SECRET}' type='password'>",
        f"<INPUT TYPE=PASSWORD VALUE={SECRET}>",
        f'<input type="hidden" name="csrf_token" value="{SECRET}">',
        f'<input type="hidden" name="authenticity_token" value="{SECRET}"/>',
        f'<input id="client-secret" value="{SECRET}">',
        f'<input name="user[password]" value="{SECRET}">',
        f'<input name="otp" value="{SECRET}">',
        f'<input name="otp_code" value="{SECRET}">',
        f'<input autocomplete="one-time-code" value="{SECRET}">',
        f'<input autocomplete="current-password" name="x" value="{SECRET}">',
        f'<input\n  type="password"\n  value="{SECRET}"\n>',
        f'<input data-x="a>b" type="password" value="{SECRET}">',
    ],
)
def test_secret_input_values_are_redacted(r: Redactor, html: str) -> None:
    out, counts = redact(r, html)

    assert SECRET not in out
    assert counts == {"html_secret": 1}
    assert TOKEN_RE.search(out)


@pytest.mark.parametrize(
    "html",
    [
        f'<input type="text" name="search" value="{SECRET}">',
        f'<input name="footprint" value="{SECRET}">',
        f'<input name="shipping" value="{SECRET}">',
        f'<input name="top_pick" value="{SECRET}">',
        f'<p>type="password" value="{SECRET}"</p>',
        f'<button type="submit" name="password" value="{SECRET}">Go</button>',
    ],
)
def test_other_values_are_kept(r: Redactor, html: str) -> None:
    out, counts = redact(r, html)

    assert out == html
    assert counts == {}


def test_tag_shape_is_kept(r: Redactor) -> None:
    out, _ = redact(r, f'<input type="password" name="pw" value="{SECRET}" required>')

    token = r.fingerprint(SECRET)
    assert (
        out == f'<input type="password" name="pw" value="[REDACTED:html_secret:{token}]" required>'
    )


def test_unquoted_value_becomes_quoted(r: Redactor) -> None:
    out, _ = redact(r, f"<input type=password value={SECRET} required>")

    assert out == (
        f'<input type=password value="[REDACTED:html_secret:{r.fingerprint(SECRET)}]" required>'
    )


def test_empty_value_is_left_alone(r: Redactor) -> None:
    html = '<input type="password" value="">'

    assert redact(r, html) == (html, {})


def test_idempotent(r: Redactor) -> None:
    once, _ = redact(r, f'<input type="password" value="{SECRET}">')

    assert redact(r, once) == (once, {})


def test_snapshot_html_and_html_bodies_are_redacted(r: Redactor) -> None:
    page = f'<form><input type="password" name="password" value="{SECRET}"></form>'
    run = Run(
        run_id="r",
        source="t",
        snapshots=[PageSnapshot(id="snap#1", ts=0, url="https://shop.test/login", html=page)],
        network=[
            NetworkEvent(
                id="net#1",
                ts=0,
                method="GET",
                url="https://shop.test/login",
                response_body=page,
                mime_type="text/html",
            )
        ],
    )

    out, report = r.redact_run(run)

    assert out.snapshots[0].html is not None and SECRET not in out.snapshots[0].html
    assert out.network[0].response_body is not None and SECRET not in out.network[0].response_body
    assert report.counts == {"html_secret": 2}


def test_large_html_is_redacted_quickly(r: Redactor) -> None:
    import time

    html = '<div class="row"><input type="text" name="q" value="x"></div>' * 20_000
    html += f'<input type="password" value="{SECRET}">' + "<" * 50_000

    start = time.perf_counter()
    out, counts = redact(r, html)

    assert time.perf_counter() - start < 2.0
    assert SECRET not in out and counts == {"html_secret": 1}
