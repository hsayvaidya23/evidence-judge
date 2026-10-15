"""Time budgets for ingest, merge and index operations on long synthetic recordings.

Budgets are several times the measured cost on a laptop, so they catch accidental quadratic
behaviour without flaking on a slow CI runner.
"""

from __future__ import annotations

import json
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.core.evidence import (
    ActionEvent,
    Channel,
    NetworkEvent,
    PageState,
    Run,
    Screenshot,
)
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.ingest import attach_har, load_playwright_trace

BUDGET_S = 2.0
TRACE_BUDGET_S = 10.0
NETWORK_EVENTS = 20_000
ACTIONS = 5_000
FRAMES = 2_000
SNAPSHOTS = 500
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40


@contextmanager
def within(budget_s: float) -> Iterator[None]:
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    assert elapsed < budget_s, f"took {elapsed:.2f}s, budget {budget_s}s"


def network_run(source: str, count: int) -> Run:
    return Run(
        run_id="r",
        source=source,
        network=[
            NetworkEvent(
                id=f"net#{i}",
                ts=float(i),
                method="POST" if i % 10 == 0 else "GET",
                url=f"https://shop.test/api/x{i % 2000}",
                status=200,
            )
            for i in range(count)
        ],
        captured={Channel.NETWORK},
    )


@pytest.fixture(scope="module")
def big_run() -> Run:
    return network_run("playwright-trace", NETWORK_EVENTS).model_copy(
        update={
            "actions": [
                ActionEvent(id=f"act#{i}", ts=float(i * 4), name="click", selector=f"#b{i}")
                for i in range(ACTIONS)
            ],
            "screenshots": [
                Screenshot(id=f"shot#{i}", ts=float(i * 10), resource=f"s{i}.jpeg")
                for i in range(FRAMES)
            ],
            "final_state": PageState(url="https://shop.test/thanks", text="Thank you"),
            "captured": {Channel.NETWORK, Channel.ACTIONS, Channel.SCREENSHOTS},
        }
    )


def test_attach_har_is_linear() -> None:
    trace = network_run("playwright-trace", NETWORK_EVENTS)
    har = network_run("har", NETWORK_EVENTS)
    har = har.model_copy(update={"network": har.network[::-1]})

    with within(BUDGET_S):
        merged = attach_har(trace, har)

    assert len(merged.network) == NETWORK_EVENTS


def test_merge_of_large_runs(big_run: Run) -> None:
    har = network_run("har", NETWORK_EVENTS)

    with within(BUDGET_S):
        merged = big_run.merge(har)

    assert len(merged.actions) == ACTIONS


def test_index_lookups_on_a_large_run(big_run: Run) -> None:
    with within(BUDGET_S):
        index = EvidenceIndex(big_run)
        assert len(index.by_id) == NETWORK_EVENTS + ACTIONS + FRAMES
        assert len(index.mutations()) == NETWORK_EVENTS // 10
        assert len(index.requests("GET")) == NETWORK_EVENTS - NETWORK_EVENTS // 10
        for ts in range(0, NETWORK_EVENTS, 2):
            assert index.nearest_screenshot(float(ts)) is not None
        assert len(index.page_states()) == 1


def _event_lines(events: Iterator[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events).encode()


def _trace_events() -> Iterator[dict[str, Any]]:
    yield {
        "version": 10,
        "type": "context-options",
        "origin": "library",
        "browserName": "chromium",
        "options": {"viewport": {"width": 800, "height": 600}},
        "wallTime": 1_792_000_000_000,
        "monotonicTime": 0,
    }
    for i in range(ACTIONS):
        start = 1.0 + i * 4
        yield {
            "type": "before",
            "callId": f"call@{i}",
            "startTime": start,
            "method": "click",
            "params": {"selector": f"#button-{i}"},
        }
        yield {"type": "after", "callId": f"call@{i}", "endTime": start + 1}
    for i in range(SNAPSHOTS):
        yield {
            "type": "frame-snapshot",
            "snapshot": {
                "callId": f"call@{i * 10}",
                "pageId": "page@1",
                "frameId": "frame@1",
                "frameUrl": f"https://shop.test/p/{i}",
                "html": ["HTML", {}, ["BODY", {}, ["P", {}, f"Product {i} " * 20]]],
                "timestamp": 2.0 + i * 40,
                "isMainFrame": True,
            },
        }
    for i in range(FRAMES):
        yield {
            "type": "screencast-frame",
            "pageId": "page@1",
            "file": f"screencast/frame-{i}.jpeg",
            "width": 800,
            "height": 600,
            "timestamp": 3.0 + i * 10,
        }


def _network_events() -> Iterator[dict[str, Any]]:
    for i in range(NETWORK_EVENTS):
        yield {
            "type": "resource-snapshot",
            "snapshot": {
                "_monotonicTime": 1.0 + i,
                "request": {
                    "method": "POST" if i % 10 == 0 else "GET",
                    "url": f"https://shop.test/api/x{i}",
                    "headers": [{"name": "Accept", "value": "application/json"}],
                },
                "response": {
                    "status": 200,
                    "headers": [{"name": "Content-Type", "value": "application/json"}],
                    "content": {"mimeType": "application/json", "text": f'{{"n": {i}}}'},
                },
            },
        }


@pytest.fixture(scope="module")
def large_trace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("perf") / "large.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("trace.trace", _event_lines(_trace_events()))
        archive.writestr("trace.network", _event_lines(_network_events()))
        for i in range(FRAMES):
            archive.writestr(f"screencast/frame-{i}.jpeg", JPEG, zipfile.ZIP_STORED)
    return path


@pytest.mark.perf
def test_large_trace_loads_within_budget(large_trace: Path) -> None:
    with within(TRACE_BUDGET_S):
        run = load_playwright_trace(large_trace)

    assert len(run.network) == NETWORK_EVENTS
    assert len(run.actions) == ACTIONS
    assert len(run.screenshots) == FRAMES
    assert len(run.snapshots) == SNAPSHOTS
    assert EvidenceIndex(run).screenshot_bytes(run.screenshots[-1]) == JPEG


STYLED_SNAPSHOTS = 100
STYLED_ELEMENTS = 2_000
STYLED_BUDGET_S = 20.0
STYLE_RULES = 10_000


def _styled_events() -> Iterator[dict[str, Any]]:
    yield {
        "version": 10,
        "type": "context-options",
        "origin": "library",
        "browserName": "chromium",
        "wallTime": 1_792_000_000_000,
        "monotonicTime": 0,
    }
    head = [
        "HEAD",
        {},
        ["BASE", {"href": "https://shop.test/"}],
        ["LINK", {"rel": "stylesheet", "href": "/app.css"}],
    ]
    for i in range(STYLED_SNAPSHOTS):
        items = [
            [
                "DIV",
                {"class": f"card c{n % 500} {'hidden' if n % 7 == 0 else 'shown'}"},
                f"Item {n}.",
            ]
            for n in range(STYLED_ELEMENTS)
        ]
        yield {
            "type": "frame-snapshot",
            "snapshot": {
                "pageId": "page@1",
                "frameId": "frame@1",
                "frameUrl": f"https://shop.test/{i}",
                "html": ["HTML", {}, head, ["BODY", {}, ["MAIN", {"id": f"m{i}"}, *items]]],
                "viewport": {"width": 1280, "height": 720},
                "timestamp": 2.0 + i,
                "isMainFrame": True,
            },
        }
    css = "".join(
        f".c{i % 500} .x{i}, main > .c{i % 500}.y{i} {{ display: none }}"
        f"@media (min-width: {i % 2000}px) {{ #m{i} .card {{ visibility: visible }} }}"
        for i in range(STYLE_RULES)
    )
    yield {
        "type": "resource-snapshot",
        "snapshot": {
            "_monotonicTime": 1.0,
            "request": {"method": "GET", "url": "https://shop.test/app.css", "headers": []},
            "response": {
                "status": 200,
                "headers": [],
                "content": {"mimeType": "text/css", "text": css},
            },
        },
    }


@pytest.mark.perf
def test_snapshots_with_a_large_stylesheet_render_within_budget(tmp_path: Path) -> None:
    path = tmp_path / "styled.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("trace.trace", _event_lines(_styled_events()))

    with within(TRACE_BUDGET_S):
        run = load_playwright_trace(path)

    assert len(run.snapshots) == STYLED_SNAPSHOTS
    assert "Item 1." in run.snapshots[-1].text
    assert "Item 7." not in run.snapshots[-1].text
