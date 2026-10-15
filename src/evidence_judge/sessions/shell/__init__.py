"""A small, conservative shell parser for the commands coding agents run."""

from __future__ import annotations

from evidence_judge.sessions.shell.classify import (
    READ_ONLY_PROGRAMS,
    SESSION_STORE,
    analyse,
    guard_path_reason,
    hook_switch_reason,
    regenerates_env,
    tokenize,
    unwrap_shell,
)

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
