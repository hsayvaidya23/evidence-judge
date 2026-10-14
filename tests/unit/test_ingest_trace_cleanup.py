"""Loading a trace never leaves the zip file open, whether it succeeds or fails."""

from __future__ import annotations

import gc
import io
import sys
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from evidence_judge.ingest import TraceFormatError, load_playwright_trace
from tests.unit.test_ingest_playwright_trace import RUNS, Event, rewrite_trace

Rewrite = Callable[[str, list[Event]], list[Event]]


def _set_version(name: str, events: list[Event]) -> list[Event]:
    if name.endswith(".trace") and events:
        events[0] = {**events[0], "version": 99}
    return events


def _drop_context_options(name: str, events: list[Event]) -> list[Event]:
    return events[1:] if name.endswith(".trace") else events


def _bad_monotonic(name: str, events: list[Event]) -> list[Event]:
    if name.endswith(".trace") and events:
        events[0] = {**events[0], "monotonicTime": "soon"}
    return events


def _open_handles(path: Path) -> list[object]:
    target = str(path)
    return [
        o
        for o in gc.get_objects()
        if isinstance(o, io.BufferedReader | io.FileIO)
        and not o.closed
        and str(getattr(o, "name", "")) == target
    ]


@pytest.fixture
def unraisable() -> Iterator[list[str]]:
    """Collects ResourceWarnings raised from finalizers (where they cannot propagate)."""
    seen: list[str] = []

    def hook(info: sys.UnraisableHookArgs) -> None:
        seen.append(repr(info.exc_value))

    previous = sys.unraisablehook
    sys.unraisablehook = hook
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            yield seen
            gc.collect()
    finally:
        sys.unraisablehook = previous


@pytest.mark.parametrize("rewrite", [_set_version, _drop_context_options, _bad_monotonic])
def test_failed_load_closes_the_archive(
    tmp_path: Path, unraisable: list[str], rewrite: Rewrite
) -> None:
    trace = rewrite_trace(RUNS / "null_agent" / "trace.zip", tmp_path / "t.zip", rewrite)
    with pytest.raises(TraceFormatError) as info:
        load_playwright_trace(trace)
    assert _open_handles(trace) == []
    del info
    gc.collect()
    assert unraisable == []


def test_lazy_blobs_do_not_hold_the_archive_open(unraisable: list[str]) -> None:
    run = load_playwright_trace(RUNS / "honest_success" / "trace.zip")
    assert run.screenshots
    assert run.blobs[run.screenshots[0].resource]
    assert _open_handles(RUNS / "honest_success" / "trace.zip") == []
    del run
    gc.collect()
    assert unraisable == []
