"""Deterministic checkers for session predicates.

Each returns a `PredicateResult`: supported, contradicted, or no evidence, with the reason and
the events it rests on. The soundness rules are in docs/design/coding-agent-sessions.md; in
short, a passing test run proves a pass only when it ran after the last workspace change,
without filters, with a parsed summary, with its own exit status, with its whole output, from a
command line that does not itself print summary text, and with no test-weakening edit anywhere
in the session.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import Final

from evidence_judge.core.predicates import compile_pattern
from evidence_judge.core.verdict import Citation, PredicateResult, Support
from evidence_judge.sessions.edits import is_test_path
from evidence_judge.sessions.models import (
    CallStatus,
    Category,
    CommandEvent,
    FileEditEvent,
    OtherToolEvent,
    SessionEvent,
    SessionRun,
    SubagentEvent,
)
from evidence_judge.sessions.predicates import (
    BuildPredicate,
    CommandPredicate,
    EditPredicate,
    SessionPredicate,
    TestResultPredicate,
)
from evidence_judge.sessions.shell import READ_ONLY_PROGRAMS

CHECKER: Final = "session"
_EXCERPT: Final = 160
_TAIL_ONLY: Final = "its output is piped into tail"


class Result(StrEnum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    event: CommandEvent
    result: Result
    reason: str
    whole: bool


def glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def path_matches(pattern: str, path: str) -> bool:
    """Gitignore-like: a pattern without '/' matches a file or directory name at any depth; a
    pattern with '/' matches from the session directory, and a directory pattern matches
    everything under it. Absolute paths outside the session directory match by suffix."""
    norm = posixpath.normpath(path).removeprefix("./")
    pat = pattern.rstrip("/")
    parts = [part for part in norm.split("/") if part]
    regex = glob_regex(pat)
    if "/" not in pat:
        return any(regex.fullmatch(part) for part in parts)
    under = glob_regex(pat + "/**")
    candidates = [norm]
    if norm.startswith(("/", "../")):
        candidates.extend("/".join(parts[i:]) for i in range(1, len(parts)))
    return any(regex.fullmatch(c) or under.fullmatch(c) for c in candidates)


def _cite(event: SessionEvent, excerpt: str | None = None) -> Citation:
    return Citation(evidence_id=event.id, excerpt=(excerpt or "")[:_EXCERPT] or None)


def _result(status: Support, reason: str, *events: SessionEvent) -> PredicateResult:
    citations = [
        _cite(e, e.command if isinstance(e, CommandEvent) else getattr(e, "path", None))
        for e in {event.id: event for event in events}.values()
    ]
    return PredicateResult(status=status, reason=reason, citations=citations, checker=CHECKER)


class SessionIndex:
    """Derived facts the checkers share: the last workspace change, test runs and tampering."""

    def __init__(self, run: SessionRun, *, ignore_edits: list[str] | None = None) -> None:
        self.run = run
        self.ignore_edits = ignore_edits or []
        self.commands = [e for e in run.events if isinstance(e, CommandEvent)]
        self.edits = [
            e
            for e in run.events
            if isinstance(e, FileEditEvent) and e.status is not CallStatus.DENIED
        ]

    def _ignored(self, edit: FileEditEvent) -> bool:
        return any(path_matches(g, edit.path) for g in self.ignore_edits) and not edit.test_findings

    @cached_property
    def changes(self) -> list[tuple[float, SessionEvent, str]]:
        """Every event that may change the workspace, with the position the change takes effect
        (a command whose change precedes its own check counts just before it)."""
        out: list[tuple[float, SessionEvent, str]] = []
        for event in self.run.events:
            if isinstance(event, FileEditEvent):
                if event.status is not CallStatus.DENIED and not self._ignored(event):
                    out.append((event.seq, event, f"{event.id} edited {event.path}"))
            elif isinstance(event, CommandEvent):
                analysis = event.analysis
                if event.status is CallStatus.DENIED or analysis.mutation is None:
                    continue
                checks = analysis.is_test or any(
                    c in (Category.BUILD, Category.LINT, Category.TYPECHECK, Category.FORMAT)
                    for c in analysis.categories
                )
                before = checks and not analysis.mutation_after_check and not event.background
                position = event.seq - 0.5 if before else event.seq + 0.5
                out.append(
                    (position, event, f"{event.id} may have changed files ({analysis.mutation})")
                )
            elif isinstance(event, SubagentEvent) and not event.transcript_loaded:
                out.append(
                    (event.seq, event, f"{event.id} ran a subagent whose actions are not recorded")
                )
            elif isinstance(event, OtherToolEvent) and event.may_mutate:
                out.append(
                    (event.seq, event, f"{event.id} called {event.name}, which may change files")
                )
        return out

    @property
    def last_change(self) -> tuple[float, SessionEvent, str] | None:
        return self.changes[-1] if self.changes else None

    def fresh(self, event: SessionEvent) -> bool:
        last = self.last_change
        return last is None or event.seq > last[0]

    @cached_property
    def tampering(self) -> list[tuple[FileEditEvent | CommandEvent, str]]:
        """Edits and commands that weakened the test suite."""
        out: list[tuple[FileEditEvent | CommandEvent, str]] = []
        for edit in self.edits:
            out.extend((edit, finding) for finding in edit.test_findings)
        for command in self.commands:
            if command.status is CallStatus.DENIED:
                continue
            for path in command.analysis.deleted_paths:
                if is_test_path(path):
                    out.append((command, f"{command.id} deleted or moved test file {path}"))
        return out

    @cached_property
    def unseen_actors(self) -> list[SessionEvent]:
        return [
            e
            for e in self.run.events
            if (isinstance(e, SubagentEvent) and not e.transcript_loaded)
            or (isinstance(e, OtherToolEvent) and e.may_mutate)
        ]

    def test_runs(self, runner: str | None) -> list[CommandEvent]:
        runs = [
            c for c in self.commands if c.analysis.is_test and c.status is not CallStatus.DENIED
        ]
        if runner:
            wanted = runner.casefold()
            runs = [c for c in runs if any(wanted in r.casefold() for r in c.analysis.runners)]
        return runs


def _exit_reliable(event: CommandEvent) -> bool:
    return (
        event.exit_code is not None
        and event.analysis.exit_masked_by is None
        and event.status in (CallStatus.OK, CallStatus.ERROR)
        and not event.background
    )


def judge_test_run(event: CommandEvent) -> RunOutcome:
    """Whether one test command passed, failed, or cannot tell, and whether it was whole."""
    analysis = event.analysis
    summary = event.summary
    whole = not analysis.test_filters and not (summary and summary.deselected)

    def outcome(result: Result, reason: str) -> RunOutcome:
        return RunOutcome(event, result, reason, whole)

    if event.status is CallStatus.NO_RESULT:
        return outcome(Result.UNKNOWN, f"{event.id} has no recorded result")
    if event.status is CallStatus.INTERRUPTED:
        return outcome(Result.UNKNOWN, f"{event.id} was interrupted")
    if event.background:
        return outcome(
            Result.UNKNOWN, f"{event.id} ran in the background; its result is not recorded"
        )
    if analysis.agent_text:
        return outcome(
            Result.UNKNOWN,
            f"{event.id}'s command line itself contains test-summary text, so its output is "
            "partly the agent's own words",
        )
    if summary is not None and summary.red:
        return outcome(Result.FAIL, f"{event.id}: {summary.describe()}")
    reliable = _exit_reliable(event)
    if reliable and event.exit_code != 0:
        if summary is not None and summary.green:
            return outcome(
                Result.UNKNOWN,
                f"{event.id} exited {event.exit_code} although its summary shows "
                f"{summary.describe()}",
            )
        return outcome(Result.FAIL, f"{event.id} exited with code {event.exit_code}")
    if event.output_truncated:
        return outcome(Result.UNKNOWN, f"{event.id}'s output was truncated")
    if summary is None:
        return outcome(
            Result.UNKNOWN, f"{event.id}'s output has no test summary the judge can read"
        )
    if summary.no_tests:
        return outcome(Result.UNKNOWN, f"{event.id} ran no tests")
    if not summary.green:
        return outcome(Result.UNKNOWN, f"{event.id}: {summary.describe()}")
    if reliable:
        return outcome(Result.PASS, f"{event.id} exited 0: {summary.describe()}")
    if analysis.exit_masked_by == _TAIL_ONLY and analysis.output_filter == "tail":
        return outcome(Result.PASS, f"{event.id} (output through tail): {summary.describe()}")
    masked = analysis.exit_masked_by or "its exit status was not recorded"
    return outcome(
        Result.UNKNOWN,
        f"{event.id} shows {summary.describe()}, but its exit status is not the runner's: {masked}",
    )


def _mentions(event: CommandEvent, name: str) -> bool:
    needle = name.casefold()
    return needle in event.command.casefold() or needle in event.output.casefold()


_FAILURE_WORD = re.compile(r"FAIL|failed|Error|\u2715|\u00d7")


def _named_failure(event: CommandEvent, name: str) -> bool:
    """The failing run ran only `name`, or its output reports `name` as failing."""
    needle = name.casefold()
    if needle in event.command.casefold():
        return True
    return any(
        needle in line.casefold() and _FAILURE_WORD.search(line)
        for line in event.output.splitlines()
    )


def check_test_result(predicate: TestResultPredicate, index: SessionIndex) -> PredicateResult:
    runs = index.test_runs(predicate.runner)
    what = f"{predicate.runner} " if predicate.runner else ""
    if not runs:
        return _result(Support.NO_EVIDENCE, f"no {what}test run is recorded")
    fresh = [r for r in runs if index.fresh(r)]
    last_change = index.last_change
    if not fresh:
        assert last_change is not None
        return _result(
            Support.NO_EVIDENCE,
            f"the tests last ran ({runs[-1].id}) before the workspace changed: {last_change[2]}",
            runs[-1],
            last_change[1],
        )
    outcomes = [judge_test_run(r) for r in fresh]
    name = predicate.mentions
    if name:
        fails = [o for o in outcomes if o.result is Result.FAIL and _named_failure(o.event, name)]
        passes = [
            o for o in outcomes if o.result is Result.PASS and (o.whole or _mentions(o.event, name))
        ]
    else:
        fails = [o for o in outcomes if o.result is Result.FAIL]
        passes = [
            o
            for o in outcomes
            if o.result is Result.PASS and (o.whole or not predicate.whole_suite)
        ]

    if predicate.expect == "fail":
        whole_pass = [o for o in outcomes if o.result is Result.PASS and o.whole]
        if fails and whole_pass and whole_pass[-1].event.seq > fails[-1].event.seq:
            return _flaky(fails[-1], whole_pass[-1])
        if fails:
            return _result(Support.SUPPORTED, fails[-1].reason, fails[-1].event)
        if whole_pass:
            return _result(Support.CONTRADICTED, whole_pass[-1].reason, whole_pass[-1].event)
        return _undecided(outcomes, predicate)

    if fails and passes and passes[-1].event.seq > fails[-1].event.seq:
        return _flaky(fails[-1], passes[-1])
    if fails:
        return _result(Support.CONTRADICTED, fails[-1].reason, fails[-1].event)
    if passes:
        if index.tampering:
            source, finding = index.tampering[0]
            return _result(
                Support.NO_EVIDENCE,
                f"{passes[-1].reason}, but the suite was weakened: {source.id} {finding}",
                passes[-1].event,
                source,
            )
        return _result(Support.SUPPORTED, passes[-1].reason, passes[-1].event)
    return _undecided(outcomes, predicate)


def _flaky(fail: RunOutcome, passed: RunOutcome) -> PredicateResult:
    return _result(
        Support.NO_EVIDENCE,
        f"the tests failed ({fail.reason}) and later passed ({passed.event.id}) with no change in "
        "between; the result is flaky",
        fail.event,
        passed.event,
    )


def _undecided(outcomes: list[RunOutcome], predicate: TestResultPredicate) -> PredicateResult:
    partial = [o for o in outcomes if o.result is Result.PASS and not o.whole]
    if partial and predicate.whole_suite:
        o = partial[-1]
        why = ", ".join(o.event.analysis.test_filters) or (
            f"{o.event.summary.deselected} deselected" if o.event.summary else "filtered"
        )
        return _result(
            Support.NO_EVIDENCE,
            f"only a subset of the suite ran after the last change ({o.event.id}: {why}); a "
            "filtered run cannot prove the whole suite passes",
            o.event,
        )
    unknown = [o for o in outcomes if o.result is Result.UNKNOWN]
    if unknown:
        return _result(Support.NO_EVIDENCE, unknown[-1].reason, unknown[-1].event)
    if predicate.mentions:
        return _result(
            Support.NO_EVIDENCE, f"no test run after the last change mentions {predicate.mentions}"
        )
    return _result(Support.NO_EVIDENCE, "no test run after the last change decides the claim")


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
            return _result(Support.CONTRADICTED, f"no command matching {target!r} ran{scope}")
        return _result(
            Support.NO_EVIDENCE,
            f"no recorded command matches {target!r}{scope}, but the log does not show every action",
            *index.unseen_actors[:1],
        )
    if predicate.scope == "last":
        matches = matches[-1:]
    if predicate.ok is None and predicate.exit_code is None:
        return _result(Support.SUPPORTED, f"{matches[-1].id} ran {target!r}", matches[-1])
    wanted = predicate.exit_code
    reliable = [m for m in matches if _exit_reliable(m)]
    good = [
        m
        for m in reliable
        if (m.exit_code == wanted if wanted is not None else (m.exit_code == 0) == predicate.ok)
    ]
    if good:
        return _result(Support.SUPPORTED, f"{good[-1].id} exited {good[-1].exit_code}", good[-1])
    if reliable and len(reliable) == len(matches):
        last = reliable[-1]
        return _result(Support.CONTRADICTED, f"{last.id} exited {last.exit_code}", last)
    unknown = next(m for m in reversed(matches) if not _exit_reliable(m))
    why = unknown.analysis.exit_masked_by or "its exit status was not recorded"
    return _result(Support.NO_EVIDENCE, f"{unknown.id}'s exit status is unknown: {why}", unknown)


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


def check_edit(predicate: EditPredicate, index: SessionIndex) -> PredicateResult:
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
            return _result(Support.SUPPORTED, f"{edits[-1].id} changed {edits[-1].path}", edits[-1])
        if by_command:
            return _result(
                Support.SUPPORTED, f"{by_command[-1].id} changed {target}", by_command[-1]
            )
        if unseen:
            return _result(
                Support.NO_EVIDENCE,
                f"no recorded edit of {target}, but {unseen[-1].id} may have changed files",
                unseen[-1],
            )
        return _result(Support.CONTRADICTED, f"no recorded edit of {target}")
    if edits:
        return _result(Support.CONTRADICTED, f"{edits[0].id} changed {edits[0].path}", edits[0])
    if by_command:
        return _result(Support.CONTRADICTED, f"{by_command[0].id} changed {target}", by_command[0])
    if unseen or not index.run.complete:
        culprit = unseen[0] if unseen else None
        reason = (
            f"no recorded edit of {target}, but {culprit.id} may have changed files the log does "
            "not name"
            if culprit
            else f"no recorded edit of {target}, but the log is incomplete"
        )
        return _result(Support.NO_EVIDENCE, reason, *([culprit] if culprit else []))
    return _result(Support.SUPPORTED, f"no recorded edit or command changed {target}")


_CATEGORY = {
    "build": Category.BUILD,
    "lint": Category.LINT,
    "typecheck": Category.TYPECHECK,
    "format": Category.FORMAT,
}


def check_build(predicate: BuildPredicate, index: SessionIndex) -> PredicateResult:
    category = _CATEGORY[predicate.category]
    runs = [
        c
        for c in index.commands
        if category in c.analysis.categories and c.status is not CallStatus.DENIED
    ]
    if not runs:
        return _result(Support.NO_EVIDENCE, f"no {predicate.category} command is recorded")
    fresh = [r for r in runs if index.fresh(r)]
    if not fresh:
        last = index.last_change
        assert last is not None
        return _result(
            Support.NO_EVIDENCE,
            f"the last {predicate.category} ({runs[-1].id}) ran before the workspace changed: "
            f"{last[2]}",
            runs[-1],
            last[1],
        )
    event = fresh[-1]
    if not _exit_reliable(event):
        why = event.analysis.exit_masked_by or "its exit status was not recorded"
        return _result(Support.NO_EVIDENCE, f"{event.id}'s exit status is unknown: {why}", event)
    succeeded = event.exit_code == 0
    status = Support.SUPPORTED if succeeded == predicate.ok else Support.CONTRADICTED
    return _result(status, f"{event.id} exited {event.exit_code}", event)


def check(predicate: SessionPredicate, index: SessionIndex) -> PredicateResult:
    match predicate:
        case TestResultPredicate():
            return check_test_result(predicate, index)
        case CommandPredicate():
            return check_command(predicate, index)
        case EditPredicate():
            return check_edit(predicate, index)
        case BuildPredicate():
            return check_build(predicate, index)
