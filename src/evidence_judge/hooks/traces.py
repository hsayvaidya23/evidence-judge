"""Find the trace an agent just recorded, and turn a Playwright MCP trace directory into a zip.

Playwright MCP (``--caps=devtools``, ``browser_start_tracing``/``browser_stop_tracing``) writes
an unzipped trace: ``<output dir>/traces/trace-<ts>.trace``, a ``.network`` sibling and a shared
``resources/`` directory. Playwright Test and ``context.tracing.stop(path=...)`` write zips.
"""

from __future__ import annotations

import os
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

DEFAULT_TRACE_DIRS: Final = (".playwright-mcp", ".evidence-judge/traces", "test-results")
_MAX_DEPTH: Final = 5
_MAX_ENTRIES: Final = 20_000
_MAX_PACKED_BYTES: Final = 512 * 1024 * 1024
_BLOB_REF: Final = re.compile(rb'"_?(file|sha1)"\s*:\s*"([^"\\]{1,512})"')
_SAFE_NAME: Final = re.compile(r"^[\w@.+=-]+(/[\w@.+=-]+)*$")
_WALL_TIME: Final = re.compile(rb'"wallTime"\s*:\s*(\d{1,16}(?:\.\d+)?)')
_HEAD_BYTES: Final = 64 * 1024
_TAIL_BYTES: Final = 1024 * 1024
_MAX_EVENTS_BYTES: Final = 256 * 1024 * 1024


class TraceTooLargeError(ValueError):
    """The trace's files exceed the packing limit."""


@dataclass(frozen=True, slots=True)
class TraceFile:
    path: Path
    mtime: float
    kind: Literal["zip", "raw"]


def search_roots(root: Path, extra: Iterable[Path] = ()) -> list[Path]:
    """The directories to search: the defaults under ``root`` plus configured ones (relative
    ones resolved against ``root``). Missing directories are dropped."""
    roots: list[Path] = []
    for entry in (*(Path(d) for d in DEFAULT_TRACE_DIRS), *extra):
        candidate = (entry if entry.is_absolute() else root / entry).resolve()
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def _walk(top: Path) -> Iterator[os.DirEntry[str]]:
    """Files under ``top``, without following symlinks, bounded in depth and entry count."""
    stack: list[tuple[Path, int]] = [(top, 0)]
    seen = 0
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > _MAX_ENTRIES:
                        return
                    if entry.is_dir(follow_symlinks=False):
                        if depth < _MAX_DEPTH and entry.name != "resources":
                            stack.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        yield entry
        except OSError:
            continue


def kind_of(name: str) -> Literal["zip", "raw"] | None:
    if name.endswith(".zip") and "trace" in name:
        return "zip"
    if name.endswith(".trace"):
        return "raw"
    return None


def fresh_traces(roots: Iterable[Path], *, since: float) -> list[TraceFile]:
    """Every trace under ``roots`` modified at or after ``since``, newest first."""
    found: dict[Path, TraceFile] = {}
    for top in roots:
        for entry in _walk(top):
            kind = kind_of(entry.name)
            if kind is None:
                continue
            try:
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime >= since:
                path = Path(entry.path)
                found[path] = TraceFile(path, mtime, kind)
    return sorted(found.values(), key=lambda t: t.mtime, reverse=True)


def _wall_times(data: bytes) -> list[float]:
    return [float(v) / 1000 for v in _WALL_TIME.findall(data)]


def _events_bytes(trace: TraceFile) -> bytes:
    """The trace's event stream: the head and tail of a raw ``.trace``, or a zip's whole
    ``*.trace`` members (bounded)."""
    if trace.kind == "raw":
        with trace.path.open("rb") as handle:
            head = handle.read(_HEAD_BYTES)
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(_HEAD_BYTES, size - _TAIL_BYTES))
            return head + b"\n" + handle.read(_TAIL_BYTES)
    chunks: list[bytes] = []
    total = 0
    with zipfile.ZipFile(trace.path) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".trace"):
                continue
            total += info.file_size
            if total > _MAX_EVENTS_BYTES:
                raise TraceTooLargeError(f"{trace.path.name}: trace events exceed the read limit")
            chunks.append(archive.read(info))
    return b"\n".join(chunks)


def recorded_until(trace: TraceFile) -> float | None:
    """The latest wall-clock time (epoch seconds) the trace's own events record, or None when
    it records none. Unlike the file's mtime, the agent cannot change this without rewriting
    the trace.

    Raises:
        OSError, ValueError: the trace cannot be read.
    """
    times = _wall_times(_events_bytes(trace))
    return max(times) if times else None


def _blob_names(data: bytes) -> set[str]:
    names: set[str] = set()
    for field, raw in _BLOB_REF.findall(data):
        value = raw.decode("utf-8", errors="replace")
        name = f"resources/{value}" if field == b"sha1" else value
        if _SAFE_NAME.match(name) and ".." not in name.split("/"):
            names.add(name)
    return names


def _locate(base: Path, name: str) -> Path | None:
    """Where a blob the trace names lives: at its own path, or in the shared ``resources/``."""
    for candidate in (base / name, base / "resources" / Path(name).name):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_relative_to(base) and resolved.is_file():
            return resolved
    return None


def pack_raw_trace(trace: Path, dest: Path) -> Path:
    """Zip an unzipped trace (``<stem>.trace``, its ``.network`` sibling and the blobs they
    reference) into ``dest`` in the layout Playwright's own zips use.

    Raises:
        TraceTooLargeError: the files together exceed the packing limit.
        OSError: a file cannot be read or the zip cannot be written.
    """
    base = trace.parent.resolve()
    network = trace.with_suffix(".network")
    events = [(trace, "trace.trace")]
    if network.is_file():
        events.append((network, "trace.network"))
    total = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        names: set[str] = set()
        for source, arcname in events:
            data = source.read_bytes()
            total += len(data)
            if total > _MAX_PACKED_BYTES:
                raise TraceTooLargeError(f"{trace.name}: trace files exceed the packing limit")
            archive.writestr(arcname, data)
            names |= _blob_names(data)
        for name in sorted(names):
            blob = _locate(base, name)
            if blob is None:
                continue
            total += blob.stat().st_size
            if total > _MAX_PACKED_BYTES:
                raise TraceTooLargeError(f"{trace.name}: trace files exceed the packing limit")
            archive.write(blob, name)
    return dest
