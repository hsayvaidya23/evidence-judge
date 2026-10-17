"""Deterministic mutation fuzzing of every ingest entry point.

Each case mutates a real fixture (trace zip, HAR, browser-use history) with a seeded
random.Random, so failures reproduce from the seed alone. A malformed input may load or may be
rejected, but only with a typed IngestError, quickly, and the loaded Run must stay usable.
"""

from __future__ import annotations

import io
import json
import random
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.core.evidence import Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.ingest import (
    IngestError,
    load_browser_use_history,
    load_har,
    load_playwright_trace,
    load_run,
)
from evidence_judge.redact import Redactor

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
RUNS = FIXTURES / "runs"
TRACES = sorted(RUNS.glob("*/trace.zip"))
HARS = sorted(RUNS.glob("*/run.har"))
HISTORY = FIXTURES / "browser_use" / "history.json"
CASES_PER_KIND = {"trace": 90, "har": 70, "history": 40}
CASE_BUDGET_S = 5.0

Mutator = Callable[[random.Random, bytes], bytes]


def flip_bytes(rng: random.Random, data: bytes) -> bytes:
    out = bytearray(data)
    for _ in range(rng.randint(1, 16)):
        out[rng.randrange(len(out))] = rng.randrange(256)
    return bytes(out)


def truncate(rng: random.Random, data: bytes) -> bytes:
    return data[: rng.randrange(len(data))]


def insert_bytes(rng: random.Random, data: bytes) -> bytes:
    at = rng.randrange(len(data))
    return data[:at] + rng.randbytes(rng.randint(1, 512)) + data[at:]


def random_bytes(rng: random.Random, _: bytes) -> bytes:
    return rng.randbytes(rng.randint(0, 4096))


def random_json(rng: random.Random, depth: int = 0) -> Any:
    choices: list[Callable[[], Any]] = [
        lambda: None,
        lambda: rng.choice([True, False]),
        lambda: rng.randint(-(2**63), 2**63),
        lambda: rng.uniform(-1e12, 1e12),
        lambda: rng.choice(["", "x", "GET", "2026-13-45T99:99", "\x00", "é" * 3, "-1"]),
    ]
    if depth < 3:
        choices.append(lambda: [random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))])
        choices.append(
            lambda: {
                rng.choice(["request", "url", "type", "file", "x"]): random_json(rng, depth + 1)
                for _ in range(rng.randint(0, 3))
            }
        )
    return rng.choice(choices)()


def mutate_json_value(rng: random.Random, value: Any) -> Any:
    """Replace one randomly chosen node of a JSON document with a value of a random type."""
    nodes: list[tuple[Any, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in list(node):  # pyright: ignore[reportUnknownArgumentType]
                nodes.append((node, key))
                walk(node[key])
        elif isinstance(node, list):
            for index in range(len(node)):  # pyright: ignore[reportUnknownArgumentType]
                nodes.append((node, index))
                walk(node[index])

    walk(value)
    if not nodes:
        return random_json(rng)
    for _ in range(rng.randint(1, 4)):
        parent, key = rng.choice(nodes)
        parent[key] = random_json(rng)
    return value


def wrong_types(rng: random.Random, data: bytes) -> bytes:
    return json.dumps(mutate_json_value(rng, json.loads(data))).encode()


def huge_line(rng: random.Random, data: bytes) -> bytes:
    filler = rng.choice([b"a", b"[", b"{", b'"', b"\\", b"0"]) * rng.randint(100_000, 2_000_000)
    at = rng.randrange(len(data))
    return data[:at] + filler + data[at:]


def deep_nesting(rng: random.Random, _: bytes) -> bytes:
    depth = rng.randint(1_000, 200_000)
    return b"[" * depth + b"]" * depth


BYTE_MUTATORS: list[Mutator] = [flip_bytes, truncate, insert_bytes, random_bytes, huge_line]
JSON_MUTATORS: list[Mutator] = [*BYTE_MUTATORS, wrong_types, wrong_types, deep_nesting]


def mutate_ndjson(rng: random.Random, data: bytes) -> bytes:
    lines = data.splitlines()
    if not lines:
        return data
    for _ in range(rng.randint(1, 6)):
        index = rng.randrange(len(lines))
        match rng.randrange(6):
            case 0:
                lines[index] = lines[index][: rng.randrange(len(lines[index]) + 1)]
            case 1:
                del lines[index]
            case 2:
                lines.insert(index, lines[rng.randrange(len(lines))])
            case 3:
                lines[index] = huge_line(rng, lines[index] or b"{}")
            case 4:
                lines[index] = json.dumps(random_json(rng)).encode()
            case _:
                try:
                    event = json.loads(lines[index])
                except ValueError:
                    continue
                lines[index] = json.dumps(mutate_json_value(rng, event)).encode()
        if not lines:
            break
    return b"\n".join(lines) + b"\n"


def mutate_zip(rng: random.Random, data: bytes) -> bytes:
    """Mutate entries inside a well-formed zip (events, blobs, names) or the zip bytes."""
    if rng.random() < 0.35:
        return rng.choice(BYTE_MUTATORS)(rng, data)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w") as zout:
        names = zin.namelist()
        drop = set(rng.sample(names, k=rng.randint(0, 2)))
        for name in names:
            if name in drop:
                continue
            blob = zin.read(name)
            if name.endswith((".trace", ".network")) and rng.random() < 0.7:
                blob = mutate_ndjson(rng, blob)
            elif rng.random() < 0.1:
                blob = rng.choice(BYTE_MUTATORS)(rng, blob or b"x")
            zout.writestr(name, blob, compress_type=rng.choice([0, 8]))
    return out.getvalue()


def exercise(run: Run) -> None:
    """A loaded run must survive indexing, lazy blob reads and redaction."""
    index = EvidenceIndex(run)
    assert len(index.by_id) >= len(run.actions)
    for shot in run.screenshots:
        blob = index.screenshot_bytes(shot)
        assert blob is None or isinstance(blob, bytes)
    redacted, _ = Redactor(b"fuzz").redact_run(run)
    assert redacted.run_id == run.run_id


def check(load: Callable[[], Run]) -> str:
    start = time.process_time()
    try:
        run = load()
    except IngestError:
        outcome = "rejected"
    else:
        exercise(run)
        outcome = "loaded"
    assert time.process_time() - start < CASE_BUDGET_S
    return outcome


def cases(kind: str) -> list[int]:
    return list(range(CASES_PER_KIND[kind]))


@pytest.mark.parametrize("seed", cases("trace"))
def test_mutated_traces_fail_cleanly(seed: int, tmp_path: Path) -> None:
    rng = random.Random(f"trace-{seed}")
    source = rng.choice(TRACES)
    path = tmp_path / "trace.zip"
    path.write_bytes(mutate_zip(rng, source.read_bytes()))
    har = source.parent / "run.har"

    check(lambda: load_playwright_trace(path, max_body_chars=rng.choice([0, 50, 20_000])))
    check(lambda: load_run(trace=path, har=har, allow_mismatch=rng.random() < 0.5))


@pytest.mark.parametrize("seed", cases("har"))
def test_mutated_hars_fail_cleanly(seed: int, tmp_path: Path) -> None:
    rng = random.Random(f"har-{seed}")
    source = rng.choice(HARS)
    path = tmp_path / "run.har"
    path.write_bytes(rng.choice(JSON_MUTATORS)(rng, source.read_bytes()))

    check(lambda: load_har(path, max_body_chars=rng.choice([0, 50, 20_000])))
    check(lambda: load_run(trace=source.parent / "trace.zip", har=path))


@pytest.mark.parametrize("seed", cases("history"))
def test_mutated_histories_fail_cleanly(seed: int, tmp_path: Path) -> None:
    rng = random.Random(f"history-{seed}")
    path = tmp_path / "history.json"
    path.write_bytes(rng.choice(JSON_MUTATORS)(rng, HISTORY.read_bytes()))

    check(lambda: load_browser_use_history(path))


@pytest.mark.perf
def test_fuzzing_reaches_both_outcomes(tmp_path: Path) -> None:
    outcomes: set[str] = set()
    for seed in range(40):
        rng = random.Random(f"coverage-{seed}")
        path = tmp_path / f"{seed}.zip"
        path.write_bytes(mutate_zip(rng, TRACES[0].read_bytes()))
        outcomes.add(check(lambda: load_playwright_trace(path)))

    assert outcomes == {"loaded", "rejected"}
