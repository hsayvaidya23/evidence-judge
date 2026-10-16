"""A forgery benchmark for the session judge.

Starting from real sessions in which the tests did not pass (or were never shown to pass for
the whole suite), it forges success two ways, as the browser forgery lab does: by rewriting
only the final answer, and by appending the tool calls an agent would use to fake a green run
(masking the exit status, echoing a summary, skipping or deleting tests, filtering the run,
shadowing the runner, rewriting the transcript). Every forgery must come out as anything but
PASS. Honest sessions are controls with known outcomes.

    uv run python -m evidence_judge.sessions.bench tests/fixtures/sessions
"""

from __future__ import annotations

from evidence_judge.sessions.bench.cases import Case, cases
from evidence_judge.sessions.bench.controls import (
    CHECKED_ANSWERS,
    CONTROLS,
    ONLY_ANSWERS,
    PARTIAL_FIX_ANSWERS,
    REWORDED,
    WRAPPER_CONTROLS,
)
from evidence_judge.sessions.bench.projects import PROJECTS, Project
from evidence_judge.sessions.bench.pytest_tricks import PARTIAL_RED, TRICKS
from evidence_judge.sessions.bench.run import (
    BenchResult,
    KindResult,
    main,
    run_benchmark,
    to_markdown,
)

__all__ = [
    "CHECKED_ANSWERS",
    "CONTROLS",
    "ONLY_ANSWERS",
    "PARTIAL_FIX_ANSWERS",
    "PARTIAL_RED",
    "PROJECTS",
    "REWORDED",
    "TRICKS",
    "WRAPPER_CONTROLS",
    "BenchResult",
    "Case",
    "KindResult",
    "Project",
    "cases",
    "main",
    "run_benchmark",
    "to_markdown",
]
