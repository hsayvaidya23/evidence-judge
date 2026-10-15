"""Finding the trace an agent just recorded, and packing a Playwright MCP trace directory."""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

from evidence_judge.hooks import traces
from evidence_judge.hooks.traces import (
    TraceTooLargeError,
    newest_trace,
    pack_raw_trace,
    search_roots,
)
from evidence_judge.ingest import load_run
from tests.unit.hook_fixtures import RUNS, raw_trace


def _touch(path: Path, age_s: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_search_roots_keeps_existing_dirs_once(tmp_path: Path) -> None:
    (tmp_path / ".playwright-mcp").mkdir()
    (tmp_path / "out").mkdir()
    absolute = tmp_path / "abs"
    absolute.mkdir()
    roots = search_roots(tmp_path, [Path("out"), absolute, Path("missing"), Path("out")])
    assert roots == [
        (tmp_path / ".playwright-mcp").resolve(),
        (tmp_path / "out").resolve(),
        absolute.resolve(),
    ]


def test_newest_trace_picks_the_most_recent_fresh_trace(tmp_path: Path) -> None:
    _touch(tmp_path / "a" / "traces" / "trace-1.trace", 30)
    newest = _touch(tmp_path / "a" / "traces" / "trace-2.trace", 10)
    _touch(tmp_path / "b" / "run" / "trace.zip", 20)
    _touch(tmp_path / "b" / "notes.zip", 1)
    _touch(tmp_path / "b" / "page.png", 1)
    _touch(tmp_path / "a" / "traces" / "resources" / "x.trace", 1)
    found = newest_trace([tmp_path / "a", tmp_path / "b"], since=time.time() - 60)
    assert found is not None
    assert (found.path, found.kind) == (newest, "raw")
    assert newest_trace([tmp_path / "a"], since=time.time() - 5) is None


def test_newest_trace_finds_zips_and_skips_symlinks(tmp_path: Path) -> None:
    outside = _touch(tmp_path / "outside" / "trace.zip", 1)
    root = tmp_path / "root"
    (root / "linked").mkdir(parents=True)
    (root / "linked" / "trace.zip").symlink_to(outside)
    (root / "dir-link").symlink_to(tmp_path / "outside")
    assert newest_trace([root], since=0) is None
    real = _touch(root / "r" / "trace.zip", 5)
    found = newest_trace([root], since=0)
    assert found is not None
    assert (found.path, found.kind) == (real, "zip")


def test_newest_trace_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(5):
        _touch(tmp_path / f"f{i}.trace", 1)
    monkeypatch.setattr(traces, "_MAX_ENTRIES", 2)
    found = newest_trace([tmp_path], since=0)
    assert found is not None
    deep = tmp_path / "deep"
    path = deep
    for i in range(8):
        path = path / f"d{i}"
    _touch(path / "trace-9.trace", 0)
    monkeypatch.setattr(traces, "_MAX_ENTRIES", 10_000)
    found = newest_trace([deep], since=0)
    assert found is None


def test_newest_trace_survives_an_unreadable_dir(tmp_path: Path) -> None:
    assert newest_trace([tmp_path / "missing"], since=0) is None


def test_a_packed_trace_judges_like_the_original(tmp_path: Path) -> None:
    trace = raw_trace("forged_success", tmp_path / "traces")
    packed = pack_raw_trace(trace, tmp_path / "packed.zip")
    with (
        zipfile.ZipFile(packed) as archive,
        zipfile.ZipFile(RUNS / "forged_success" / "trace.zip") as original,
    ):
        assert sorted(archive.namelist()) == sorted(
            n for n in original.namelist() if not n.endswith("/")
        )
    run = load_run(trace=packed)
    reference = load_run(trace=RUNS / "forged_success" / "trace.zip")
    assert len(run.network) == len(reference.network)
    assert len(run.screenshots) == len(reference.screenshots)


def test_packing_skips_unsafe_and_missing_blob_names(tmp_path: Path) -> None:
    base = tmp_path / "traces"
    base.mkdir()
    (tmp_path / "secret.txt").write_text("do not pack")
    (base / "resources").mkdir()
    (base / "resources" / "ok.jpeg").write_bytes(b"jpeg")
    trace = base / "trace-1.trace"
    trace.write_text(
        '{"type":"screencast-frame","file":"../secret.txt"}\n'
        '{"type":"screencast-frame","file":"/etc/passwd"}\n'
        '{"type":"screenshot","sha1":"ok.jpeg"}\n'
        '{"type":"screenshot","sha1":"missing.jpeg"}\n'
    )
    (base / "resources" / "link.jpeg").symlink_to(tmp_path / "secret.txt")
    trace.write_text(trace.read_text() + '{"type":"screenshot","sha1":"link.jpeg"}\n')
    with zipfile.ZipFile(pack_raw_trace(trace, tmp_path / "p.zip")) as archive:
        assert sorted(archive.namelist()) == ["resources/ok.jpeg", "trace.trace"]


def test_packing_enforces_the_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trace = raw_trace("forged_success", tmp_path / "traces")
    monkeypatch.setattr(traces, "_MAX_PACKED_BYTES", 30_000)
    with pytest.raises(TraceTooLargeError):
        pack_raw_trace(trace, tmp_path / "p.zip")
    monkeypatch.setattr(traces, "_MAX_PACKED_BYTES", 10)
    with pytest.raises(TraceTooLargeError):
        pack_raw_trace(trace, tmp_path / "q.zip")
