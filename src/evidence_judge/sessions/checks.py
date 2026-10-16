"""Deterministic checkers for session predicates.

Each returns a `PredicateResult`: supported, contradicted, or no evidence, with the reason and
the events it rests on. The soundness rules are in docs/design/coding-agent-sessions.md; in
short, a passing test run proves a pass only when it ran after the last workspace change,
without filters, with a parsed summary, with its own exit status, with its whole output, from a
command line that does not itself print summary text, and with no test-weakening edit anywhere
in the session.
"""

from __future__ import annotations

import math
import posixpath
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import Final

from evidence_judge.core.predicates import compile_pattern
from evidence_judge.core.verdict import Citation, PredicateResult, Support
from evidence_judge.sessions.edits import (
    NO_DIFF,
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
from evidence_judge.sessions.predicates import (
    BuildPredicate,
    CommandPredicate,
    EditPredicate,
    SessionPredicate,
    TestResultPredicate,
)
from evidence_judge.sessions.rerun import RerunResult, RerunStatus
from evidence_judge.sessions.shell import READ_ONLY_PROGRAMS, regenerates_env

CHECKER: Final = "session"
_EXCERPT: Final = 160
_TAIL_ONLY: Final = "its output is piped into tail"
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


class Result(StrEnum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    event: CommandEvent
    result: Result
    reason: str
    partial: str | None = None
    """Why the run is not the whole suite, or None when it is."""

    @property
    def whole(self) -> bool:
        return self.partial is None


def _blocker(finding: str) -> str:
    return "the code could not be checked" if NO_DIFF in finding else "the suite was weakened"


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
        """`judge_test_run`, with a run outside the session directory, or one that ran fewer
        tests than an earlier whole-suite run (or than `minimum`), counted as partial."""
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
    def _run_envs(self) -> dict[str, set[str]]:
        """The variables each test run sets on its command line or inherits from an earlier
        `export` in the session."""
        exported: set[str] = set()
        out: dict[str, set[str]] = {}
        for command in self.commands:
            if command.status is CallStatus.DENIED:
                continue
            if command.analysis.is_test:
                out[command.id] = exported | set(command.analysis.run_env)
            exported |= set(command.analysis.exports)
        return out

    def run_env(self, event: CommandEvent) -> list[str]:
        """Variables the run sets or inherits beyond the few known to change only output or
        import paths (`CI=true` can switch on `skipif` markers, `RECORD_MODE=all` can rewrite
        recorded answers)."""
        return sorted(self._run_envs.get(event.id, set()))

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
            if e.seq < newest.seq and _runner_key(e) == key and self._scoped(e).whole
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
            outcome = self.outcome(event)
            if total is None or outcome.result is not Result.PASS or not outcome.whole:
                continue
            if best is None or total > best[0]:
                best = (total, event)
        return best

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


def suite_size(event: CommandEvent) -> int | None:
    """How many tests the run collected and ran (skipped included, deselected not), or None."""
    summary = event.summary
    if summary is None or summary.no_tests:
        return None
    return summary.passed + summary.failed + summary.errors + summary.skipped


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
    partial = ", ".join(analysis.test_filters) or (
        f"{summary.deselected} deselected" if summary and summary.deselected else None
    )

    def outcome(result: Result, reason: str) -> RunOutcome:
        return RunOutcome(event, result, reason, partial)

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
    if summary is not None and summary.conflict:
        return outcome(
            Result.UNKNOWN,
            f"{event.id} shows {summary.describe()}, but {summary.conflict}: code the run "
            "imported can print a passing summary after the real one",
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
_FAILING_TEST: Final = re.compile(
    r"^(?:(?:FAILED|ERROR) (?P<pytest>\S+::\S+)|--- FAIL: (?P<go>[^\s/]+) \(|"
    r"test (?P<cargo>\S+) \.\.\. FAILED$)",
    re.M,
)
"""A failing test a run's output names: pytest's short summary (`FAILED tests/x.py::test_a`),
go's `--- FAIL: TestA (0.00s)` and cargo's `test a ... FAILED`."""


def names_test(name: str, text: str) -> bool:
    """`text` names the test `name` as a whole word: `test_add` is not `test_add_negative`."""
    pattern = rf"(?<![\w]){re.escape(name.strip().casefold())}(?![\w])"
    return re.search(pattern, text.casefold()) is not None


def _named_failure(event: CommandEvent, name: str) -> bool:
    """The failing run ran only `name`, or its output reports `name` as failing."""
    if names_test(name, event.command):
        return True
    return any(
        names_test(name, line) and _FAILURE_WORD.search(line) for line in event.output.splitlines()
    )


def failure_count(event: CommandEvent) -> int | None:
    """How many tests the run reports failing, or None when its summary does not count tests
    (plain `go test` counts packages; then its `--- FAIL:` lines count them)."""
    summary = event.summary
    if summary is None:
        return None
    if summary.framework == "go test":
        return len(_failing_tests(event)) or None
    return summary.failed + summary.errors


def _failing_tests(event: CommandEvent) -> list[str]:
    found = (next(g for g in m.groups() if g) for m in _FAILING_TEST.finditer(_strip(event.output)))
    return list(dict.fromkeys(found))


def _strip(output: str) -> str:
    return "\n".join(line.strip() for line in output.splitlines())


def every_failing_test(event: CommandEvent) -> list[str] | None:
    """The failing tests the run's output names when it names every one it counts, else None."""
    names = _failing_tests(event)
    count = failure_count(event)
    return names if names and count is not None and len(names) >= count else None


_STOPPED: Final = re.compile(
    r"^!+ (?:stopping after \d+ failures?|Interrupted: [^!]*|KeyboardInterrupt) !+$", re.M
)
"""pytest's banner for a run that ended before running every test it collected."""


def stopped_early(event: CommandEvent) -> str | None:
    """Why the run may have ended before running every test it selected, or None."""
    if event.analysis.stop_early:
        return event.analysis.stop_early
    if match := _STOPPED.search(_strip(event.output)):
        return f"its output says {match.group(0).strip('! ')!r}"
    return None


def _incomplete(outcome: RunOutcome) -> str | None:
    """Why the run cannot show every test of the suite that fails, or None: a filtered or
    partial run never ran some, and one that stopped early skipped the rest."""
    return outcome.partial or stopped_early(outcome.event)


def _not_failing(outcome: RunOutcome, name: str) -> RunOutcome | None:
    """The failing run, as evidence that `name` did not fail, when it ran the whole suite to the
    end and names every test it failed, and not `name`."""
    if _incomplete(outcome) or (named := every_failing_test(outcome.event)) is None:
        return None
    if any(names_test(name, test) for test in named):
        return None
    reason = f"{outcome.reason}; {_not_among(name, named)}"
    return RunOutcome(outcome.event, Result.PASS, reason)


def _not_among(name: str, failing: list[str]) -> str:
    return f"{name} is not among its failing tests ({', '.join(failing)})"


def _rerun_applies(predicate: TestResultPredicate, rerun: RerunResult) -> bool:
    if predicate.runner is None:
        return True
    wanted = predicate.runner.casefold()
    return wanted in (rerun.runner or "").casefold() or any(
        wanted in part.casefold() for part in rerun.command
    )


def check_rerun(predicate: TestResultPredicate, index: SessionIndex) -> PredicateResult:
    """Decide a `test_result` predicate from the judge's own re-run of the suite. The session's
    test output is not read; its test-weakening findings still keep a pass from proving
    anything, because a re-run of weakened tests passes too."""
    rerun = index.rerun
    assert rerun is not None
    cite = Citation(evidence_id="rerun", excerpt=" ".join(rerun.command)[:_EXCERPT] or None)
    wants_pass = predicate.expect == "pass"

    def result(status: Support, reason: str) -> PredicateResult:
        return PredicateResult(status=status, reason=reason, citations=[cite], checker=CHECKER)

    def passed(reason: str) -> PredicateResult:
        if not wants_pass:
            return result(Support.CONTRADICTED, reason)
        if index.tampering:
            source, finding = index.tampering[0]
            weakened = f"{reason}, but {_blocker(finding)}: {source.id} {finding}"
            return PredicateResult(
                status=Support.NO_EVIDENCE,
                reason=weakened,
                citations=[cite, _cite(source, getattr(source, "path", None))],
                checker=CHECKER,
            )
        return result(Support.SUPPORTED, reason)

    def failed(reason: str) -> PredicateResult:
        return result(Support.CONTRADICTED if wants_pass else Support.SUPPORTED, reason)

    if predicate.mentions and rerun.report_read:
        name = predicate.mentions
        if any(names_test(name, test) for test in rerun.failing):
            return failed(f"{rerun.reason}; {name} failed")
        if rerun.status is RerunStatus.PASS:
            return passed(rerun.reason)
        if any(names_test(name, test) for test in rerun.passing):
            return passed(f"{name} passed in the judge's re-run ({rerun.describe()})")
        every = len(rerun.failing) >= rerun.failed + rerun.errors
        if not wants_pass and rerun.status is RerunStatus.FAIL and every:
            return result(
                Support.CONTRADICTED, f"{rerun.reason}; {_not_among(name, rerun.failing)}"
            )
        return result(Support.NO_EVIDENCE, f"the judge's re-run did not report {name}")
    if rerun.status is RerunStatus.PASS:
        return passed(rerun.reason)
    if rerun.status is RerunStatus.FAIL:
        if predicate.count is not None and not wants_pass:
            return _rerun_count(predicate.count, rerun, cite)
        return failed(rerun.reason)
    return result(Support.NO_EVIDENCE, rerun.reason)


def _rerun_count(count: int, rerun: RerunResult, cite: Citation) -> PredicateResult:
    observed = rerun.failed + rerun.errors
    if not rerun.report_read:
        status = Support.NO_EVIDENCE
        reason = f"{rerun.reason}; the judge's re-run does not show how many tests failed"
    elif observed != count:
        status = Support.CONTRADICTED
        reason = f"{rerun.reason}: {observed} tests failed, not {count}"
    else:
        status, reason = Support.SUPPORTED, rerun.reason
    return PredicateResult(status=status, reason=reason, citations=[cite], checker=CHECKER)


def check_test_result(predicate: TestResultPredicate, index: SessionIndex) -> PredicateResult:
    if index.rerun is not None and _rerun_applies(predicate, index.rerun):
        return check_rerun(predicate, index)
    outcomes = _fresh_outcomes(predicate, index)
    if isinstance(outcomes, PredicateResult):
        return outcomes
    fails, passes = _deciding(predicate, outcomes)
    if predicate.expect == "fail":
        return _expect_fail(predicate, outcomes, fails)
    return _expect_pass(predicate, index, outcomes, fails, passes)


def _fresh_outcomes(
    predicate: TestResultPredicate, index: SessionIndex
) -> list[RunOutcome] | PredicateResult:
    """The outcomes of the test runs after the last change, with their union when no single run
    passed the whole suite; or why there are none."""
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
    outcomes = [index.outcome(r, predicate.min_tests) for r in fresh]
    if not any(o.result is Result.PASS and o.whole for o in outcomes) and (
        union := index.covered(outcomes, predicate.min_tests)
    ):
        outcomes = sorted([*outcomes, union], key=lambda o: o.event.seq)
    return outcomes


def _deciding(
    predicate: TestResultPredicate, outcomes: list[RunOutcome]
) -> tuple[list[RunOutcome], list[RunOutcome]]:
    """The failing and passing outcomes that speak to the claim."""
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
    return fails, passes


def _expect_fail(
    predicate: TestResultPredicate, outcomes: list[RunOutcome], fails: list[RunOutcome]
) -> PredicateResult:
    whole_pass = [o for o in outcomes if o.result is Result.PASS and o.whole]
    if fails and whole_pass and whole_pass[-1].event.seq > fails[-1].event.seq:
        return _flaky(fails[-1], whole_pass[-1])
    if fails:
        return _failing_count(predicate, fails[-1])
    red = [o for o in outcomes if o.result is Result.FAIL]
    if predicate.mentions and red and (spared := _not_failing(red[-1], predicate.mentions)):
        return _result(Support.CONTRADICTED, spared.reason, spared.event)
    if whole_pass:
        return _result(Support.CONTRADICTED, whole_pass[-1].reason, whole_pass[-1].event)
    return _undecided(outcomes, predicate)


def _failing_count(predicate: TestResultPredicate, fail: RunOutcome) -> PredicateResult:
    """A failing run supports a claim that tests fail, if it failed as many as the claim
    says: a count needs a whole-suite run that ran to the end, since a filtered run or one
    that stopped early leaves some tests unrun."""
    if predicate.count is None:
        return _result(Support.SUPPORTED, fail.reason, fail.event)
    if why := _incomplete(fail):
        return _result(
            Support.NO_EVIDENCE,
            f"{fail.reason}, but the run cannot show how many tests of the suite fail: {why}",
            fail.event,
        )
    observed = failure_count(fail.event)
    if observed is None:
        return _result(
            Support.NO_EVIDENCE,
            f"{fail.reason}, but the run does not show how many tests failed",
            fail.event,
        )
    if observed != predicate.count:
        return _result(
            Support.CONTRADICTED,
            f"{fail.reason}: {observed} tests failed, not {predicate.count}",
            fail.event,
        )
    return _result(Support.SUPPORTED, fail.reason, fail.event)


def _expect_pass(
    predicate: TestResultPredicate,
    index: SessionIndex,
    outcomes: list[RunOutcome],
    fails: list[RunOutcome],
    passes: list[RunOutcome],
) -> PredicateResult:
    if fails and passes and passes[-1].event.seq > fails[-1].event.seq:
        return _flaky(fails[-1], passes[-1])
    if fails:
        return _result(Support.CONTRADICTED, fails[-1].reason, fails[-1].event)
    if not passes and (spared := _spared(predicate, outcomes)) is not None:
        passes = [spared]
    if not passes:
        return _undecided(outcomes, predicate)
    if index.tampering:
        source, finding = index.tampering[0]
        return _result(
            Support.NO_EVIDENCE,
            f"{passes[-1].reason}, but {_blocker(finding)}: {source.id} {finding}",
            passes[-1].event,
            source,
        )
    return _result(Support.SUPPORTED, passes[-1].reason, passes[-1].event)


def _spared(predicate: TestResultPredicate, outcomes: list[RunOutcome]) -> RunOutcome | None:
    """A failing whole-suite run that ran to the end and names every test it failed, and not
    the one the claim says passes."""
    red = [o for o in outcomes if o.result is Result.FAIL]
    if not predicate.mentions or not red:
        return None
    return _not_failing(red[-1], predicate.mentions)


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
        why = o.partial
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
