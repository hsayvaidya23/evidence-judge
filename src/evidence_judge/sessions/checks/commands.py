"""Deciding `command` predicates."""

from __future__ import annotations

import re

from evidence_judge.core.predicates import compile_pattern
from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.sessions.checks.common import result
from evidence_judge.sessions.checks.index import SessionIndex
from evidence_judge.sessions.checks.runs import exit_reliable
from evidence_judge.sessions.models import CallStatus, Category, CommandEvent
from evidence_judge.sessions.predicates import CommandPredicate
from evidence_judge.sessions.shell.env import INSTALLERS, installs_from_manifest


def _command_matches(predicate: CommandPredicate, event: CommandEvent) -> bool:
    if predicate.category is not None:
        return Category(predicate.category) in event.analysis.categories
    if predicate.contains is not None:
        return _squash(predicate.contains) in _squash(event.command)
    assert predicate.regex is not None
    return compile_pattern(predicate.regex).search(event.command) is not None


_SPACES = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _SPACES.sub(" ", text).strip().casefold()


def _complete_log(index: SessionIndex) -> bool:
    return index.run.complete and not index.unseen_actors


def _manifest_install(
    predicate: CommandPredicate, index: SessionIndex, after: CommandEvent | None
) -> PredicateResult | None:
    """No evidence for a claimed install the command line does not name, when a later install
    from a requirements or lock file succeeded: that file may list the package."""
    if not (predicate.ok and predicate.regex and predicate.regex.startswith(INSTALLERS)):
        return None
    for event in reversed(index.commands):
        if after is not None and event.seq <= after.seq:
            return None
        if predicate.scope == "after_last_edit" and not index.fresh(event):
            continue
        if exit_reliable(event) and event.exit_code == 0 and installs_from_manifest(event.command):
            return result(
                Support.NO_EVIDENCE,
                f"{event.id} installed from a requirements or lock file the judge cannot read, "
                "which may list the package",
                event,
            )
    return None


def check_command(predicate: CommandPredicate, index: SessionIndex) -> PredicateResult:
    matches = [
        c
        for c in index.commands
        if c.status is not CallStatus.DENIED and _command_matches(predicate, c)
    ]
    target = predicate.contains or predicate.category or f"/{predicate.regex}/"
    if predicate.scope == "after_last_edit":
        matches = [m for m in matches if index.fresh(m)]
    if not matches:
        scope = " after the last change" if predicate.scope == "after_last_edit" else ""
        if _complete_log(index):
            if manifest := _manifest_install(predicate, index, None):
                return manifest
            return result(Support.CONTRADICTED, f"no command matching {target!r} ran{scope}")
        return result(
            Support.NO_EVIDENCE,
            f"no recorded command matches {target!r}{scope}, but the log does not show every action",
            *index.unseen_actors[:1],
        )
    if predicate.scope == "last":
        matches = matches[-1:]
    if predicate.ok is None and predicate.exit_code is None:
        return result(Support.SUPPORTED, f"{matches[-1].id} ran {target!r}", matches[-1])
    wanted = predicate.exit_code
    reliable = [m for m in matches if exit_reliable(m)]
    good = [
        m
        for m in reliable
        if (m.exit_code == wanted if wanted is not None else (m.exit_code == 0) == predicate.ok)
    ]
    if good:
        return result(Support.SUPPORTED, f"{good[-1].id} exited {good[-1].exit_code}", good[-1])
    if reliable and len(reliable) == len(matches):
        last = reliable[-1]
        if manifest := _manifest_install(predicate, index, last):
            return manifest
        return result(Support.CONTRADICTED, f"{last.id} exited {last.exit_code}", last)
    unknown = next(m for m in reversed(matches) if not exit_reliable(m))
    why = unknown.analysis.exit_masked_by or "its exit status was not recorded"
    return result(Support.NO_EVIDENCE, f"{unknown.id}'s exit status is unknown: {why}", unknown)
