from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.core.evidence import Channel
from evidence_judge.ingest import HarFormatError, load_har
from evidence_judge.ingest._network import BodyPrefix, decode_body, headers_dict

RUNS = Path(__file__).resolve().parent.parent / "fixtures" / "runs"


def entry(
    method: str = "GET",
    url: str = "https://shop.test/",
    *,
    started: str = "2026-10-14T10:00:00.000Z",
    status: int = 200,
    content: dict[str, Any] | None = None,
    post_data: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "method": method,
        "url": url,
        "httpVersion": "HTTP/1.1",
        "headers": [{"name": "Accept", "value": "*/*"}],
    }
    if post_data is not None:
        request["postData"] = post_data
    return {
        "startedDateTime": started,
        "time": 1.5,
        "request": request,
        "response": {
            "status": status,
            "headers": [{"name": "Content-Type", "value": "text/plain"}],
            "content": content or {"size": 0, "mimeType": "text/plain"},
        },
        **extra,
    }


def write_har(
    path: Path, *entries: dict[str, Any], pages: list[dict[str, Any]] | None = None
) -> Path:
    log = {"version": "1.2", "creator": {"name": "test", "version": "1"}, "entries": list(entries)}
    if pages is not None:
        log["pages"] = pages
    path.write_text(json.dumps({"log": log}))
    return path


def test_recorded_har_has_the_order_request() -> None:
    run = load_har(RUNS / "honest_success" / "run.har")

    assert run.source == "har"
    assert run.captured == {Channel.NETWORK}
    assert run.metadata["har_version"] == "1.2"
    assert run.metadata["creator"].startswith("Playwright ")
    orders = [n for n in run.network if n.method == "POST" and n.url.endswith("/api/orders")]
    assert [(n.status, n.resource_type) for n in orders] == [(201, "fetch")]
    assert orders[0].request_body is not None and "red-kettle" in orders[0].request_body
    assert [n.id for n in run.network] == [f"net#{i}" for i in range(1, len(run.network) + 1)]
    times = [n.ts for n in run.network]
    assert times == sorted(times) and times[0] >= 0


def test_recorded_har_of_forged_run_has_no_order() -> None:
    run = load_har(RUNS / "forged_success" / "run.har")

    assert run.network
    assert not [n for n in run.network if "/api/orders" in n.url]


def test_timestamps_are_relative_to_the_earliest_page(tmp_path: Path) -> None:
    har = write_har(
        tmp_path / "x.har",
        entry(url="https://shop.test/b", started="2026-10-14T10:00:02.500Z"),
        entry(url="https://shop.test/a", started="2026-10-14T10:00:01.000Z"),
        pages=[{"id": "page_1", "startedDateTime": "2026-10-14T10:00:00.000Z"}],
    )

    run = load_har(har)
    assert [(n.url, n.ts) for n in run.network] == [
        ("https://shop.test/a", 1000.0),
        ("https://shop.test/b", 2500.0),
    ]


def test_base64_text_bodies_are_decoded(tmp_path: Path) -> None:
    body = json.dumps({"order_id": 1001})
    har = write_har(
        tmp_path / "x.har",
        entry(
            content={
                "mimeType": "application/json",
                "text": base64.b64encode(body.encode()).decode(),
                "encoding": "base64",
            }
        ),
    )

    assert load_har(har).network[0].response_body == body


def test_binary_bodies_are_skipped(tmp_path: Path) -> None:
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00").decode()
    har = write_har(
        tmp_path / "x.har",
        entry(content={"mimeType": "image/png", "text": png, "encoding": "base64"}),
        entry(content={"mimeType": "", "text": png, "encoding": "base64"}),
    )

    assert [n.response_body for n in load_har(har).network] == [None, None]


def test_form_params_become_the_request_body(tmp_path: Path) -> None:
    har = write_har(
        tmp_path / "x.har",
        entry(
            "POST",
            post_data={
                "mimeType": "application/x-www-form-urlencoded",
                "params": [{"name": "email", "value": "a@b.c"}, {"name": "q", "value": "x y"}],
            },
        ),
    )

    assert load_har(har).network[0].request_body == "email=a%40b.c&q=x+y"


def test_failed_requests(tmp_path: Path) -> None:
    har = write_har(
        tmp_path / "x.har",
        entry(status=-1),
        entry(status=0),
        entry(status=500),
    )

    network = load_har(har).network
    assert [(n.status, n.failed, n.ok) for n in network] == [
        (None, True, False),
        (None, True, False),
        (500, False, False),
    ]


def test_attached_body_files_resolve_next_to_the_har(tmp_path: Path) -> None:
    (tmp_path / "bodies").mkdir()
    (tmp_path / "bodies" / "a.json").write_text('{"ok": true}')
    har = write_har(
        tmp_path / "x.har",
        entry(content={"mimeType": "application/json", "_file": "bodies/a.json"}),
        entry(content={"mimeType": "application/json", "_file": "../outside.json"}),
        entry(content={"mimeType": "application/json", "_file": "bodies/missing.json"}),
    )

    run = load_har(har)
    assert [n.response_body for n in run.network] == ['{"ok": true}', None, None]
    assert len(run.metadata["warnings"]) == 2


def test_run_id_defaults_to_the_file_stem(tmp_path: Path) -> None:
    har = write_har(tmp_path / "session-7.har", entry())

    assert load_har(har).run_id == "session-7"
    assert load_har(har, run_id="custom").run_id == "custom"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "not valid JSON"),
        ('{"log": {}}', "missing log.entries"),
        ("[]", "missing log.entries"),
        ('{"log": {"entries": [{"request": {}}]}}', "startedDateTime"),
    ],
)
def test_malformed_har(tmp_path: Path, content: str, message: str) -> None:
    har = tmp_path / "bad.har"
    har.write_text(content)

    with pytest.raises(HarFormatError, match=message):
        load_har(har)


def test_entry_without_url_is_rejected(tmp_path: Path) -> None:
    bad = entry()
    del bad["request"]["url"]
    har = write_har(tmp_path / "x.har", bad)

    with pytest.raises(HarFormatError, match="no request method or URL"):
        load_har(har)


@pytest.mark.parametrize(
    ("raw", "mime", "expected"),
    [
        (None, "text/plain", None),
        ("", "text/plain", None),
        ("hello", None, "hello"),
        (b"caf\xe9", "text/plain; charset=latin-1", "café"),
        (b"caf\xc3\xa9", "application/json", "café"),
        (b"\xff\xfe\x00binary", None, None),
        (b"\xff\xfe", None, None),
        (b"plain bytes", "application/x-unknown", "plain bytes"),
        (b"ignored", "font/woff2", None),
        (b"ok", "text/plain; charset=bogus", "ok"),
    ],
)
def test_decode_body(raw: bytes | str | None, mime: str | None, expected: str | None) -> None:
    assert decode_body(raw, mime, 100) == expected


def test_decode_body_truncates() -> None:
    assert decode_body("abcdefghij", "text/plain", 4) == "abcd…[truncated 6 chars]"


def test_decode_body_of_a_prefix_drops_a_cut_character() -> None:
    prefix = BodyPrefix("abcé".encode()[:-1], total_bytes=1000)

    assert decode_body(prefix, None, 2) == "ab…[truncated; body is 1000 bytes]"
    assert decode_body(prefix, "font/woff2", 2) is None


def test_decode_body_ignores_non_text_charsets() -> None:
    assert decode_body(b"aGk=", "text/plain; charset=base64", 100) == "aGk="


def test_large_body_files_are_read_as_a_prefix(tmp_path: Path) -> None:
    (tmp_path / "big.json").write_text('{"items": "' + "x" * 100_000 + '"}')
    har = write_har(
        tmp_path / "x.har", entry(content={"mimeType": "application/json", "_file": "big.json"})
    )

    body = load_har(har, max_body_chars=20).network[0].response_body

    assert body == '{"items": "xxxxxxxxx…[truncated; body is 100013 bytes]'


def test_body_files_that_are_not_regular_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "dir.json").mkdir()
    har = write_har(
        tmp_path / "x.har", entry(content={"mimeType": "application/json", "_file": "dir.json"})
    )

    run = load_har(har)

    assert run.network[0].response_body is None
    assert run.metadata["warnings"] == ["body file dir.json is not a regular file"]


def test_deeply_nested_har_is_a_format_error(tmp_path: Path) -> None:
    har = tmp_path / "deep.har"
    har.write_text("[" * 100_000 + "]" * 100_000)

    with pytest.raises(HarFormatError, match="nests too deeply"):
        load_har(har)


def test_headers_dict_lowercases_and_joins_duplicates() -> None:
    raw = [
        {"name": "Set-Cookie", "value": "a=1"},
        {"name": "set-cookie", "value": "b=2"},
        {"name": "X-Bad"},
        "junk",
    ]

    assert headers_dict(raw) == {"set-cookie": "a=1, b=2"}
