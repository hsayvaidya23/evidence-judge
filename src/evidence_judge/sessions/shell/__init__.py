"""A small, conservative shell parser for the commands coding agents run.

It never executes anything. It splits a command line into simple commands joined by operators,
strips environment assignments, redirections and wrappers (`timeout`, `uv run`, `npx`,
`bash -c`, ...), and classifies each simple command: a test run (and which runner, and whether it
runs a subset), a build, a lint, a type check, a formatter, a read-only command, or one that may
change the workspace. Anything it cannot classify may change the workspace: being wrong in that
direction costs a NEEDS_REVIEW, never a false PASS.
"""

from __future__ import annotations

from evidence_judge.sessions.shell.classify import READ_ONLY_PROGRAMS, analyse
from evidence_judge.sessions.shell.env import regenerates_env
from evidence_judge.sessions.shell.guard_paths import (
    SESSION_STORE,
    guard_path_reason,
    hook_switch_reason,
)
from evidence_judge.sessions.shell.lexer import tokenize, unwrap_shell

__all__ = [
    "READ_ONLY_PROGRAMS",
    "SESSION_STORE",
    "analyse",
    "guard_path_reason",
    "hook_switch_reason",
    "regenerates_env",
    "tokenize",
    "unwrap_shell",
]
