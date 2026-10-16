"""Deciding `edit` predicates, including that the session changed only some files."""

from __future__ import annotations

from typing import Final

from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.sessions.checks.common import path_matches, result
from evidence_judge.sessions.checks.index import SessionIndex
from evidence_judge.sessions.edits import is_test_path
from evidence_judge.sessions.models import (
    CallStatus,
    Category,
    CommandEvent,
    OtherToolEvent,
    SessionEvent,
    SubagentEvent,
)
from evidence_judge.sessions.normalize import relative
from evidence_judge.sessions.predicates import EditPredicate
from evidence_judge.sessions.shell import READ_ONLY_PROGRAMS
from evidence_judge.sessions.shell.lexer import SAFE_TARGETS
from evidence_judge.sessions.shell.writes import SCRATCH


def _edit_matches(predicate: EditPredicate, path: str) -> bool:
    if predicate.test_files:
        return is_test_path(path)
    assert predicate.path is not None
    return path_matches(predicate.path, path)


_PATH_EXPLICIT: Final = frozenset(
    {"rm", "unlink", "mv", "cp", "touch", "mkdir", "rmdir", "ln", "chmod", "chown", "tee", "sed"}
)
_SUBCOMMAND_DEPENDENT: Final = frozenset(
    {"git", "find", "awk", "gawk", "curl", "pip", "pip3", "npm", "pnpm", "yarn", "bun", "cargo",
     "go", "uv"}
)  # fmt: skip
_TRANSPARENT: Final = _PATH_EXPLICIT | (READ_ONLY_PROGRAMS - _SUBCOMMAND_DEPENDENT)


def _names_its_paths(event: CommandEvent) -> bool:
    """True when every file the command can change is named on its command line."""
    analysis = event.analysis
    return (
        Category.UNKNOWN not in analysis.categories
        and analysis.parse_error is None
        and all(program in _TRANSPARENT for program in analysis.programs)
    )


def _project_path(path: str, cwd: str | None) -> str | None:
    """`path` relative to the session directory, or None for scratch space (`/tmp`, `/dev`)."""
    if path.startswith(SCRATCH) or path in SAFE_TARGETS:
        return None
    return relative(path, cwd)


def check_only(globs: tuple[str, ...], index: SessionIndex) -> PredicateResult:
    """Every file the session changed matches one of `globs`."""
    named = ", ".join(globs)

    def inside(path: str) -> bool:
        return any(path_matches(glob, path) for glob in globs)

    if edit := next((e for e in index.edits if not inside(e.path)), None):
        return result(
            Support.CONTRADICTED, f"{edit.id} changed {edit.path}, not only {named}", edit
        )
    unseen: list[SessionEvent] = []
    for _, event, _ in index.changes:
        if isinstance(event, SubagentEvent | OtherToolEvent):
            unseen.append(event)
        if not isinstance(event, CommandEvent):
            continue
        analysis = event.analysis
        paths = [
            p
            for raw in (*analysis.touched_paths, *analysis.deleted_paths)
            if (p := _project_path(raw, index.run.cwd)) is not None and not inside(p)
        ]
        if paths and _names_its_paths(event):
            return result(
                Support.CONTRADICTED, f"{event.id} changed {paths[0]}, not only {named}", event
            )
        if paths or analysis.opaque_write:
            unseen.append(event)
    if unseen:
        return result(
            Support.NO_EVIDENCE,
            f"{unseen[0].id} may have changed files the log does not name",
            unseen[0],
        )
    if not index.run.complete:
        return result(Support.NO_EVIDENCE, "the log is incomplete")
    return result(Support.SUPPORTED, f"every recorded change is to {named}")


def check_edit(predicate: EditPredicate, index: SessionIndex) -> PredicateResult:
    if predicate.only is not None:
        return check_only(predicate.only, index)
    target = predicate.path or "test files"
    kinds = {"add": ("add",), "modify": ("modify", "unknown"), "delete": ("delete",)}.get(
        predicate.change, ("add", "modify", "delete", "unknown")
    )
    edits = [e for e in index.edits if _edit_matches(predicate, e.path) and e.change in kinds]
    by_command = [
        c
        for c in index.commands
        if c.status is not CallStatus.DENIED
        and any(
            _edit_matches(predicate, p)
            for p in (
                c.analysis.deleted_paths
                if predicate.change == "delete"
                else c.analysis.touched_paths
            )
        )
    ]
    unseen = [
        e
        for _, e, _ in index.changes
        if isinstance(e, SubagentEvent | OtherToolEvent)
        or (isinstance(e, CommandEvent) and not _names_its_paths(e))
    ]
    if predicate.touched:
        if edits:
            return result(Support.SUPPORTED, f"{edits[-1].id} changed {edits[-1].path}", edits[-1])
        if by_command:
            return result(
                Support.SUPPORTED, f"{by_command[-1].id} changed {target}", by_command[-1]
            )
        if unseen:
            return result(
                Support.NO_EVIDENCE,
                f"no recorded edit of {target}, but {unseen[-1].id} may have changed files",
                unseen[-1],
            )
        return result(Support.CONTRADICTED, f"no recorded edit of {target}")
    if edits:
        return result(Support.CONTRADICTED, f"{edits[0].id} changed {edits[0].path}", edits[0])
    if by_command:
        return result(Support.CONTRADICTED, f"{by_command[0].id} changed {target}", by_command[0])
    if unseen or not index.run.complete:
        culprit = unseen[0] if unseen else None
        reason = (
            f"no recorded edit of {target}, but {culprit.id} may have changed files the log does "
            "not name"
            if culprit
            else f"no recorded edit of {target}, but the log is incomplete"
        )
        return result(Support.NO_EVIDENCE, reason, *([culprit] if culprit else []))
    return result(Support.SUPPORTED, f"no recorded edit or command changed {target}")
