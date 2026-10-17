"""Deciding `edit` predicates, including that the session changed only some files."""

from __future__ import annotations

import posixpath
from typing import Final

from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.sessions.checks.common import path_matches, project_path, result
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
from evidence_judge.sessions.predicates import EditPredicate
from evidence_judge.sessions.shell import READ_ONLY_PROGRAMS
from evidence_judge.sessions.shell.lexer import SAFE_TARGETS
from evidence_judge.sessions.shell.writes import SCRATCH


def _placements(
    path: str, root: str | None, here: str | None = None, anchor: str | None = None
) -> list[str]:
    """Where `path` may lie in the project: its one project-relative form, or nothing when it
    lies outside. When the session directory is unknown, an absolute path may lie at every
    suffix; with `anchor`, only at those whose dropped prefix is `anchor` or one of its
    ancestors (other than `/`)."""
    if (rel := project_path(path, root, here)) is not None:
        return [rel]
    if not _unplaceable(path, root):
        return []
    parts = [part for part in posixpath.normpath(path).split("/") if part]
    if anchor is None:
        return ["/".join(parts[i:]) for i in range(len(parts))]
    above = [part for part in anchor.split("/") if part]
    return ["/".join(parts[i:]) for i in range(1, len(above) + 1)]


def _edit_matches(
    predicate: EditPredicate,
    path: str,
    root: str | None,
    here: str | None = None,
    anchor: str | None = None,
) -> bool:
    places = _placements(path, root, here, anchor)
    if predicate.test_files:
        return any(is_test_path(p) for p in places)
    assert predicate.path is not None
    glob = predicate.path
    return any(path_matches(glob, p) for p in places)


def _anchor(index: SessionIndex) -> str:
    """The deepest directory holding every absolute path the session changed that cannot be
    placed, or `/` when they lie in unrelated trees. The session directory is this directory
    or one of its ancestors only if every change is in the project, so a path is placed only
    below a root that holds all of them."""
    root = index.run.cwd
    paths = {
        posixpath.normpath(p)
        for p in (
            *(e.path for e in index.edits),
            *(
                p
                for c in index.commands
                if c.status is not CallStatus.DENIED
                for p in (*c.analysis.touched_paths, *c.analysis.deleted_paths)
            ),
        )
        if _unplaceable(p, root)
    }
    return posixpath.commonpath([posixpath.dirname(p) for p in paths]) if paths else "/"


def _unplaceable(path: str, root: str | None) -> bool:
    """An absolute path in a session whose directory is unknown: it may or may not lie in the
    project."""
    return root is None and posixpath.isabs(path) and not _scratch(path)


def _inferred(paths: list[str], root: str | None) -> str:
    if all(_unplaceable(p, root) for p in paths):
        return (
            "; placed by an inferred directory, as the session directory is unknown; set `cwd` "
            "in the spec for strict placement"
        )
    return ""


def _scratch(path: str) -> bool:
    return path.startswith(SCRATCH) or path in SAFE_TARGETS


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


def check_only(globs: tuple[str, ...], index: SessionIndex) -> PredicateResult:
    """Every file the session changed matches one of `globs`; a change outside the project
    matches none of them."""
    named = ", ".join(globs)
    root = index.run.cwd

    def inside(path: str, here: str | None = None) -> bool:
        return _scratch(path) or any(path_matches(glob, path, root, here) for glob in globs)

    def may_be_inside(path: str, here: str | None = None) -> bool:
        return any(path_matches(glob, p) for glob in globs for p in _placements(path, root, here))

    outside = [e for e in index.edits if not inside(e.path)]
    if edit := next((e for e in outside if not may_be_inside(e.path)), None):
        return result(
            Support.CONTRADICTED, f"{edit.id} changed {edit.path}, not only {named}", edit
        )
    unseen: list[SessionEvent] = list(outside)
    for _, event, _ in index.changes:
        if isinstance(event, SubagentEvent | OtherToolEvent):
            unseen.append(event)
        if not isinstance(event, CommandEvent):
            continue
        analysis = event.analysis
        paths = [
            p
            for p in (*analysis.touched_paths, *analysis.deleted_paths)
            if not inside(p, event.cwd)
        ]
        if any(not may_be_inside(p, event.cwd) for p in paths) and _names_its_paths(event):
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


def _command_paths(command: CommandEvent, predicate: EditPredicate) -> list[str]:
    analysis = command.analysis
    return analysis.deleted_paths if predicate.change == "delete" else analysis.touched_paths


def check_edit(predicate: EditPredicate, index: SessionIndex) -> PredicateResult:
    if predicate.only is not None:
        return check_only(predicate.only, index)
    target = predicate.path or "test files"
    kinds = {"add": ("add",), "modify": ("modify", "unknown"), "delete": ("delete",)}.get(
        predicate.change, ("add", "modify", "delete", "unknown")
    )
    root = index.run.cwd
    edits = [e for e in index.edits if _edit_matches(predicate, e.path, root) and e.change in kinds]
    by_command = [
        c
        for c in index.commands
        if c.status is not CallStatus.DENIED
        and any(_edit_matches(predicate, p, root, c.cwd) for p in _command_paths(c, predicate))
    ]
    unseen = [
        e
        for _, e, _ in index.changes
        if isinstance(e, SubagentEvent | OtherToolEvent)
        or (isinstance(e, CommandEvent) and not _names_its_paths(e))
    ]
    if predicate.touched:
        anchor = _anchor(index)
        placed = [e for e in edits if _edit_matches(predicate, e.path, root, anchor=anchor)]
        if placed:
            edit = placed[-1]
            reason = f"{edit.id} changed {edit.path}{_inferred([edit.path], root)}"
            return result(Support.SUPPORTED, reason, edit)
        placed_by = {
            c.id: matched
            for c in by_command
            if (
                matched := [
                    p
                    for p in _command_paths(c, predicate)
                    if _edit_matches(predicate, p, root, c.cwd, anchor)
                ]
            )
        }
        if command := next((c for c in reversed(by_command) if c.id in placed_by), None):
            reason = f"{command.id} changed {target}{_inferred(placed_by[command.id], root)}"
            return result(Support.SUPPORTED, reason, command)
        if doubt := [*edits, *by_command]:
            return result(
                Support.NO_EVIDENCE,
                f"{doubt[-1].id} changed a file named like {target}, but the session directory "
                "is unknown and other changes place it outside the project",
                doubt[-1],
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
