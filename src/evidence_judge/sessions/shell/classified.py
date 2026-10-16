"""What one simple command was classified as."""

from __future__ import annotations

from dataclasses import dataclass, field

from evidence_judge.sessions.models import Category, CommandAnalysis


@dataclass(slots=True)
class Classified:
    category: Category
    program: str
    runner: str | None = None
    filters: list[str] = field(default_factory=list[str])
    collect_only: bool = False
    mutation: str | None = None
    deleted: list[str] = field(default_factory=list[str])
    touched: list[str] | None = None
    """Files the program writes, when its command line names them all; None to take them from
    its arguments."""
    opaque: str | None = None
    """Why the program may write files its command line does not name."""
    paths: list[str] | None = None
    """The paths a test run selects when plain paths are its only filters."""
    nested: CommandAnalysis | None = None
    run_env: list[str] = field(default_factory=list[str])
    """Variables set for a test run on its own command line (`CI=true pytest`)."""
    relocations: list[str] = field(default_factory=list[str])
    """Directories the run moves to on its own (`make -C dir`, `uv run --directory dir`)."""
    config_files: list[str] = field(default_factory=list[str])
    """Files the run reads as its test configuration (`make -f rules`)."""
    writes_after: bool = False
    """The run writes files its own check does not cover (`pytest --log-file=calc.py`)."""
    stop_early: str | None = None
    """Why a failing run may stop before running every test it selected (`pytest -x`)."""
