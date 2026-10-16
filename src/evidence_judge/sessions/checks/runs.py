"""Reading one test run: whether it passed, failed or cannot tell, and the failing tests and
counts its output shows."""

from __future__ import annotations

import re
from typing import Final

from evidence_judge.sessions.checks.common import Result, RunOutcome
from evidence_judge.sessions.models import CallStatus, CommandEvent

_TAIL_ONLY: Final = "its output is piped into tail"


def suite_size(event: CommandEvent) -> int | None:
    """How many tests the run collected and ran (skipped included, deselected not), or None."""
    summary = event.summary
    if summary is None or summary.no_tests:
        return None
    return summary.passed + summary.failed + summary.errors + summary.skipped


def exit_reliable(event: CommandEvent) -> bool:
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
    reliable = exit_reliable(event)
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


def mentions(event: CommandEvent, name: str) -> bool:
    needle = name.casefold()
    return needle in event.command.casefold() or needle in event.output.casefold()


_FAILURE_WORD = re.compile(r"FAIL|failed|Error|XPASS\(strict\)|\u2715|\u00d7")
_FAILING_TEST: Final = re.compile(
    r"^(?:(?:FAILED|ERROR) (?P<pytest>\S+::\S+)|XPASS\(strict\) (?P<xpass>\S+::\S+)|"
    r"--- FAIL: (?P<go>[^\s/]+) \(|test (?P<cargo>\S+) \.\.\. FAILED$)",
    re.M,
)
"""A failing test a run's output names: pytest's short summary (`FAILED tests/x.py::test_a`,
`XPASS(strict) tests/x.py::test_b`), go's `--- FAIL: TestA (0.00s)` and cargo's `test a ... FAILED`."""


def names_test(name: str, text: str) -> bool:
    """`text` names the test `name` as a whole word: `test_add` is not `test_add_negative`."""
    pattern = rf"(?<![\w]){re.escape(name.strip().casefold())}(?![\w])"
    return re.search(pattern, text.casefold()) is not None


def named_failure(event: CommandEvent, name: str) -> bool:
    """The failing run ran only `name`, or its output reports `name` as failing."""
    if names_test(name, event.command):
        return True
    return any(
        names_test(name, line) and _FAILURE_WORD.search(line) for line in event.output.splitlines()
    )


_UNLOADED: Final = re.compile(
    r"^(?:FAIL\s+\S+\s+\[(?:build|setup) failed\]|panic: |"
    r"(?:\u25cf )?Test suite failed to run|.*\bFailed Suites \d+|"
    r"ERROR collecting |ERROR (?!\S+::)\S+\.\w+(?: - |$)|"
    r".*unittest\.loader\._FailedTest)",
    re.M,
)
"""A unit of tests that failed before its tests ran: a Go package that did not build or
panicked, a jest or vitest suite that failed to run, a pytest module that failed to collect, a
unittest module that failed to import. Its tests are neither counted nor named."""


def failure_count(event: CommandEvent) -> int | None:
    """How many tests the run reports failing, or None when its summary does not count tests
    (plain `go test` counts packages; then its `--- FAIL:` lines count them) or when a unit of
    tests failed to load, so the tests in it are not counted."""
    summary = event.summary
    if summary is None or _UNLOADED.search(_strip(event.output)):
        return None
    if summary.framework == "go test":
        return len(_failing_tests(event)) or None
    return summary.failed + summary.errors


def _failing_lines(event: CommandEvent) -> list[str]:
    """Every failing test the output names, once per line that names it (a pytest test can fail
    and then error in teardown)."""
    output = _strip(event.output)
    return [next(g for g in m.groups() if g) for m in _FAILING_TEST.finditer(output)]


def _failing_tests(event: CommandEvent) -> list[str]:
    return list(dict.fromkeys(_failing_lines(event)))


def _strip(output: str) -> str:
    return "\n".join(line.strip() for line in output.splitlines())


_COMPILE_FAILED: Final = re.compile(
    r"^(?:FAIL\s+\S+\s+\[build failed\]|error: could not compile `[^`]+`.*)$", re.M
)
"""Go's `FAIL pkg [build failed]` and cargo's `error: could not compile `crate``: the code did
not compile, so the tests in it did not run."""


def compile_failure(event: CommandEvent) -> str | None:
    """The line saying a failing test run did not compile, when its output names no failing
    test: then the build, not a test, is what failed."""
    output = _strip(event.output)
    match = _COMPILE_FAILED.search(output)
    if match is None or _FAILING_TEST.search(output) or not exit_reliable(event):
        return None
    return match.group(0) if event.exit_code != 0 else None


def every_failing_test(event: CommandEvent) -> list[str] | None:
    """The failing tests the run's output names when it names every one it counts, else None."""
    lines = _failing_lines(event)
    count = failure_count(event)
    return (
        list(dict.fromkeys(lines)) if lines and count is not None and len(lines) >= count else None
    )


_PARAMETERS: Final = re.compile(r"\[.*\]$")


def failure_counts(event: CommandEvent) -> list[int]:
    """The ways to count the run's failing tests: its summary's count and, when the output
    names every failing test, the distinct tests it names, with and without their parameters
    (a test that fails and errors in teardown, or fails for two parameters, is one test)."""
    observed = failure_count(event)
    if observed is None:
        return []
    counts = [observed]
    if (names := every_failing_test(event)) is not None:
        counts += [len(names), len({_PARAMETERS.sub("", name) for name in names})]
    return list(dict.fromkeys(counts))


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


def incomplete(outcome: RunOutcome) -> str | None:
    """Why the run cannot show every test of the suite that fails, or None: a filtered or
    partial run never ran some, and one that stopped early skipped the rest."""
    return outcome.partial or stopped_early(outcome.event)


def not_failing(outcome: RunOutcome, name: str) -> RunOutcome | None:
    """The failing run, as evidence that `name` did not fail, when it ran the whole suite to the
    end and names every test it failed, and not `name`."""
    if incomplete(outcome) or (named := every_failing_test(outcome.event)) is None:
        return None
    if any(names_test(name, test) for test in named):
        return None
    reason = f"{outcome.reason}; {not_among(name, named)}"
    return RunOutcome(outcome.event, Result.PASS, reason)


def not_among(name: str, failing: list[str]) -> str:
    return f"{name} is not among its failing tests ({', '.join(failing)})"
