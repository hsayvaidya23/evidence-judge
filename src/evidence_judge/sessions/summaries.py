"""Test summaries parsed from a runner's own output.

Each parser reads the final summary a framework prints (pytest's `== 2 failed, 1 passed in
0.03s ==`, jest's `Tests: 1 failed, 4 passed, 5 total`, cargo's `test result: ok. ...`).
Only lines in that framework's fixed format count, and the *last* summary wins, because
runners print the summary at the end. A runner the judge does not know is parsed with every
parser and the counts are added, since a script can run several frameworks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from evidence_judge.sessions.models import TestSummary

_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PYTEST_LINE: Final = re.compile(
    r"^=*\s*(?P<body>(?:\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|"
    r"warnings?|rerun)(?:, )?)+|no tests ran)(?: in [\d.]+s(?: \([\d:]+\))?)?\s*=*\s*$"
)
_COUNT: Final = re.compile(r"(\d+) ([a-z]+)")
_UNITTEST_RAN: Final = re.compile(r"^Ran (\d+) tests? in [\d.]+s$")
_UNITTEST_RESULT: Final = re.compile(r"^(OK|FAILED)(?: \((?P<body>[^)]*)\))?\s*$")
_JEST_TESTS: Final = re.compile(r"^Tests:\s+(?P<body>.*\d+ total)\s*$")
_VITEST_TESTS: Final = re.compile(r"^Tests\s+(?P<body>(?:\d+ \w+(?: \| )?)+)\s*\(\d+\)")
_MOCHA: Final = re.compile(r"^(\d+) (passing|failing|pending)(?: \(\d+m?s\))?\s*$")
_GO_OK: Final = re.compile(r"^ok\s+\S+\s+(?:[\d.]+s|\(cached\))")
_GO_FAIL: Final = re.compile(
    r"^(?:FAIL\s+\S+\s+[\d.]+s|FAIL\s+\S+\s+\[(?:build|setup) failed\]|--- FAIL: )"
)
_GO_NO_FILES: Final = re.compile(r"^\?\s+\S+\s+\[no test files\]")
_GO_PANIC: Final = re.compile(r"^panic: ")
_GO_BUILD_FAIL: Final = re.compile(r"^FAIL\s+\S+\s+\[(?:build|setup) failed\]")
_NODE_COUNT: Final = re.compile(
    r"^(?:\u2139|#)\s*(?P<key>tests|suites|pass|fail|cancelled|skipped|todo|duration_ms)\s+"
    r"(?P<n>\d+)(?:\.\d+)?\s*$"
)
_CARGO: Final = re.compile(
    r"^test result: (?P<status>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed; "
    r"(?P<ignored>\d+) ignored; \d+ measured; (?P<filtered>\d+) filtered out"
)
_NEXTEST: Final = re.compile(r"^\s*Summary \[[^\]]*\]\s+(?P<total>\d+) tests? run: (?P<body>.*)$")
_RSPEC: Final = re.compile(r"^(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?")
_PHPUNIT_OK: Final = re.compile(r"^OK \((\d+) tests?, \d+ assertions?\)")
_PHPUNIT_FAIL: Final = re.compile(r"^Tests: (\d+), Assertions: \d+(?P<body>(?:, \w+: \d+)*)\.?\s*$")
_DOTNET: Final = re.compile(
    r"^(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+), Passed:\s+(\d+), Skipped:\s+(\d+), Total:\s+\d+"
)
_MAVEN: Final = re.compile(
    r"^(?:\[\w+\] )?Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$"
)


def _lines(output: str) -> list[str]:
    return [_ANSI.sub("", line).strip() for line in output.splitlines()]


def _pytest(lines: list[str]) -> TestSummary | None:
    for line in reversed(lines):
        m = _PYTEST_LINE.match(line)
        if m is None:
            continue
        body = m.group("body")
        if body == "no tests ran":
            return TestSummary(framework="pytest", no_tests=True)
        counts = {word: int(n) for n, word in _COUNT.findall(body)}
        return TestSummary(
            framework="pytest",
            passed=counts.get("passed", 0) + counts.get("xpassed", 0),
            failed=counts.get("failed", 0),
            errors=counts.get("error", 0) + counts.get("errors", 0),
            skipped=counts.get("skipped", 0) + counts.get("xfailed", 0),
            deselected=counts.get("deselected", 0),
        )
    return None


def _unittest(lines: list[str]) -> TestSummary | None:
    for index in range(len(lines) - 1, -1, -1):
        ran = _UNITTEST_RAN.match(lines[index])
        if ran is None:
            continue
        total = int(ran.group(1))
        result = next(
            (m for line in lines[index + 1 :] if (m := _UNITTEST_RESULT.match(line))), None
        )
        if result is None:
            return None
        counts = {
            k.strip(): int(v)
            for k, _, v in (part.partition("=") for part in (result.group("body") or "").split(","))
            if v.strip().isdigit()
        }
        failed = counts.get("failures", 0)
        errors = counts.get("errors", 0)
        skipped = counts.get("skipped", 0) + counts.get("expected failures", 0)
        if total == 0:
            return TestSummary(framework="unittest", no_tests=True)
        return TestSummary(
            framework="unittest",
            passed=max(total - failed - errors - skipped, 0),
            failed=failed,
            errors=errors,
            skipped=skipped,
        )
    return None


def _counts(body: str) -> dict[str, int]:
    return {word: int(n) for n, word in _COUNT.findall(body)}


def _jest(lines: list[str]) -> TestSummary | None:
    for line in reversed(lines):
        if m := _JEST_TESTS.match(line):
            c = _counts(m.group("body"))
            total = c.get("total", 0)
            return TestSummary(
                framework="jest",
                passed=c.get("passed", 0),
                failed=c.get("failed", 0),
                skipped=c.get("skipped", 0) + c.get("todo", 0),
                no_tests=total == 0,
            )
        if m := _VITEST_TESTS.match(line):
            c = _counts(m.group("body"))
            return TestSummary(
                framework="vitest",
                passed=c.get("passed", 0),
                failed=c.get("failed", 0),
                skipped=c.get("skipped", 0) + c.get("todo", 0),
            )
    return None


def _mocha(lines: list[str]) -> TestSummary | None:
    found: dict[str, int] = {}
    for line in reversed(lines):
        m = _MOCHA.match(line)
        if m is not None and m.group(2) not in found:
            found[m.group(2)] = int(m.group(1))
        elif found and line and m is None and "passing" in found:
            break
    if "passing" not in found and "failing" not in found:
        return None
    return TestSummary(
        framework="mocha",
        passed=found.get("passing", 0),
        failed=found.get("failing", 0),
        skipped=found.get("pending", 0),
    )


def _go(lines: list[str]) -> TestSummary | None:
    """Packages (`ok pkg`, `FAIL pkg`), or test functions when the run printed every result
    (`go test -v`: `--- PASS:`, `--- FAIL:`, `--- SKIP:`), so two runs compare like units."""
    ok = sum(1 for line in lines if _GO_OK.match(line))
    failed = sum(1 for line in lines if _GO_FAIL.match(line) and not line.startswith("--- "))
    failed_tests = sum(1 for line in lines if line.startswith("--- FAIL: "))
    panics = sum(1 for line in lines if _GO_PANIC.match(line))
    no_files = sum(1 for line in lines if _GO_NO_FILES.match(line))
    if not (ok or failed or failed_tests or panics or no_files):
        return None
    passed_tests = sum(1 for line in lines if line.startswith("--- PASS: "))
    skipped_tests = sum(1 for line in lines if line.startswith("--- SKIP: "))
    if passed_tests or skipped_tests:
        build = sum(1 for line in lines if _GO_BUILD_FAIL.match(line))
        return TestSummary(
            framework="go test -v",
            passed=passed_tests,
            failed=failed_tests,
            errors=panics + build,
            skipped=skipped_tests,
            no_tests=not (passed_tests or failed_tests or skipped_tests or panics or build),
        )
    return TestSummary(
        framework="go test",
        passed=ok,
        failed=max(failed, 1 if failed_tests else 0),
        errors=panics,
        no_tests=not (ok or failed or failed_tests or panics),
    )


def _node(lines: list[str]) -> TestSummary | None:
    """The `node --test` totals: the spec reporter's `tests 2` block (after an information sign) or TAP's `# tests 2`."""
    counts: dict[str, int] = {}
    for line in reversed(lines):
        m = _NODE_COUNT.match(line)
        if m is None:
            if counts:
                break
            continue
        counts.setdefault(m.group("key"), int(m.group("n")))
    if "tests" not in counts or "pass" not in counts:
        return None
    return TestSummary(
        framework="node --test",
        passed=counts["pass"],
        failed=counts.get("fail", 0),
        errors=counts.get("cancelled", 0),
        skipped=counts.get("skipped", 0) + counts.get("todo", 0),
        no_tests=counts["tests"] == 0,
    )


def _cargo(lines: list[str]) -> TestSummary | None:
    for line in reversed(lines):
        if m := _NEXTEST.match(line):
            c = _counts(m.group("body"))
            return TestSummary(
                framework="cargo nextest",
                passed=c.get("passed", 0),
                failed=c.get("failed", 0),
                skipped=c.get("skipped", 0),
                no_tests=int(m.group("total")) == 0,
            )
    results = [m for line in lines if (m := _CARGO.match(line))]
    if not results:
        return None
    passed = sum(int(m.group("passed")) for m in results)
    failed = sum(int(m.group("failed")) for m in results)
    if failed == 0 and any(m.group("status") == "FAILED" for m in results):
        failed = 1
    return TestSummary(
        framework="cargo test",
        passed=passed,
        failed=failed,
        skipped=sum(int(m.group("ignored")) for m in results),
        deselected=sum(int(m.group("filtered")) for m in results),
        no_tests=passed + failed == 0,
    )


def _others(lines: list[str]) -> TestSummary | None:
    for line in reversed(lines):
        if m := _RSPEC.match(line):
            total, failures = int(m.group(1)), int(m.group(2))
            pending = int(m.group(3) or 0)
            return TestSummary(
                framework="rspec",
                passed=max(total - failures - pending, 0),
                failed=failures,
                skipped=pending,
                no_tests=total == 0,
            )
        if m := _PHPUNIT_OK.match(line):
            return TestSummary(framework="phpunit", passed=int(m.group(1)))
        if m := _PHPUNIT_FAIL.match(line):
            c = {k.strip().lower(): int(v) for k, v in re.findall(r"(\w+): (\d+)", m.group("body"))}
            bad = c.get("failures", 0) + c.get("errors", 0)
            return TestSummary(
                framework="phpunit",
                passed=max(int(m.group(1)) - bad - c.get("skipped", 0), 0),
                failed=c.get("failures", 0),
                errors=c.get("errors", 0),
                skipped=c.get("skipped", 0),
            )
        if m := _DOTNET.match(line):
            return TestSummary(
                framework="dotnet test",
                failed=int(m.group(1)),
                passed=int(m.group(2)),
                skipped=int(m.group(3)),
            )
        if m := _MAVEN.match(line):
            run, failures, errors, skipped = (int(g) for g in m.groups())
            return TestSummary(
                framework="maven",
                passed=max(run - failures - errors - skipped, 0),
                failed=failures,
                errors=errors,
                skipped=skipped,
                no_tests=run == 0,
            )
    return None


_Parser = Callable[[list[str]], TestSummary | None]
_BY_RUNNER: Final[dict[str, _Parser]] = {
    "pytest": _pytest,
    "unittest": _unittest,
    "jest": _jest,
    "vitest": _jest,
    "bun test": _jest,
    "mocha": _mocha,
    "go test": _go,
    "node --test": _node,
    "cargo test": _cargo,
    "cargo nextest": _cargo,
    "rspec": _others,
    "phpunit": _others,
    "pest": _others,
    "dotnet test": _others,
    "mvn": _others,
    "mvnw": _others,
}
_ALL: Final[tuple[_Parser, ...]] = (_pytest, _unittest, _jest, _mocha, _go, _node, _cargo, _others)


def _combine(found: list[TestSummary]) -> TestSummary | None:
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return TestSummary(
        framework="+".join(s.framework for s in found),
        passed=sum(s.passed for s in found),
        failed=sum(s.failed for s in found),
        errors=sum(s.errors for s in found),
        skipped=sum(s.skipped for s in found),
        deselected=sum(s.deselected for s in found),
        no_tests=all(s.no_tests for s in found),
    )


def parse_summary(runners: list[str], output: str) -> TestSummary | None:
    """The test summary in `output` of a command that ran `runners`, or None if there is none."""
    if not runners or not output:
        return None
    lines = _lines(output)
    known = [_BY_RUNNER[r] for r in dict.fromkeys(runners) if r in _BY_RUNNER]
    parsers = known if len(known) == len(set(runners)) else list(_ALL)
    return _combine([s for parser in dict.fromkeys(parsers) if (s := parser(lines)) is not None])
