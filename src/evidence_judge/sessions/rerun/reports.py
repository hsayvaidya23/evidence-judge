"""Asking each runner for a machine-readable report, and reading it."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from evidence_judge.sessions.rerun.spec import Runner

_MAX_NAMES: Final = 20_000


class ReportError(Exception):
    """The runner's report is missing, unreadable or contradicts itself."""


@dataclass(slots=True)
class Counts:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failing: list[str] = field(default_factory=list[str])
    passing: list[str] = field(default_factory=list[str])

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    def add(self, name: str, state: Literal["pass", "fail", "error", "skip"]) -> None:
        if state == "pass":
            self.passed += 1
            if len(self.passing) < _MAX_NAMES:
                self.passing.append(name)
        elif state == "skip":
            self.skipped += 1
        else:
            if state == "fail":
                self.failed += 1
            else:
                self.errors += 1
            if len(self.failing) < _MAX_NAMES:
                self.failing.append(name)


def _safe_xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ReportError("the report declares a DTD or entities")
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))  # noqa: S314 - no DTD
        parser.feed(data)
        return parser.close()
    except ET.ParseError as exc:
        raise ReportError(f"the report is not valid XML ({exc})") from exc


def _int_attr(element: ET.Element, name: str) -> int | None:
    value = element.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise ReportError(f"the report's {name}={value!r} is not a number") from None


def _case_state(case: ET.Element) -> Literal["pass", "fail", "error", "skip"]:
    tags = {child.tag for child in case}
    if "error" in tags:
        return "error"
    if "failure" in tags:
        return "fail"
    if "skipped" in tags:
        return "skip"
    return "pass"


_COMMENT_COUNT: Final = re.compile(r"\A\s*(tests|pass|fail|skipped|cancelled|todo)\s+(\d+)\s*\Z")


def parse_junit(data: bytes) -> Counts:
    """Counts from a JUnit XML report, checked against the totals the report itself states."""
    root = _safe_xml(data)
    counts = Counts()
    parents = {child: parent for parent in root.iter() for child in parent}
    for case in root.iter("testcase"):
        name = "::".join(filter(None, (case.get("classname"), case.get("name"))))
        counts.add(name or "?", _case_state(case))
    for suite in root.iter("testsuite"):
        if parents.get(suite) is not None and parents[suite].tag == "testsuite":
            continue
        cases = [_case_state(c) for c in suite.iter("testcase")]
        stated = {k: _int_attr(suite, k) for k in ("tests", "failures", "errors", "skipped")}
        found = {
            "tests": len(cases),
            "failures": cases.count("fail"),
            "errors": cases.count("error"),
            "skipped": cases.count("skip"),
        }
        for key, value in stated.items():
            if value is not None and value != found[key]:
                raise ReportError(
                    f"the report is inconsistent: suite {suite.get('name')!r} states {key}="
                    f"{value} but lists {found[key]}"
                )
    comments: dict[str, int] = {}
    for node in root:
        if node.tag is ET.Comment and (m := _COMMENT_COUNT.match(node.text or "")):
            comments[m.group(1)] = int(m.group(2))
    if comments:
        expected = {
            "tests": counts.total,
            "pass": counts.passed,
            "fail": counts.failed + counts.errors,
            "skipped": counts.skipped,
        }
        for key, value in expected.items():
            if key in comments and comments[key] != value:
                raise ReportError(
                    f"the report is inconsistent: it states {key} {comments[key]} but lists {value}"
                )
        if comments.get("cancelled"):
            counts.errors += comments["cancelled"]
    return counts


def _json_lines(data: bytes) -> Iterator[dict[str, object]]:
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield cast(dict[str, object], value)


def parse_go(data: bytes) -> Counts:
    """Counts from `go test -json`: the last terminal action of each test, subtests included."""
    final: dict[tuple[str, str], str] = {}
    started: set[tuple[str, str]] = set()
    packages_failed: list[str] = []
    events = 0
    for event in _json_lines(data):
        action, package, test = event.get("Action"), event.get("Package"), event.get("Test")
        if not isinstance(action, str) or not isinstance(package, str):
            continue
        events += 1
        if isinstance(test, str):
            if action == "run":
                started.add((package, test))
            elif action in ("pass", "fail", "skip"):
                final[(package, test)] = action
        elif action == "fail":
            packages_failed.append(package)
    if not events:
        raise ReportError("the report has no go test events")
    counts = Counts()
    for (package, test), action in final.items():
        state: Literal["pass", "fail", "skip"] = cast(Literal["pass", "fail", "skip"], action)
        counts.add(f"{package}.{test}", state)
    unfinished = sorted(started - final.keys())
    if unfinished and not packages_failed:
        raise ReportError(f"the report has no result for {unfinished[0][1]}")
    for package, test in unfinished:
        counts.add(f"{package}.{test}", "error")
    for package in packages_failed:
        if not any(p == package and a == "fail" for (p, _), a in final.items()):
            counts.add(package, "error")
    return counts


def parse_jest(data: bytes) -> Counts:
    """Counts from a jest or vitest JSON report, checked against the totals it states."""
    try:
        raw: object = json.loads(data)
    except ValueError as exc:
        raise ReportError(f"the report is not JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ReportError("the report is not a JSON object")
    report = cast(dict[str, object], raw)
    counts = Counts()
    suites = report.get("testResults")
    if not isinstance(suites, list):
        raise ReportError("the report has no testResults")
    for suite in cast(list[object], suites):
        if not isinstance(suite, dict):
            raise ReportError("the report has a malformed test file entry")
        entry = cast(dict[str, object], suite)
        results = entry.get("assertionResults")
        cases = cast(list[object], results) if isinstance(results, list) else []
        for case in cases:
            if not isinstance(case, dict):
                raise ReportError("the report has a malformed test entry")
            item = cast(dict[str, object], case)
            name = str(item.get("fullName") or item.get("title") or "?")
            status = item.get("status")
            state = {"passed": "pass", "failed": "fail"}.get(str(status), "skip")
            counts.add(name, cast(Literal["pass", "fail", "skip"], state))
        if entry.get("status") == "failed" and not any(
            isinstance(c, dict) and cast(dict[str, object], c).get("status") == "failed"
            for c in cases
        ):
            counts.add(str(entry.get("name") or "a test file"), "error")
    for key, value in (
        ("numTotalTests", counts.passed + counts.failed + counts.skipped),
        ("numPassedTests", counts.passed),
        ("numFailedTests", counts.failed),
    ):
        stated = report.get(key)
        if isinstance(stated, int) and stated != value:
            raise ReportError(f"the report is inconsistent: {key} is {stated} but it lists {value}")
    errors = report.get("numRuntimeErrorTestSuites")
    if isinstance(errors, int) and errors > counts.errors:
        counts.errors = errors
    return counts


def parse_cargo(data: bytes) -> Counts:
    """Counts from libtest's JSON events, checked against each suite's own totals."""
    counts = Counts()
    suite_totals = [0, 0, 0]
    suites = 0
    for event in _json_lines(data):
        kind, what = event.get("type"), event.get("event")
        if kind == "test" and what in ("ok", "failed", "ignored", "timeout"):
            name = str(event.get("name") or "?")
            state = {"ok": "pass", "ignored": "skip"}.get(str(what), "fail")
            counts.add(name, cast(Literal["pass", "fail", "skip"], state))
        elif kind == "suite" and what in ("ok", "failed"):
            suites += 1
            for i, key in enumerate(("passed", "failed", "ignored")):
                value = event.get(key)
                suite_totals[i] += value if isinstance(value, int) else 0
    if not suites:
        raise ReportError("the report has no libtest suite results")
    if suite_totals != [counts.passed, counts.failed, counts.skipped]:
        raise ReportError("the report is inconsistent: its suite totals differ from its tests")
    return counts


def parse_unittest(data: bytes) -> Counts:
    try:
        raw: object = json.loads(data)
    except ValueError as exc:
        raise ReportError(f"the report is not JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ReportError("the report is not a JSON object")
    report = cast(dict[str, object], raw)
    run, skipped, expected = (report.get(k) for k in ("tests_run", "skipped", "expected_failures"))
    lists = [report.get(k) for k in ("failures", "errors", "unexpected_successes")]
    if not all(isinstance(v, int) for v in (run, skipped, expected)) or not all(
        isinstance(v, list) for v in lists
    ):
        raise ReportError("the report is malformed")
    failures, errors, unexpected = (cast(list[object], v) for v in lists)
    counts = Counts()
    for name in [*failures, *unexpected]:
        counts.add(str(name), "fail")
    for name in errors:
        counts.add(str(name), "error")
    counts.skipped = cast(int, skipped) + cast(int, expected)
    counts.passed = cast(int, run) - counts.failed - counts.errors - counts.skipped
    if counts.passed < 0:
        raise ReportError("the report is inconsistent: more failures than tests run")
    return counts


PARSERS: Final[dict[Runner, Callable[[bytes], Counts]]] = {
    "pytest": parse_junit,
    "node": parse_junit,
    "go": parse_go,
    "jest": parse_jest,
    "vitest": parse_jest,
    "cargo": parse_cargo,
    "unittest": parse_unittest,
}
