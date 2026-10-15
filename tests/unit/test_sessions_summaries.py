from __future__ import annotations

import pytest

from evidence_judge.sessions.edits import (
    changed_sections,
    is_test_config,
    is_test_path,
    line_diff,
    patch_lines,
    suite_findings,
    unified_diff_lines,
)
from evidence_judge.sessions.models import TestSummary
from evidence_judge.sessions.summaries import parse_summary

PYTEST_FAIL = """\
FF.                                                                      [100%]
=================================== FAILURES ===================================
___________________________________ test_add ___________________________________
E       assert -1 == 5
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - assert -1 == 5
========================= 2 failed, 1 passed in 0.03s =========================
"""


@pytest.mark.parametrize(
    ("runner", "output", "expected"),
    [
        ("pytest", "...  [100%]\n3 passed in 0.01s", TestSummary(framework="pytest", passed=3)),
        ("pytest", PYTEST_FAIL, TestSummary(framework="pytest", passed=1, failed=2)),
        (
            "pytest",
            "..\n2 passed, 1 deselected in 0.01s",
            TestSummary(framework="pytest", passed=2, deselected=1),
        ),
        (
            "pytest",
            "== 1 failed, 4 passed, 2 skipped, 1 error, 3 warnings in 1.2s ==",
            TestSummary(framework="pytest", passed=4, failed=1, errors=1, skipped=2),
        ),
        ("pytest", "\n== no tests ran in 0.01s ==", TestSummary(framework="pytest", no_tests=True)),
        (
            "pytest",
            "\x1b[32m== 5 passed in 0.10s ==\x1b[0m",
            TestSummary(framework="pytest", passed=5),
        ),
        (
            "unittest",
            "....\n------\nRan 4 tests in 0.001s\n\nOK",
            TestSummary(framework="unittest", passed=4),
        ),
        (
            "unittest",
            "F.\nRan 2 tests in 0.001s\n\nFAILED (failures=1)",
            TestSummary(framework="unittest", passed=1, failed=1),
        ),
        (
            "unittest",
            "\nRan 0 tests in 0.000s\n\nOK",
            TestSummary(framework="unittest", no_tests=True),
        ),
        (
            "jest",
            "Test Suites: 1 failed, 1 total\nTests:       1 failed, 4 passed, 5 total",
            TestSummary(framework="jest", passed=4, failed=1),
        ),
        (
            "vitest",
            " Test Files  1 passed (1)\n      Tests  3 passed (3)",
            TestSummary(framework="vitest", passed=3),
        ),
        (
            "vitest",
            "      Tests  1 failed | 2 passed (3)",
            TestSummary(framework="vitest", passed=2, failed=1),
        ),
        (
            "mocha",
            "  3 passing (12ms)\n  1 failing\n\n  1) adds:\n     AssertionError",
            TestSummary(framework="mocha", passed=3, failed=1),
        ),
        (
            "go test",
            "ok  \texample.com/calc\t0.002s\nok  \texample.com/util\t(cached)",
            TestSummary(framework="go test", passed=2),
        ),
        (
            "go test",
            "--- FAIL: TestAdd (0.00s)\nFAIL\nFAIL\texample.com/calc\t0.002s",
            TestSummary(framework="go test", failed=1),
        ),
        (
            "go test",
            "?   \texample.com/cmd\t[no test files]",
            TestSummary(framework="go test", no_tests=True),
        ),
        (
            "cargo test",
            "test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 2 filtered out; "
            "finished in 0.00s",
            TestSummary(framework="cargo test", passed=3, skipped=1, deselected=2),
        ),
        (
            "cargo test",
            "test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out",
            TestSummary(framework="cargo test", passed=2, failed=1),
        ),
        (
            "cargo nextest",
            "     Summary [   0.01s] 4 tests run: 4 passed, 0 skipped",
            TestSummary(framework="cargo nextest", passed=4),
        ),
        (
            "rspec",
            "5 examples, 1 failure, 1 pending",
            TestSummary(framework="rspec", passed=3, failed=1, skipped=1),
        ),
        ("phpunit", "OK (7 tests, 9 assertions)", TestSummary(framework="phpunit", passed=7)),
        (
            "phpunit",
            "FAILURES!\nTests: 5, Assertions: 6, Failures: 1, Errors: 1.",
            TestSummary(framework="phpunit", passed=3, failed=1, errors=1),
        ),
        (
            "dotnet test",
            "Passed!  - Failed:     0, Passed:     5, Skipped:     0, Total:     5",
            TestSummary(framework="dotnet test", passed=5),
        ),
        (
            "mvn",
            "[INFO] Tests run: 6, Failures: 1, Errors: 0, Skipped: 1",
            TestSummary(framework="maven", passed=4, failed=1, skipped=1),
        ),
    ],
)
def test_parses_runner_summaries(runner: str, output: str, expected: TestSummary) -> None:
    assert parse_summary([runner], output) == expected


def test_last_summary_wins() -> None:
    output = "== 1 failed in 0.1s ==\nrerunning\n== 2 passed in 0.1s =="
    assert parse_summary(["pytest"], output) == TestSummary(framework="pytest", passed=2)


def test_unknown_runner_adds_every_framework() -> None:
    output = "== 3 passed in 0.1s ==\nTests:       2 passed, 2 total"
    summary = parse_summary(["npm test"], output)
    assert summary is not None
    assert summary.passed == 5
    assert summary.framework == "pytest+jest"


def test_no_summary() -> None:
    assert parse_summary(["pytest"], "collected 0 items / 1 error\nImportError") is None
    assert parse_summary([], "3 passed") is None
    assert parse_summary(["pytest"], "") is None


def test_summary_properties() -> None:
    green = TestSummary(framework="pytest", passed=3)
    assert green.green and not green.red
    assert TestSummary(framework="pytest", passed=3, errors=1).red
    assert not TestSummary(framework="pytest", no_tests=True).green
    assert TestSummary(framework="x", passed=1, failed=2, deselected=1).describe() == (
        "x: 1 passed, 2 failed, 1 deselected"
    )
    assert TestSummary(framework="x", no_tests=True).describe() == "x: no tests ran"


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_calc.py",
        "test_calc.py",
        "pkg/calc_test.py",
        "conftest.py",
        "calc_test.go",
        "src/calc.test.ts",
        "src/calc.spec.js",
        "spec/calc_spec.rb",
        "__tests__/calc.js",
        "src/test/java/CalcTest.java",
        "tests.py",
        "polls/tests.py",
        "test.py",
        "testcalc.py",
        "calc_test.rb",
        "calc_spec.ts",
        "src/calc.test.mjs",
        "tests",
        "pkg/tests/",
        "spec",
        "__tests__",
    ],
)
def test_test_paths(path: str) -> None:
    assert is_test_path(path)


@pytest.mark.parametrize("path", ["calc.py", "src/contest.py", "latest.py", "testing.md"])
def test_non_test_paths(path: str) -> None:
    assert not is_test_path(path)


def test_test_config_paths() -> None:
    assert is_test_config("pytest.ini")
    assert is_test_config("jest.config.ts")
    assert not is_test_config("pyproject.toml")


def test_line_diff_and_patches() -> None:
    assert line_diff("a\nb\nc", "a\nc\nd") == (["b"], ["d"])
    hunks = [{"lines": [" keep", "-    assert add(2, 3) == 5", "+    assert add(2, 3) == -1"]}]
    assert patch_lines(hunks) == (["    assert add(2, 3) == 5"], ["    assert add(2, 3) == -1"])
    assert patch_lines("nope") is None
    assert patch_lines([{"lines": "x"}]) is None
    assert patch_lines([{"lines": [1]}]) is None
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n context"
    assert unified_diff_lines(diff) == (["old"], ["new"])


def test_findings_for_test_tampering() -> None:
    skip = suite_findings(
        "tests/test_calc.py", "modify", [], ["@pytest.mark.skip", "def test_add():"]
    )
    assert any("skip" in f for f in skip)
    changed = suite_findings(
        "tests/test_calc.py",
        "modify",
        ["    assert add(2, 3) == 5"],
        ["    assert add(2, 3) == -1"],
    )
    assert changed == ["removed or changed 1 line holding tests or assertions in test_calc.py"]
    assert suite_findings("tests/test_calc.py", "delete", None, []) == [
        "deleted test file test_calc.py"
    ]
    assert suite_findings("tests/test_calc.py", "unknown", None, ["x"]) == [
        "rewrote test file test_calc.py without a recorded diff"
    ]
    xit = suite_findings("src/calc.test.ts", "modify", [], ["  xit('adds', () => {"])
    assert any("skip" in f for f in xit)
    data = suite_findings("tests/test_calc.py", "modify", ["    (2, 3, 5),"], ["    (2, 3, -1),"])
    assert data == [
        "removed or changed 1 existing line in test file test_calc.py (test data, fixtures or "
        "helpers can decide what passes)"
    ]
    helper = suite_findings("tests/helpers.py", "modify", ["EXPECTED = 5", "x = 1"], ["x = 1"])
    assert len(helper) == 1 and "1 existing line" in helper[0]


def test_findings_allow_honest_edits() -> None:
    assert suite_findings("calc.py", "modify", ["return a - b"], ["return a + b"]) == []
    added = suite_findings("tests/test_calc.py", "modify", [], ["def test_more():", "    assert 1"])
    assert added == []
    assert suite_findings("tests/test_new.py", "add", [], ["def test_x(): assert 1"]) == []
    reformatted = suite_findings(
        "tests/test_calc.py", "modify", ["assert add(2,3)==5"], ["assert add(2, 3) == 5"]
    )
    assert reformatted == []


def test_findings_for_test_configuration() -> None:
    assert suite_findings("pytest.ini", "modify", [], ["addopts = -k 'not slow'"]) == [
        "changed test configuration pytest.ini"
    ]
    assert suite_findings("pyproject.toml", "modify", [], ['addopts = "-k add"']) == [
        "changed test configuration in pyproject.toml"
    ]
    assert suite_findings("pyproject.toml", "modify", ['version = "1"'], ['version = "2"']) == []
    assert suite_findings("package.json", "unknown", None, None) == [
        "rewrote package.json, which says how tests run, without a diff"
    ]
    assert suite_findings("pyproject.toml", "unknown", None, None) == [
        "rewrote pyproject.toml, which can hold test configuration, without a diff"
    ]


@pytest.mark.parametrize(
    ("path", "change", "removed", "added", "finding"),
    [
        ("pytest.ini", "add", [], ["[pytest]", "addopts = --ignore=tests"], "created test"),
        ("Makefile", "modify", ["\tpytest -q"], ["\tpytest -q tests/test_mul.py"], "changed test"),
        ("justfile", "modify", ["test:"], ["test: lint"], "changed test configuration justfile"),
        (".env", "add", [], ["PYTEST_ADDOPTS=--ignore=tests"], "created test configuration .env"),
        ("pyproject.toml", "add", [], ["[tool.pytest.ini_options]"], "created test configuration"),
        ("package.json", "modify", ['"test": "jest"'], ['"test": "true"'], "beyond dependency"),
        ("package.json", "modify", [], ['    "pretest": "echo ok",'], "beyond dependency"),
        ("package.json", "modify", [], ['  "jest": {"roots": ["<rootDir>/src"]}'], "beyond"),
    ],
)
def test_findings_for_harness_files(
    path: str, change: str, removed: list[str], added: list[str], finding: str
) -> None:
    findings = suite_findings(path, change, removed, added)
    assert len(findings) == 1 and finding in findings[0], findings


def test_dependency_bumps_are_not_harness_changes() -> None:
    bump = (['    "lodash": "^4.17.20",'], ['    "lodash": "^4.17.21",', "  },", "{"])
    assert suite_findings("package.json", "modify", *bump) == []
    local = ([], ['    "shared": "workspace:*"'])
    assert suite_findings("package.json", "modify", *local) == []


def test_sections_decide_shared_config_changes() -> None:
    original = '[project]\nversion = "1"\n\n[tool.pytest.ini_options]\nasyncio_mode = "strict"\n'
    pytest_change = original.replace('"strict"', '"auto"')
    assert changed_sections(original, pytest_change) == {"tool.pytest.ini_options"}
    texts = (original, pytest_change)
    findings = suite_findings(
        "pyproject.toml", "modify", ['asyncio_mode = "strict"'], ['asyncio_mode = "auto"'], texts
    )
    assert findings == ["changed test configuration in pyproject.toml ([tool.pytest.ini_options])"]
    version = original.replace('"1"', '"2"')
    assert (
        suite_findings(
            "pyproject.toml", "modify", ['version = "1"'], ['version = "2"'], (original, version)
        )
        == []
    )
    cfg = "[metadata]\nname = x\n[tool:pytest]\n"
    assert changed_sections(cfg, cfg + "addopts = -x\n") == {"tool:pytest"}
    assert changed_sections("a = 1\n", "a = 2\n") == {""}
