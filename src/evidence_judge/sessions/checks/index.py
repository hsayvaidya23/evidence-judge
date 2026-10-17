"""`SessionIndex`: the facts the checkers derive from a whole session."""

from __future__ import annotations

import math
import posixpath
import re
from functools import cached_property
from typing import Final

from evidence_judge.sessions.checks.common import Result, RunOutcome, path_matches
from evidence_judge.sessions.checks.runs import (
    every_failing_test,
    incomplete,
    judge_test_run,
    stopped_early,
    suite_size,
)
from evidence_judge.sessions.edits import (
    git_metadata_reason,
    is_config_path,
    is_harness_path,
    is_test_config,
    is_test_path,
    session_config_reason,
)
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
from evidence_judge.sessions.normalize import relative
from evidence_judge.sessions.rerun import RerunResult, RerunStatus
from evidence_judge.sessions.shell import regenerates_env
from evidence_judge.sessions.shell.wrappers import may_redefine, wrapper_reads

_SHELL_STATE: Final = re.compile(
    r"\A(?:defines a shell function|(?:un)?alias |hash -p|(?:export|declare|typeset|readonly|set"
    r"|sets) )"
)
"""Harness changes that live only in the agent's shell (aliases, functions, exported
variables); a re-run in the judge's own environment never sees them. Sourced files and `eval`
can also write files, so they stay findings."""
_PER_TEST: Final = frozenset({"go test -v"})
"""Summaries that count test functions where the same runner otherwise counts packages: a run
is compared only with earlier runs counting the same unit."""


_ScopedPass = tuple[RunOutcome, list[str], int]
"""A passing run, the session-relative paths it selected, and how many tests it ran."""


def _touched_path_finding(path: str, configs: dict[str, str]) -> str | None:
    """How a command that wrote `path` may have weakened the tests, if it may have."""
    if why := git_metadata_reason(path) or session_config_reason(path):
        return why
    if reader := configs.get(posixpath.basename(path)):
        return f"changed {path}, which {reader} reads as its test configuration"
    if is_harness_path(path) or is_test_config(path):
        return f"changed test harness file {path}"
    if is_test_path(path):
        return f"wrote test file {path} from the shell"
    if is_config_path(path):
        return f"changed {path}, which can configure the tests"
    return None


class SessionIndex:
    """Derived facts the checkers share: the last workspace change, test runs and tampering."""

    def __init__(self, run: SessionRun, *, ignore_edits: list[str] | None = None) -> None:
        self.run = run
        self.ignore_edits = ignore_edits or []
        self.rerun: RerunResult | None = None
        """The judge's own re-run of the suite; when set, it decides `test_result` predicates."""
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
                lasting = analysis.lasting_mutation or (
                    f"{analysis.mutation} in a background command" if event.background else None
                )
                if lasting:
                    reason = f"{event.id} may change files until the session ends ({lasting})"
                    out.append((math.inf, event, reason))
                    continue
                before = checks and not analysis.mutation_after_check
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
        return max(self.changes, key=lambda change: change[0]) if self.changes else None

    def fresh(self, event: SessionEvent) -> bool:
        last = self.last_change
        return last is None or event.seq > last[0]

    @cached_property
    def tampering(self) -> list[tuple[FileEditEvent | CommandEvent, str]]:
        """Edits and commands that weakened the test suite."""
        out: list[tuple[FileEditEvent | CommandEvent, str]] = []
        for edit in self.edits:
            out.extend((edit, finding) for finding in edit.test_findings)
        configs = self._config_files
        for edit in self.edits:
            if (reader := configs.get(posixpath.basename(edit.path))) and not is_test_config(
                edit.path
            ):
                out.append(
                    (edit, f"changed {edit.path}, which {reader} reads as its test configuration")
                )
        exported: list[str] = []
        for command in self.commands:
            if command.status is CallStatus.DENIED:
                continue
            analysis = command.analysis
            if analysis.is_test and exported and analysis.opaque_write is None:
                why = f"{exported[0]} set: the run rewrites expected outputs"
                out.append((command, f"may have changed test files: {why}"))
            exported.extend(n for n in analysis.exports if regenerates_env(n))
            for path in analysis.deleted_paths:
                if is_test_path(path):
                    out.append((command, f"deleted or moved test file {path}"))
            out.extend(
                (command, finding)
                for path in analysis.touched_paths
                if (finding := _touched_path_finding(path, configs)) is not None
            )
            if analysis.opaque_write:
                out.append((command, f"may have changed test files: {analysis.opaque_write}"))
            if analysis.harness_change and not (
                self.rerun is not None and _SHELL_STATE.match(analysis.harness_change)
            ):
                out.append((command, f"changed how later commands run: {analysis.harness_change}"))
            out.extend((command, finding) for finding in analysis.source_findings)
        return out

    @cached_property
    def _config_files(self) -> dict[str, str]:
        """Names of files a test run reads as its configuration although no name rule marks
        them (`make -f ci/rules`), to the run that reads them."""
        return {
            posixpath.basename(path): command.id
            for command in self.commands
            if command.status is not CallStatus.DENIED
            for path in command.analysis.config_files
        }

    @property
    def source_changed(self) -> bool:
        """Something outside the tests and their configuration may have changed: an edit of a
        non-test file, a command that names one or writes files it does not name, or an
        action the log does not show."""

        def outside(path: str) -> bool:
            return not (is_test_path(path) or is_test_config(path) or is_harness_path(path))

        if self.unseen_actors or any(outside(e.path) for e in self.edits):
            return True
        for command in self.commands:
            analysis = command.analysis
            if command.status is CallStatus.DENIED or analysis.mutation is None:
                continue
            paths = [*analysis.touched_paths, *analysis.deleted_paths]
            if analysis.opaque_write or any(outside(p) for p in paths):
                return True
        return False

    @cached_property
    def unseen_actors(self) -> list[SessionEvent]:
        return [
            e
            for e in self.run.events
            if (isinstance(e, SubagentEvent) and not e.transcript_loaded)
            or (isinstance(e, OtherToolEvent) and e.may_mutate)
        ]

    @cached_property
    def run_dirs(self) -> dict[str, str | None]:
        """Where each command started, relative to the session directory (`.`), or None when
        only the shell knows. The shell keeps its directory between tool calls, but a
        transcript may record the session directory for every call, so a top-level `cd` is
        followed across calls as well as the recorded directory."""
        root = self.run.cwd
        shell: str | None = "."
        dirs: dict[str, str | None] = {}
        for command in self.commands:
            recorded = _relative_dir(command.cwd, root)
            here = shell if recorded == "." else recorded
            dirs[command.id] = here
            targets = command.analysis.cd_targets
            if command.status is CallStatus.DENIED or not targets:
                continue
            if command.status is not CallStatus.OK:
                shell = None
                continue
            for target in targets:
                here = _resolve_dir(here, target, root)
            shell = here
        return dirs

    def run_dir(self, event: CommandEvent) -> str | None:
        """The directory the command's test run ran in, relative to the session directory."""
        here = self.run_dirs.get(event.id, ".")
        for target in event.analysis.run_cds:
            here = _resolve_dir(here, target, self.run.cwd)
        return here

    def _scoped(self, event: CommandEvent) -> RunOutcome:
        judged = judge_test_run(event)
        where = self.run_dir(event)
        if where == ".":
            return judged
        place = f"in {where}" if where is not None else "in a directory only the shell knows"
        why = f"ran {place}, not the session directory"
        return RunOutcome(
            event, judged.result, judged.reason, ", ".join(filter(None, [judged.partial, why]))
        )

    @cached_property
    def _largest_earlier(self) -> dict[str, tuple[int, CommandEvent]]:
        """For each test run, the most tests an earlier whole-suite run of the same runner
        ran, and that run."""
        largest: dict[tuple[str, ...], tuple[int, CommandEvent]] = {}
        out: dict[str, tuple[int, CommandEvent]] = {}
        for event in self.test_runs(None):
            per_test = event.summary is not None and event.summary.framework in _PER_TEST
            key = (*sorted(set(event.analysis.runners)), *(["per test"] if per_test else []))
            if key in largest:
                out[event.id] = largest[key]
            total = suite_size(event)
            bigger = total is not None and (key not in largest or total > largest[key][0])
            if bigger and total is not None and self._scoped(event).whole:
                largest[key] = (total, event)
        return out

    def outcome(self, event: CommandEvent, minimum: int | None = None) -> RunOutcome:
        """`judge_test_run`, with a run outside the session directory, one that ran fewer tests
        than an earlier whole-suite run (or than `minimum`), or a passing run through a wrapper
        whose command the session does not show that no earlier count vouches for, counted as
        partial."""
        judged = self._counted(event, minimum)
        if (
            judged.whole
            and judged.result is Result.PASS
            and (why := self._unvouched(event)) is not None
        ):
            return RunOutcome(event, judged.result, judged.reason, why)
        return judged

    @cached_property
    def _counts_before(self) -> dict[str, tuple[int, CommandEvent]]:
        """For each test run through a wrapper whose command the session does not show, the most
        tests an earlier run of the same framework ran when it ran the whole suite to the end
        and the session showed its command, and that run."""
        best: dict[str, tuple[int, CommandEvent]] = {}
        out: dict[str, tuple[int, CommandEvent]] = {}
        for event in self.test_runs(None):
            summary = event.summary
            if summary is None or summary.no_tests:
                continue
            per_test = summary.framework in _PER_TEST
            key = f"{summary.framework}{' per test' if per_test else ''}"
            if event.analysis.wrapper_unseen is not None:
                if key in best:
                    out[event.id] = best[key]
                continue
            ran = summary.passed + summary.failed + summary.errors
            if (
                self._counted(event).whole
                and stopped_early(event) is None
                and (key not in best or ran > best[key][0])
            ):
                best[key] = (ran, event)
        return out

    def _unvouched(self, event: CommandEvent) -> str | None:
        """Why a passing run through a wrapper whose command the session does not show may be a
        part of the suite (`@pytest -k sub` in a silent recipe), or None when it passed at least
        as many tests as an earlier whole-suite run the session showed ran, plus the tests the
        session added since, when every change since can be counted."""
        wrapper = event.analysis.wrapper
        if wrapper is None or event.analysis.wrapper_unseen is None or event.summary is None:
            return None
        unseen = f"it ran through {wrapper}, whose command the session does not show"
        earlier = self._counts_before.get(event.id)
        if earlier is None:
            return (
                f"{unseen}, so it may have run only part of the suite, and no earlier "
                "whole-suite run the session showed counted the tests to compare it with"
            )
        if redefined := self._redefined_before(event):
            return (
                f"{unseen}, and {redefined.id} may have changed what it runs, so no earlier "
                "count is a floor for it"
            )
        ran, baseline = earlier
        added = self._tests_added(baseline.seq)
        if added is None:
            return (
                f"{unseen}, and since the earlier whole-suite run {baseline.id} the session "
                "changed the tests in a way the judge cannot count (a command, a parametrize "
                "case, a removed test), so that run's count is no floor for this one"
            )
        if event.summary.passed >= ran + added:
            return None
        since = f" plus the {added} the session added since" if added else ""
        return (
            f"{unseen}, and it passed {event.summary.passed} tests, fewer than the {ran} the "
            f"earlier whole-suite run {baseline.id} ran{since}"
        )

    def _redefined_before(self, event: CommandEvent) -> SessionEvent | None:
        """The first event before `event` that may have changed what a wrapper runs: the rule
        `trusted_wrappers` follows."""
        reads = self._wrapper_reads
        return next(
            (e for e in self.run.events if e.seq < event.seq and may_redefine(e, reads)), None
        )

    @cached_property
    def _wrapper_reads(self) -> frozenset[str]:
        return wrapper_reads(self.run.events)

    def _counted(self, event: CommandEvent, minimum: int | None = None) -> RunOutcome:
        judged = self._scoped(event)
        if not judged.whole:
            return judged
        if env := self.run_env(event):
            why = f"runs with {', '.join(env)} set, which the judge cannot tell is harmless"
            return RunOutcome(event, judged.result, judged.reason, why)
        total = suite_size(event)
        if total is None:
            return judged
        earlier = self._largest_earlier.get(event.id)
        skipped = event.summary.skipped if event.summary else 0
        before = earlier[1].summary.skipped if earlier and earlier[1].summary else 0
        if earlier is not None and total < earlier[0]:
            why = (
                f"ran {total} tests, fewer than the {earlier[0]} of the earlier whole-suite run "
                f"{earlier[1].id}"
            )
        elif earlier is not None and skipped > before:
            why = (
                f"skipped {skipped} tests, more than the {before} of the earlier whole-suite run "
                f"{earlier[1].id}; a skipped test is not a passing one"
            )
        elif minimum is not None and total < minimum:
            why = f"ran {total} tests, fewer than the {minimum} the suite has"
        else:
            return judged
        return RunOutcome(event, judged.result, judged.reason, why)

    @cached_property
    def _inherited(self) -> dict[str, set[str]]:
        """The variables each command inherits from an earlier `export` in the session."""
        exported: set[str] = set()
        out: dict[str, set[str]] = {}
        for command in self.commands:
            if command.status is CallStatus.DENIED:
                continue
            out[command.id] = set(exported)
            exported |= set(command.analysis.exports)
        return out

    def inherited(self, event: CommandEvent) -> list[str]:
        """Variables the command inherits from an earlier `export` beyond the few known to
        change only output or import paths."""
        return sorted(self._inherited.get(event.id, set()))

    def run_env(self, event: CommandEvent) -> list[str]:
        """Variables the run sets or inherits beyond the few known to change only output or
        import paths (`CI=true` can switch on `skipif` markers, `RECORD_MODE=all` can rewrite
        recorded answers)."""
        if not event.analysis.is_test:
            return []
        return sorted({*self.inherited(event), *event.analysis.run_env})

    def scopes(self, event: CommandEvent) -> list[str] | None:
        """The parts of the session directory a run selected only by plain paths and its
        directory (`tests`, `pkg_b`, `.`), relative to it; None if it filtered any other way,
        reached outside it, or named overlapping paths."""
        paths = event.analysis.test_paths
        where = self.run_dir(event)
        summary = event.summary
        if paths is None or where is None or summary is None or summary.deselected:
            return None
        scopes: list[str] = []
        for path in paths or ["."]:
            scope = _resolve_dir(where, path, self.run.cwd)
            if scope is None or posixpath.isabs(scope) or scope.startswith(".."):
                return None
            scopes.append(scope)
        return None if _overlapping(scopes, []) else scopes

    def _tests_added(self, after: int) -> int | None:
        """How many test functions the changes after event `after` added, or None when a change
        may have added tests that cannot be counted (a command, an unknown diff)."""
        count = 0
        for position, event, _ in self.changes:
            if position <= after:
                continue
            if not isinstance(event, FileEditEvent) or event.tests_added is None:
                return None
            if event.adds_tests:
                count += event.tests_added
        return count

    def _new_test_files(self, after: int) -> bool:
        return any(
            isinstance(event, FileEditEvent) and event.change == "add" and event.test_file
            for position, event, _ in self.changes
            if position > after
        )

    def covered(self, outcomes: list[RunOutcome], minimum: int | None = None) -> RunOutcome | None:
        """Passing runs over disjoint paths (`pytest tests`, `cd pkg_b && pytest`) that together
        ran exactly the tests of an earlier whole-suite run, when no change since that run can
        have added a test it cannot count: then they ran every test of it. When several paths
        are combined and the session added tests or test files since, that run's output must
        name every test file it ran, or a new file could stand in for one left out. Overlapping
        paths could count a test twice, and a new test could stand in for one left out, so
        either rules it out."""
        candidates = self._scoped_passes(outcomes)
        if not candidates:
            return None
        newest = candidates[0][0].event
        key = _runner_key(newest)
        found = self._largest_baseline(newest, key)
        if found is None:
            return None
        size, baseline = found
        added = self._tests_added(baseline.seq)
        if size < (minimum or 1) or added is None:
            return None
        chosen, taken = _disjoint(candidates, key)
        if sum(total for _, _, total in chosen) != size + added:
            return None
        if not self._accounts_for(baseline, size, added, chosen, taken):
            return None
        ids = ", ".join(o.event.id for o, _, _ in reversed(chosen))
        where = ", ".join(sorted(taken))
        since = (
            f"plus the {added} test function{'s' if added > 1 else ''} the session added since"
            if added
            else "with no test added since"
        )
        reason = (
            f"{ids} together ran {size + added} tests over {where}, every test of the "
            f"whole-suite run {baseline.id}, {since}"
        )
        return RunOutcome(chosen[0][0].event, Result.PASS, reason, None)

    def _scoped_passes(self, outcomes: list[RunOutcome]) -> list[_ScopedPass]:
        """Passing runs selected only by plain paths, in a clean environment, whose size is
        known, newest first."""
        return [
            (o, scopes, total)
            for o in reversed(outcomes)
            if o.result is Result.PASS
            and o.event.analysis.wrapper_unseen is None
            and not self.run_env(o.event)
            and (scopes := self.scopes(o.event)) is not None
            and (total := suite_size(o.event)) is not None
        ]

    def _largest_baseline(
        self, newest: CommandEvent, key: tuple[str, ...]
    ) -> tuple[int, CommandEvent] | None:
        """The largest whole-suite run of the same runners before `newest`, and its size."""
        baselines = [
            (suite_size(e) or 0, e)
            for e in self.test_runs(None)
            if e.seq < newest.seq
            and _runner_key(e) == key
            and e.analysis.wrapper_unseen is None
            and self._scoped(e).whole
        ]
        if not baselines:
            return None
        return max(baselines, key=lambda b: b[0])

    def _accounts_for(
        self,
        baseline: CommandEvent,
        size: int,
        added: int,
        chosen: list[_ScopedPass],
        taken: list[str],
    ) -> bool:
        """The chosen runs, whose sizes add up to the baseline's, ran its tests and no others:
        a new test file cannot stand in for one left out, they skipped no more tests than it,
        and every test file its output names lies under one of their paths."""
        changed = added or self._new_test_files(baseline.seq)
        if len(taken) > 1 and changed and _every_file(baseline, size) is None:
            return False
        skipped = sum(o.event.summary.skipped for o, _, _ in chosen if o.event.summary)
        if baseline.summary is not None and skipped > baseline.summary.skipped:
            return False
        named = [relative(path, self.run.cwd) for path in _NAMED_TEST_FILE.findall(baseline.output)]
        return all(any(_inside(path, scope) for scope in taken) for path in named)

    def largest_whole_suite(self) -> tuple[int, CommandEvent] | None:
        """The most tests a passing whole-suite run in the session ran, and that run."""
        best: tuple[int, CommandEvent] | None = None
        for event in self.test_runs(None):
            total = suite_size(event)
            outcome = self._counted(event)
            if total is None or outcome.result is not Result.PASS or not outcome.whole:
                continue
            if best is None or total > best[0]:
                best = (total, event)
        return best

    def red_runs(self) -> list[CommandEvent]:
        """The test runs after the last change that failed."""
        return [
            r
            for r in self.test_runs(None)
            if self.fresh(r) and self.outcome(r).result is Result.FAIL
        ]

    def fixed_subset(self) -> bool:
        """The tests failing at the end are a strict subset of those failing before the first
        change: both runs ran the whole suite to the end with the same runner and named every
        test they failed, and the change made none fail that did not before."""
        if not self.changes:
            return False
        first = min(position for position, _, _ in self.changes)
        before = [r for r in self.test_runs(None) if r.seq < first]
        if not before or (start := self._every_failing(before[-1])) is None:
            return False
        if self.rerun is not None:
            rerun = self.rerun
            every = len(rerun.failing) >= rerun.failed + rerun.errors
            complete = rerun.status is RerunStatus.FAIL and rerun.report_read and every
            end = rerun.failing if complete else None
        else:
            red = self.red_runs()
            same = red and all(_runner_key(r) == _runner_key(before[-1]) for r in red)
            end = self._every_failing(red[-1]) if same else None
        return end is not None and set(end) < set(start)

    def _every_failing(self, event: CommandEvent) -> list[str] | None:
        outcome = self.outcome(event)
        if outcome.result is not Result.FAIL or incomplete(outcome):
            return None
        return every_failing_test(event)

    def test_runs(self, runner: str | None) -> list[CommandEvent]:
        runs = [
            c for c in self.commands if c.analysis.is_test and c.status is not CallStatus.DENIED
        ]
        if runner:
            wanted = runner.casefold()
            runs = [c for c in runs if any(wanted in r.casefold() for r in c.analysis.runners)]
        return runs


_NAMED_TEST_FILE: Final = re.compile(r"([\w./-]+\.\w+)(?:::|:\d+:)")
"""A test file a run's output names: in a node id (`tests/x.py::test_a`) or a traceback line
(`tests/x.py:5: AssertionError`)."""


_NODE_ID: Final = re.compile(r"([\w./-]+\.\w+)::\S+")
_PROGRESS: Final = re.compile(r"^([\w./-]+\.\w+) ([.FEsxX]+)\s*(?:\[\s*\d+%\])?\s*$", re.M)


def _runner_key(event: CommandEvent) -> tuple[str, ...]:
    return tuple(sorted(set(event.analysis.runners)))


def _disjoint(
    candidates: list[_ScopedPass], key: tuple[str, ...]
) -> tuple[list[_ScopedPass], list[str]]:
    """The newest runs of the `key` runners whose paths overlap none taken before, and those
    paths."""
    chosen: list[_ScopedPass] = []
    taken: list[str] = []
    for outcome, scopes, total in candidates:
        if _runner_key(outcome.event) != key:
            continue
        if _overlapping(scopes, taken):
            continue
        chosen.append((outcome, scopes, total))
        taken.extend(scopes)
    return chosen, taken


def _every_file(event: CommandEvent, size: int) -> set[str] | None:
    """The test files a run's output names when it names one for every test it ran (`-v`
    lines, `-rA` summaries or per-file progress lines), else None."""
    nodes = {m.group(0) for m in _NODE_ID.finditer(event.output)}
    if len(nodes) >= size:
        return {node.partition("::")[0] for node in nodes}
    progress = _PROGRESS.findall(event.output)
    if progress and sum(len(marks) for _, marks in progress) >= size:
        return {path for path, _ in progress}
    return None


def _inside(path: str, scope: str) -> bool:
    path = posixpath.normpath(path)
    return scope == "." or path == scope or path.startswith(scope + "/")


def _overlapping(scopes: list[str], taken: list[str]) -> bool:
    """Two of `scopes`, or one of them and one of `taken`, are the same or nested paths."""

    def nested(a: str, b: str) -> bool:
        return "." in (a, b) or a == b or b.startswith(a + "/") or a.startswith(b + "/")

    pairs = [(a, b) for i, a in enumerate(scopes) for b in [*scopes[i + 1 :], *taken]]
    return any(nested(a, b) for a, b in pairs)


def _relative_dir(path: str | None, root: str | None) -> str | None:
    if path is None or root is None:
        return "." if path is None or path == root else None
    rel = relative(path, root)
    return rel if not posixpath.isabs(rel) and not rel.startswith("../") else path


def _resolve_dir(here: str | None, target: str, root: str | None) -> str | None:
    """The directory a `cd target` from `here` reaches, relative to `root`, or None."""
    if target == "?" or (here is None and not posixpath.isabs(target)):
        return None
    joined = posixpath.normpath(posixpath.join(here or "", target))
    if not posixpath.isabs(joined) and not joined.startswith(".."):
        return joined
    if root is None:
        return None
    return _relative_dir(posixpath.normpath(posixpath.join(root, joined)), root)
