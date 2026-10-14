from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path
from typing import IO, Any

import pytest

from evidence_judge.core.evidence import Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.ingest import (
    TraceFormatError,
    TraceLimits,
    TraceTooLargeError,
    load_playwright_trace,
)

RUNS = Path(__file__).resolve().parent.parent / "fixtures" / "runs"
HONEST = RUNS / "honest_success" / "trace.zip"
MiB = 1024 * 1024


def copy_trace(dst: Path, extra: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(HONEST) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            zout.writestr(name, zin.read(name))
        for name, data in (extra or {}).items():
            zout.writestr(name, data)
    return dst


def cart_body_file() -> str:
    with zipfile.ZipFile(HONEST) as archive:
        lines = archive.read("trace.network").decode().splitlines()
    cart = next(json.loads(line) for line in lines if "/api/cart" in line)
    return cart["snapshot"]["response"]["content"]["_file"]


def test_too_large_is_a_format_error() -> None:
    assert issubclass(TraceTooLargeError, TraceFormatError)


def test_zip_bomb_entry_is_refused_by_compression_ratio(tmp_path: Path) -> None:
    trace = copy_trace(tmp_path / "t.zip", {"resources/bomb": b"\0" * (4 * MiB)})

    with pytest.raises(TraceTooLargeError, match=r"resources/bomb.*compression ratio"):
        load_playwright_trace(trace)


def test_small_highly_compressible_entries_are_allowed(tmp_path: Path) -> None:
    trace = copy_trace(tmp_path / "t.zip", {"resources/zeros": b"\0" * 4096})

    assert load_playwright_trace(trace).actions


def test_total_uncompressed_size_is_capped() -> None:
    with pytest.raises(TraceTooLargeError, match="uncompressed"):
        load_playwright_trace(HONEST, limits=TraceLimits(max_total_bytes=100_000))


def test_trace_entry_size_is_capped() -> None:
    with pytest.raises(TraceTooLargeError, match=r"trace\.trace"):
        load_playwright_trace(HONEST, limits=TraceLimits(max_entry_bytes=20_000))


def test_entry_count_is_capped() -> None:
    with pytest.raises(TraceTooLargeError, match="entries"):
        load_playwright_trace(HONEST, limits=TraceLimits(max_entries=5))


def test_declared_sizes_cannot_be_exceeded(tmp_path: Path) -> None:
    trace = copy_trace(tmp_path / "t.zip")
    with zipfile.ZipFile(trace) as archive:
        real_size = archive.getinfo("trace.trace").file_size
    data = bytearray(trace.read_bytes())
    central_header = data.rfind(b"trace.trace") - 46
    assert data[central_header : central_header + 4] == b"PK\x01\x02"
    data[central_header + 24 : central_header + 28] = (real_size // 4).to_bytes(4, "little")
    trace.write_bytes(bytes(data))

    with pytest.raises(TraceFormatError):
        load_playwright_trace(trace)


def test_oversized_resources_are_skipped_with_warnings(tmp_path: Path) -> None:
    body = cart_body_file()
    trace = copy_trace(tmp_path / "t.zip", {"resources/huge.jpeg": b"\xff\xd8" + b"x" * 300})
    limits = TraceLimits(max_resource_bytes=40)

    run = load_playwright_trace(trace, limits=limits)

    assert run.screenshots == []
    cart = next(n for n in run.network if n.url.endswith("/api/cart"))
    assert cart.response_body is None
    warnings = run.metadata["warnings"]
    assert f"body resource {body} is larger than 40 bytes" in warnings
    assert any(w.startswith("screenshot screencast/") and "larger than 40" in w for w in warnings)


class _OpenSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[str] = []
        original = zipfile.ZipFile.open

        def spy(archive: zipfile.ZipFile, name: Any, *args: Any, **kwargs: Any) -> IO[bytes]:
            self.opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
            return original(archive, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", spy)


def test_screenshots_are_read_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _OpenSpy(monkeypatch)

    run = load_playwright_trace(HONEST)

    assert run.screenshots
    assert not [n for n in spy.opened if n.startswith("screencast/")]
    shot = run.screenshots[-1]
    with zipfile.ZipFile(HONEST) as archive:
        expected = archive.read(shot.resource)
    assert EvidenceIndex(run).screenshot_bytes(shot) == expected
    assert run.blobs.get(shot.resource) == expected
    assert spy.opened.count(shot.resource) == 3


def test_event_files_are_streamed_not_read_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> bytes:
        raise AssertionError("ZipFile.read loads a whole entry into memory")

    monkeypatch.setattr(zipfile.ZipFile, "read", refuse)

    run = load_playwright_trace(HONEST)

    assert run.actions and run.network and run.snapshots


def test_large_bodies_are_read_as_a_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = cart_body_file()
    payload = json.dumps({"items": ["kettle"] * 50_000}).encode()
    with zipfile.ZipFile(HONEST) as zin, zipfile.ZipFile(tmp_path / "t.zip", "w") as zout:
        for name in zin.namelist():
            zout.writestr(name, payload if name == body else zin.read(name))
    reads: list[int] = []
    original = zipfile.ZipExtFile.read

    def spy(stream: zipfile.ZipExtFile, n: int | None = -1) -> bytes:
        data = original(stream, n)
        if stream.name == body:
            reads.append(len(data))
        return data

    monkeypatch.setattr(zipfile.ZipExtFile, "read", spy)

    run = load_playwright_trace(tmp_path / "t.zip", max_body_chars=100)

    cart = next(n for n in run.network if n.url.endswith("/api/cart"))
    assert cart.response_body is not None
    assert cart.response_body.startswith('{"items": ["kettle", ')
    assert cart.response_body.endswith(f"…[truncated; body is {len(payload)} bytes]")
    assert sum(reads) <= 4 * 100 + 8


def test_lazy_blobs_survive_copies_and_merges() -> None:
    run = load_playwright_trace(HONEST)
    shot = run.screenshots[0]

    copied = run.model_copy(deep=True)
    merged = Run(run_id="other", source="har", blobs={"extra": b"x"}).merge(run)
    clone = copy.deepcopy(run)

    for candidate in (copied, merged, clone):
        assert candidate.blobs[shot.resource] == run.blobs[shot.resource]
    assert merged.blobs["extra"] == b"x"
    assert set(merged.blobs) == set(run.blobs) | {"extra"}


def test_lazy_blob_missing_after_the_trace_is_removed(tmp_path: Path) -> None:
    trace = copy_trace(tmp_path / "t.zip")
    run = load_playwright_trace(trace)
    shot = run.screenshots[0]
    trace.unlink()

    assert EvidenceIndex(run).screenshot_bytes(shot) is None
