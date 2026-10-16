"""Load directories of recorded, labelled runs as benchmark scenarios.

Layout, one directory per scenario::

    <runs_dir>/<scenario>/
        expected.json    {"outcome": "pass" | "fail" | "needs_review"}   (required)
        trace.zip        Playwright trace          } at least one
        run.har          HAR                       } evidence source
        history.json     browser-use history       }
        answer.txt       agent final answer        (optional)
        reasoning.txt    agent reasoning, one step per paragraph (optional)
        spec.yaml        task spec                 (optional; enables the with-spec judge)
        spec.generic.yaml  a coarser spec written without looking at the recording
                         (optional; enables the generic-spec judge)

Directories without ``expected.json`` are skipped.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome
from evidence_judge.ingest import load_run

EXPECTED_FILE = "expected.json"
GENERIC_SPEC_FILE = "spec.generic.yaml"


class BenchInputError(ValueError):
    """A scenario directory is missing evidence or holds an invalid label or spec."""


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    run: Run
    expected: Outcome
    spec: TaskSpec | None = None
    generic_spec: TaskSpec | None = None
    recording: str = ""
    """A digest of the evidence files: scenarios that judge one recording again share it."""

    @property
    def forgery_base(self) -> bool:
        """Failed runs (and any null-agent run) are the ones forged into fake successes."""
        return self.expected is Outcome.FAIL or "null" in self.name.lower()


def load_scenarios(*runs_dirs: Path, max_body_chars: int = 20_000) -> list[Scenario]:
    """The scenarios of every directory, in order; a name may appear in only one of them."""
    folders: list[Path] = []
    for runs_dir in runs_dirs:
        if not runs_dir.is_dir():
            raise BenchInputError(f"{runs_dir} is not a directory")
        found = sorted(p for p in runs_dir.iterdir() if (p / EXPECTED_FILE).is_file())
        if not found:
            raise BenchInputError(f"no scenario directories with {EXPECTED_FILE} under {runs_dir}")
        folders += found
    names = [folder.name for folder in folders]
    if duplicate := next((n for n in names if names.count(n) > 1), None):
        raise BenchInputError(f"scenario {duplicate} is in more than one directory")
    return [load_scenario(folder, max_body_chars=max_body_chars) for folder in folders]


def redaction_key(*runs_dirs: Path) -> bytes:
    """A redaction key derived from the recorded files: the same runs get the same tokens on
    every offline bench, so its results are byte-reproducible, and only a reader who already
    holds the unredacted runs can recompute it."""
    digest = hashlib.sha256(b"evidence-judge bench redaction key\0")
    for runs_dir in runs_dirs:
        for path in sorted(p for p in runs_dir.rglob("*") if p.is_file()):
            digest.update(path.relative_to(runs_dir).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
    return digest.digest()


def load_scenario(folder: Path, *, max_body_chars: int = 20_000) -> Scenario:
    name = folder.name
    try:
        expected = Outcome(json.loads((folder / EXPECTED_FILE).read_text("utf-8"))["outcome"])
        spec_path = folder / "spec.yaml"
        spec = TaskSpec.load(spec_path) if spec_path.is_file() else None
        generic_path = folder / GENERIC_SPEC_FILE
        generic_spec = TaskSpec.load(generic_path) if generic_path.is_file() else None
        trace = _optional(folder / "trace.zip")
        har = _optional(folder / "run.har")
        history = _optional(folder / "history.json")
        if not (trace or har or history):
            raise BenchInputError("no trace.zip, run.har or history.json")
        run = load_run(
            trace=trace,
            har=har,
            browser_use=history,
            answer_file=_optional(folder / "answer.txt"),
            reasoning_file=_optional(folder / "reasoning.txt"),
            task=spec.task if spec else None,
            run_id=name,
            max_body_chars=max_body_chars,
        )
        recording = _digest(p for p in (trace, har, history) if p is not None)
    except BenchInputError as exc:
        raise BenchInputError(f"scenario {name}: {exc}") from exc
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError, zipfile.BadZipFile) as exc:
        raise BenchInputError(f"scenario {name}: {type(exc).__name__}: {exc}") from exc
    return Scenario(
        name=name,
        run=run,
        expected=expected,
        spec=spec,
        generic_spec=generic_spec,
        recording=recording,
    )


def _digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()[:16]


def _optional(path: Path) -> Path | None:
    return path if path.is_file() else None
