"""HAR and browser-use files are size-checked before they are read."""

from __future__ import annotations

from pathlib import Path

import pytest

from evidence_judge.ingest import (
    IngestError,
    TooLargeError,
    TraceTooLargeError,
    load_browser_use_history,
    load_har,
)
from evidence_judge.ingest.browser_use import MAX_HISTORY_BYTES
from evidence_judge.ingest.har import MAX_HAR_BYTES

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
HAR = FIXTURES / "runs" / "honest_success" / "run.har"
HISTORY = FIXTURES / "browser_use" / "history.json"


def test_defaults() -> None:
    assert MAX_HAR_BYTES == 200 * 1024 * 1024
    assert MAX_HISTORY_BYTES == 50 * 1024 * 1024


def test_har_over_the_cap_is_rejected() -> None:
    size = HAR.stat().st_size
    with pytest.raises(TooLargeError, match="limit") as info:
        load_har(HAR, max_file_bytes=size - 1)
    assert isinstance(info.value, IngestError)
    assert load_har(HAR, max_file_bytes=size).network


def test_history_over_the_cap_is_rejected() -> None:
    size = HISTORY.stat().st_size
    with pytest.raises(TooLargeError, match="limit"):
        load_browser_use_history(HISTORY, max_file_bytes=size - 1)
    assert load_browser_use_history(HISTORY, max_file_bytes=size).actions


def test_cap_holds_even_when_the_size_is_not_known_in_advance(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    path.write_text('{"history": []}' + " " * 100, encoding="utf-8")
    with pytest.raises(TooLargeError):
        load_browser_use_history(path, max_file_bytes=50)


def test_trace_too_large_is_a_too_large_error() -> None:
    assert issubclass(TraceTooLargeError, TooLargeError)
