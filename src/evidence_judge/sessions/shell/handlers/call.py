"""The simple command a per-program handler classifies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from evidence_judge.sessions.shell.classified import Classified


@dataclass(frozen=True, slots=True)
class Call:
    """One simple command, unwrapped, as the per-program handlers see it."""

    words: list[str]
    prog: str
    args: list[str]
    env: dict[str, str]
    depth: int
    cwd: str | None

    @property
    def sub(self) -> str:
        return self.args[0] if self.args else ""


Handler = Callable[[Call], Classified | None]
"""Classifies a command of the programs it is registered for, or None to leave it to the
read-only, version-flag and inline-Python fallbacks."""
