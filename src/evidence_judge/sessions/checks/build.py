"""Deciding `build` predicates: build, lint, type-check and format runs."""

from __future__ import annotations

from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.sessions.checks.common import Result, RunOutcome, result
from evidence_judge.sessions.checks.index import SessionIndex
from evidence_judge.sessions.checks.runs import compile_failure, exit_reliable
from evidence_judge.sessions.edits import SUPPRESSES
from evidence_judge.sessions.models import CallStatus, Category, CommandEvent, FileEditEvent
from evidence_judge.sessions.predicates import BuildPredicate

_CATEGORY = {
    "build": Category.BUILD,
    "lint": Category.LINT,
    "typecheck": Category.TYPECHECK,
    "format": Category.FORMAT,
}


def judge_check_run(event: CommandEvent, index: SessionIndex) -> RunOutcome:
    """Whether one build, lint, type-check or format run passed, failed or cannot tell, and why
    it does not cover the whole project, if it does not: errors its own output counts fail it
    whatever its exit status; a pass needs the tool's own exit status 0."""
    where = index.run_dir(event)
    scope = [*event.analysis.check_filters]
    if where != ".":
        scope.append(
            f"ran in {where or 'a directory only the shell knows'}, not the session directory"
        )
    partial = ", ".join(scope) or None
    if event.check_errors:
        return RunOutcome(
            event, Result.FAIL, f"{event.id}'s output counts {event.check_errors} error(s)", partial
        )
    if not exit_reliable(event):
        why = event.analysis.exit_masked_by or "its exit status was not recorded"
        return RunOutcome(
            event, Result.UNKNOWN, f"{event.id}'s exit status is unknown: {why}", partial
        )
    if event.exit_code != 0:
        return RunOutcome(event, Result.FAIL, f"{event.id} exited {event.exit_code}", partial)
    return RunOutcome(event, Result.PASS, f"{event.id} exited 0", partial)


def _suppressed(category: str, index: SessionIndex) -> FileEditEvent | None:
    """An edit that added a comment switching `category` off for a line or a file."""
    return next(
        (e for e in index.edits if any(SUPPRESSES.get(s) == category for s in e.suppressions)),
        None,
    )


def _test_build_failed(index: SessionIndex) -> PredicateResult | None:
    """'The build still fails' with no build run after the last change: the last test run after
    it failed to compile."""
    runs = [r for r in index.test_runs(None) if index.fresh(r)]
    if not runs or (line := compile_failure(runs[-1])) is None:
        return None
    return result(Support.SUPPORTED, f"{runs[-1].id} did not compile: {line}", runs[-1])


def check_build(predicate: BuildPredicate, index: SessionIndex) -> PredicateResult:
    category = _CATEGORY[predicate.category]
    runs = [
        c
        for c in index.commands
        if category in c.analysis.categories and c.status is not CallStatus.DENIED
    ]
    if (
        predicate.category == "build"
        and not predicate.ok
        and not any(map(index.fresh, runs))
        and (compiled := _test_build_failed(index))
    ):
        return compiled
    if not runs:
        return result(Support.NO_EVIDENCE, f"no {predicate.category} command is recorded")
    fresh = [r for r in runs if index.fresh(r)]
    if not fresh:
        last = index.last_change
        assert last is not None
        return result(
            Support.NO_EVIDENCE,
            f"the last {predicate.category} ({runs[-1].id}) ran before the workspace changed: "
            f"{last[2]}",
            runs[-1],
            last[1],
        )
    outcomes = [judge_check_run(event, index) for event in fresh]
    whole = [o for o in outcomes if o.whole and o.result is not Result.UNKNOWN]
    failed = [o for o in outcomes if o.result is Result.FAIL]
    if whole and not any(o.event.seq > whole[-1].event.seq for o in failed):
        deciding = whole[-1]
    elif failed:
        deciding = failed[-1]
    else:
        last = outcomes[-1]
        why = (
            f"{last.event.id} checked only part of the project ({last.partial})"
            if last.partial
            else last.reason
        )
        return result(Support.NO_EVIDENCE, why, last.event)
    succeeded = deciding.result is Result.PASS
    if succeeded and predicate.ok and (edit := _suppressed(predicate.category, index)):
        marks = ", ".join(s for s in edit.suppressions if SUPPRESSES.get(s) == predicate.category)
        return result(
            Support.NO_EVIDENCE,
            f"{deciding.reason}, but {edit.id} added {marks} to {edit.path}, which hides "
            f"{predicate.category} findings",
            deciding.event,
            edit,
        )
    status = Support.SUPPORTED if succeeded == predicate.ok else Support.CONTRADICTED
    return result(status, deciding.reason, deciding.event)
