from __future__ import annotations

import itertools
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.core.evidence import Channel, Run
from evidence_judge.ingest import (
    SUPPORTED_TRACE_VERSIONS,
    TraceFormatError,
    UnsupportedTraceVersionError,
    load_playwright_trace,
)

RUNS = Path(__file__).resolve().parent.parent / "fixtures" / "runs"
CONFIRMED = {"honest_success", "constraint_violation", "side_effect", "loop"}
SCENARIOS = sorted(p.name for p in RUNS.iterdir() if p.is_dir())

Event = dict[str, Any]


def load(scenario: str) -> Run:
    return load_playwright_trace(RUNS / scenario / "trace.zip", run_id=scenario)


def rewrite_trace(
    src: Path,
    dst: Path,
    transform: Callable[[str, list[Event]], list[Event]] = lambda _, events: events,
    rename: Callable[[str], str] = lambda name: name,
) -> Path:
    """Copy a trace zip, passing every NDJSON file's events through `transform`."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name.endswith((".trace", ".network")):
                events = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
                data = "".join(json.dumps(e) + "\n" for e in transform(name, events)).encode()
            zout.writestr(rename(name), data)
    return dst


def test_scenarios_were_recorded() -> None:
    expected = {
        "constraint_violation",
        "forged_success",
        "honest_failure",
        "honest_success",
        "loop",
        "null_agent",
        "side_effect",
    }
    assert set(SCENARIOS) == expected


def test_honest_success_actions_are_normalised() -> None:
    run = load("honest_success")

    assert [(a.name, a.selector) for a in run.actions] == [
        ("goto", None),
        ("click", "text=Red Kettle"),
        ("click", "#add-to-cart"),
        ("click", "header >> text=Cart"),
        ("click", "#checkout"),
        ("click", "#confirm-order"),
    ]
    goto, *_, confirm = run.actions
    assert goto.value is not None and goto.value.endswith("/")
    assert goto.url == "about:blank"
    assert confirm.url is not None and confirm.url.endswith("/checkout")
    assert all(a.call_id and a.error is None for a in run.actions)


def test_honest_success_records_the_order_request() -> None:
    run = load("honest_success")

    orders = [n for n in run.network if n.method == "POST" and n.url.endswith("/api/orders")]
    assert len(orders) == 1
    order = orders[0]
    assert order.status == 201
    assert order.ok and order.is_mutation
    assert order.resource_type == "fetch"
    assert order.request_body is not None
    assert json.loads(order.request_body) == {
        "items": [{"product_id": "red-kettle", "qty": 1, "price": 24}]
    }
    assert order.request_headers["content-type"] == "application/json"

    cart = next(n for n in run.network if n.url.endswith("/api/cart"))
    assert cart.response_body is not None
    assert json.loads(cart.response_body)["items"][0]["price"] == 24.0


def test_forged_success_never_posts_an_order() -> None:
    run = load("forged_success")

    assert not [n for n in run.network if n.url.endswith("/api/orders")]
    assert "#confirm-order" not in [a.selector for a in run.actions]
    assert run.final_state is not None
    assert run.final_state.url.endswith("/checkout")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_confirmation_text_only_in_final_state_of_completed_orders(scenario: str) -> None:
    run = load(scenario)

    assert run.final_state is not None
    assert ("Thank you for your order" in run.final_state.text) == (scenario in CONFIRMED)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_ids_are_sequential_and_chronological(scenario: str) -> None:
    run = load(scenario)

    for prefix, events in (
        ("act", run.actions),
        ("net", run.network),
        ("snap", run.snapshots),
        ("shot", run.screenshots),
        ("con", run.console),
    ):
        assert [e.id for e in events] == [f"{prefix}#{i}" for i in range(1, len(events) + 1)]
        times = [e.ts for e in events]
        assert times == sorted(times)
        assert all(t >= 0 for t in times)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_captured_channels_and_states(scenario: str) -> None:
    run = load(scenario)

    assert run.captured == set(Channel)
    assert run.source == "playwright-trace"
    assert run.metadata["trace_version"] in SUPPORTED_TRACE_VERSIONS
    assert run.metadata["viewport"] == {"width": 800, "height": 600}
    assert "warnings" not in run.metadata
    assert run.initial_state is not None and run.initial_state.snapshot_id == "snap#1"
    final = run.final_state
    assert final is not None
    assert final.snapshot_id == run.snapshots[-1].id
    assert final.screenshot_id == run.screenshots[-1].id


def test_snapshot_text_is_visible_text_only() -> None:
    run = load("honest_success")

    product = next(s for s in run.snapshots if s.url.endswith("/products/red-kettle"))
    assert product.title == "Red Kettle | Kettle Co."
    assert "Red Kettle A cheerful 1.5 L stovetop kettle in pillar-box red. $24.00" in product.text
    assert "Quantity Add to cart" in product.text
    assert run.metadata["form_values_excluded"] > 0
    for snapshot in run.snapshots:
        assert "KETTLE10" not in snapshot.text
        assert "postJson" not in snapshot.text
        assert "font-family" not in snapshot.text
        assert "  " not in snapshot.text


def test_consecutive_identical_snapshots_are_collapsed() -> None:
    run = load("honest_success")

    keys = [(s.url, s.title, s.text) for s in run.snapshots]
    assert all(a != b for a, b in itertools.pairwise(keys))
    assert any("Added to cart." in s.text for s in run.snapshots)


def test_screenshots_are_loaded_as_blobs() -> None:
    run = load("honest_success")

    assert run.screenshots
    for shot in run.screenshots:
        blob = run.blobs[shot.resource]
        assert blob.startswith(b"\xff\xd8")
        assert shot.mime_type == "image/jpeg"
        assert (shot.width, shot.height) == (800, 600)


def test_side_effect_fill_and_account_request() -> None:
    run = load("side_effect")

    fill = next(a for a in run.actions if a.name == "fill")
    assert (fill.selector, fill.value) == ("#email", "kettle-bot@example.net")
    assert fill.url is not None and fill.url.endswith("/account")
    account = [n for n in run.network if "/api/account" in n.url]
    assert [(n.method, n.status) for n in account] == [("POST", 200)]


def test_loop_repeats_add_to_cart_and_logs_console() -> None:
    run = load("loop")

    assert [a.selector for a in run.actions].count("#add-to-cart") == 6
    assert len([n for n in run.network if n.url.endswith("/api/cart")]) == 6
    assert len(run.console) == 6
    assert {c.level for c in run.console} == {"info"}
    assert all(c.text.startswith("cart updated 200") for c in run.console)


def test_null_agent_only_navigates() -> None:
    run = load("null_agent")

    assert [a.name for a in run.actions] == ["goto"]
    assert [n.method for n in run.network] == ["GET"]


def test_body_truncation() -> None:
    run = load_playwright_trace(RUNS / "honest_success" / "trace.zip", max_body_chars=10)

    page = next(n for n in run.network if n.resource_type == "document")
    assert page.response_body is not None
    assert page.response_body.startswith("<!doctype ")
    assert "[truncated" in page.response_body


def test_negative_max_body_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_body_chars"):
        load_playwright_trace(RUNS / "honest_success" / "trace.zip", max_body_chars=-1)


def _set_version(version: object) -> Callable[[str, list[Event]], list[Event]]:
    def transform(name: str, events: list[Event]) -> list[Event]:
        if name.endswith(".trace"):
            events[0]["version"] = version
        return events

    return transform


@pytest.mark.parametrize("version", [99, 5, "10", 10.5, True, None])
def test_unknown_version_is_rejected(tmp_path: Path, version: object) -> None:
    trace = rewrite_trace(
        RUNS / "null_agent" / "trace.zip", tmp_path / "t.zip", _set_version(version)
    )

    with pytest.raises(UnsupportedTraceVersionError) as info:
        load_playwright_trace(trace)
    assert info.value.version == version
    assert isinstance(info.value, TraceFormatError)


def test_missing_context_options_is_a_format_error(tmp_path: Path) -> None:
    trace = rewrite_trace(
        RUNS / "null_agent" / "trace.zip",
        tmp_path / "t.zip",
        lambda name, events: events[1:] if name.endswith(".trace") else events,
    )

    with pytest.raises(TraceFormatError, match="context-options"):
        load_playwright_trace(trace)


def test_not_a_zip_is_a_format_error(tmp_path: Path) -> None:
    bogus = tmp_path / "trace.zip"
    bogus.write_text("not a zip")

    with pytest.raises(TraceFormatError, match="not a readable trace zip"):
        load_playwright_trace(bogus)


def test_truncated_zip_is_a_format_error(tmp_path: Path) -> None:
    data = (RUNS / "honest_success" / "trace.zip").read_bytes()
    truncated = tmp_path / "trace.zip"
    truncated.write_bytes(data[: len(data) // 2])

    with pytest.raises(TraceFormatError):
        load_playwright_trace(truncated)


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_playwright_trace(tmp_path / "absent.zip")


def test_zip_without_trace_entry_is_a_format_error(tmp_path: Path) -> None:
    archive = tmp_path / "t.zip"
    with zipfile.ZipFile(archive, "w") as zout:
        zout.writestr("readme.txt", "hello")

    with pytest.raises(TraceFormatError, match=r"no \.trace entry"):
        load_playwright_trace(archive)


def _raw_rewrite(src: Path, dst: Path, mangle: Callable[[bytes], bytes]) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            zout.writestr(name, mangle(data) if name == "trace.trace" else data)
    return dst


def test_truncated_final_event_is_tolerated_with_warning(tmp_path: Path) -> None:
    trace = _raw_rewrite(
        RUNS / "honest_success" / "trace.zip",
        tmp_path / "t.zip",
        lambda data: data.rstrip(b"\n")[:-40],
    )

    run = load_playwright_trace(trace)
    assert run.actions
    assert any("truncated final event" in w for w in run.metadata["warnings"])


def test_corrupt_event_mid_file_is_a_format_error(tmp_path: Path) -> None:
    def corrupt(data: bytes) -> bytes:
        lines = data.splitlines()
        lines[3] = lines[3][:-10]
        return b"\n".join(lines) + b"\n"

    trace = _raw_rewrite(RUNS / "honest_success" / "trace.zip", tmp_path / "t.zip", corrupt)

    with pytest.raises(TraceFormatError, match=r"trace\.trace:4: invalid JSON"):
        load_playwright_trace(trace)


def test_legacy_sha1_blob_references(tmp_path: Path) -> None:
    def to_sha1(name: str, events: list[Event]) -> list[Event]:
        for event in events:
            if event.get("type") == "context-options":
                event["version"] = 8
            if event.get("type") == "screencast-frame":
                event["sha1"] = event.pop("file").removeprefix("screencast/")
            snapshot = event.get("snapshot", {})
            for part in (
                snapshot.get("request", {}).get("postData"),
                snapshot.get("response", {}).get("content"),
            ):
                if part and "_file" in part:
                    part["_sha1"] = part.pop("_file").removeprefix("resources/")
        return events

    trace = rewrite_trace(
        RUNS / "honest_success" / "trace.zip",
        tmp_path / "t.zip",
        to_sha1,
        rename=lambda name: name.replace("screencast/", "resources/"),
    )

    legacy = load_playwright_trace(trace)
    current = load("honest_success")
    assert legacy.metadata["trace_version"] == 8
    assert [n.request_body for n in legacy.network] == [n.request_body for n in current.network]
    assert [n.response_body for n in legacy.network] == [n.response_body for n in current.network]
    assert len(legacy.screenshots) == len(current.screenshots)
    assert all(s.resource.startswith("resources/") for s in legacy.screenshots)
    assert all(legacy.blobs[s.resource] for s in legacy.screenshots)


def test_multiple_contexts_are_combined(tmp_path: Path) -> None:
    archive = tmp_path / "multi.zip"
    with zipfile.ZipFile(archive, "w") as zout:
        for ordinal, scenario in enumerate(["honest_success", "null_agent"]):
            with zipfile.ZipFile(RUNS / scenario / "trace.zip") as zin:
                for name in zin.namelist():
                    target = f"{ordinal}-{name}" if name.startswith("trace.") else name
                    if target not in zout.namelist():
                        zout.writestr(target, zin.read(name))
        zout.writestr(
            "test.trace",
            json.dumps({"type": "context-options", "version": 10, "origin": "testRunner"}) + "\n",
        )

    run = load_playwright_trace(archive)
    first, second = load("honest_success"), load("null_agent")
    assert len(run.actions) == len(first.actions) + len(second.actions)
    assert len(run.network) == len(first.network) + len(second.network)
    assert [a.id for a in run.actions] == [f"act#{i}" for i in range(1, len(run.actions) + 1)]
    assert {a.call_id for a in run.actions} >= {a.call_id for a in first.actions}
    assert all(a.url is not None for a in run.actions)


def test_only_test_runner_trace_is_a_format_error(tmp_path: Path) -> None:
    archive = tmp_path / "t.zip"
    with zipfile.ZipFile(archive, "w") as zout:
        zout.writestr(
            "test.trace",
            json.dumps({"type": "context-options", "version": 10, "origin": "testRunner"}) + "\n",
        )

    with pytest.raises(TraceFormatError, match="no browser context"):
        load_playwright_trace(archive)


def test_page_errors_and_failed_actions(tmp_path: Path) -> None:
    def add_events(name: str, events: list[Event]) -> list[Event]:
        if not name.endswith(".trace"):
            return events
        start = events[0]["monotonicTime"]
        events.append(
            {
                "type": "before",
                "callId": "call@999",
                "startTime": start + 90_000,
                "class": "Frame",
                "method": "selectOption",
                "params": {"selector": "#size", "options": [{"valueOrLabel": "large"}]},
            }
        )
        events.append(
            {
                "type": "after",
                "callId": "call@999",
                "endTime": start + 90_500,
                "error": {"message": "Timeout 500ms exceeded.", "stack": "TimeoutError: ..."},
            }
        )
        events.append(
            {
                "type": "event",
                "time": start + 90_100,
                "class": "Page",
                "method": "pageError",
                "params": {"error": {"error": {"name": "TypeError", "message": "x is undefined"}}},
            }
        )
        return events

    trace = rewrite_trace(RUNS / "null_agent" / "trace.zip", tmp_path / "t.zip", add_events)

    run = load_playwright_trace(trace)
    select = run.actions[-1]
    assert (select.name, select.selector, select.value) == ("select", "#size", "large")
    assert select.error == "Timeout 500ms exceeded."
    assert select.ts == 90_000
    assert run.console[-1].level == "error"
    assert run.console[-1].text == "x is undefined"


def test_missing_blobs_are_skipped_with_warnings(tmp_path: Path) -> None:
    current = load("honest_success")
    order = next(n for n in current.network if n.url.endswith("/api/cart"))
    dropped_shot = current.screenshots[0].resource
    with zipfile.ZipFile(RUNS / "honest_success" / "trace.zip") as zin:
        network_lines = zin.read("trace.network").decode().splitlines()
        cart = next(json.loads(line) for line in network_lines if "/api/cart" in line)
        dropped_body = cart["snapshot"]["response"]["content"]["_file"]
        trimmed = tmp_path / "t.zip"
        with zipfile.ZipFile(trimmed, "w") as zout:
            for name in zin.namelist():
                if name not in {dropped_shot, dropped_body}:
                    zout.writestr(name, zin.read(name))

    run = load_playwright_trace(trimmed)

    assert len(run.screenshots) == len(current.screenshots) - 1
    assert dropped_shot not in run.blobs
    cart_event = next(n for n in run.network if n.url.endswith("/api/cart"))
    assert cart_event.response_body is None
    assert cart_event.request_body == order.request_body
    assert sorted(run.metadata["warnings"]) == sorted(
        [
            f"body resource {dropped_body} missing from trace",
            f"screenshot {dropped_shot} missing from trace",
        ]
    )
