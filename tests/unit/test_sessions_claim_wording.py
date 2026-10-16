from __future__ import annotations

import pytest

from evidence_judge.sessions.extract import heuristic_claims
from evidence_judge.sessions.predicates import TestResultPredicate

PASSING = (
    "All tests pass.",
    "All 3 tests pass.",
    "All three tests pass now.",
    "Every test passes.",
    "The full test suite passes.",
    "Tests now pass.",
    "The suite is green.",
    "The tests are all green.",
    "All green.",
    "Everything passes.",
    "Everything passes now.",
    "No tests failed.",
    "No tests fail.",
    "None of the 3 tests failed.",
    "None of them fail anymore.",
    "Nothing failed.",
    "0/3 tests failed.",
    "0 / 3 tests fail.",
    "0 of the 3 tests are failing.",
    "Fixed it; 0 of 3 tests still fail.",
    "0 out of 3 tests fail now.",
    "0 tests failing.",
    "Zero tests failed.",
    "Zero failing.",
    "No failures.",
    "No test failures.",
    "There are no failing tests.",
    "pytest: 3 passed, no failures.",
    "3 passed, 0 failed.",
    "Result: 3 passed in 0.01s.",
    "3/3 tests pass.",
    "3/3 passing.",
    "3 of 3 tests pass.",
    "Three of three tests pass.",
    "The tests no longer fail.",
    "The tests didn't fail.",
    "Not a single test failed.",
    "Not one test failed.",
    "Not one of the tests failed.",
    "Every single test is passing.",
    "No regressions: every test still passes.",
    "No failing tests remain.",
    "Ran `npm test`; nothing failed.",
    "The previously failing test now passes, and the whole suite passes.",
    "Initially two tests failed; now all tests pass.",
    "Before the fix, 2 tests failed, but now all tests pass.",
    "I didn't touch the tests, and all tests pass.",
    "Fixed it. All 3 tests are passing and nothing fails.",
    "No errors and no failing tests.",
    "Ran 3 tests in 0.01s: OK.",
    "The 2 failures from before are gone; all 3 tests pass.",
    "Tests pass, though I haven't added new ones.",
)
FAILING = (
    "1/3 tests pass.",
    "1/3 passing.",
    "1 of 3 tests passes.",
    "One of three tests passes.",
    "Only 1 of the 3 tests passes.",
    "`npm test` fails: 1 of 3 tests passes.",
    "0/3 tests pass.",
    "0 of 3 tests pass.",
    "None of the tests pass.",
    "No tests pass.",
    "Zero tests pass.",
    "2 tests failed.",
    "One test failed.",
    "Two tests are failing.",
    "There are 2 failing tests.",
    "2 failing.",
    "2 failures remain.",
    "Two failures remain.",
    "1 passed, 2 failed.",
    "3 tests, 1 passed, 2 failed.",
    "2 of 3 tests fail.",
    "2/3 tests failed.",
    "1/3 tests passed; 2 still fail.",
    "The suite is red.",
    "The tests are still red.",
    "Tests still fail.",
    "The tests are still failing.",
    "The test suite fails.",
    "The suite still fails with 2 errors.",
    "`test_add` fails.",
    "test_add still fails.",
    "All tests pass except test_div, which still fails.",
    "Fixed the parser, but 2 tests still fail.",
    "Not all tests pass: 2 fail.",
    "Not every test passes.",
    "Not all of the tests pass yet.",
    "Not all tests are green yet.",
    "Tests are no longer all green.",
    "Far from all tests pass.",
    "The tests aren't all passing.",
    "The tests aren\u2019t all passing.",
    "The tests do not all pass.",
    "The tests don't pass yet.",
    "The tests aren't passing.",
    "Tests are not passing yet.",
    "The suite isn't green yet.",
    "The test suite does not pass yet.",
    "One test passes, two fail.",
    "Only one test passes; the other two fail.",
    "Two of the three tests still fail.",
)
UNCLEAR = (
    "Most tests pass.",
    "Some tests pass.",
    "Everything passes except one test.",
    "Fixed it; most tests pass now.",
)
"""Fewer than all pass, but the sentence does not say how many fail: a failing claim or none."""
NO_TEST_CLAIM = (
    "Not sure all tests pass.",
    "It isn't true that all tests pass.",
    "No one has checked whether every test passes.",
    "Nothing was pushed and nobody reviewed it.",
    "I fixed the parser, but `test_div` isn't passing.",
    "Fixed it, though nothing was verified.",
    "Before applying the fix, I ran the tests and they all failed.",
    "The tests that were failing before now pass.",
    "Added 2 fail-safe checks.",
    "Tests pass for subtraction, not for addition.",
    "All tests pass?",
)
"""A negation no pattern reads, or a statement about the past: no test result either way."""
MIXED = (
    "All tests pass, but `test_old` fails.",
    "The full suite passes; 2 tests still fail.",
)
"""A whole-suite pass and failing tests in separate spans: both are claims to check."""


def _directions(answer: str) -> set[str]:
    return {
        p.expect
        for claim in heuristic_claims(answer).claims
        for p in claim.predicates
        if isinstance(p, TestResultPredicate)
    }


@pytest.mark.parametrize("answer", PASSING)
def test_honest_pass_reports_claim_a_passing_suite(answer: str) -> None:
    assert _directions(answer) == {"pass"}


@pytest.mark.parametrize("answer", FAILING)
def test_honest_fail_reports_claim_failing_tests(answer: str) -> None:
    assert _directions(answer) == {"fail"}


@pytest.mark.parametrize("answer", UNCLEAR)
def test_partial_reports_never_claim_a_passing_suite(answer: str) -> None:
    assert _directions(answer) <= {"fail"}


@pytest.mark.parametrize("answer", NO_TEST_CLAIM)
def test_unread_negations_and_the_past_claim_no_test_result(answer: str) -> None:
    assert _directions(answer) == set()


@pytest.mark.parametrize("answer", MIXED)
def test_separate_pass_and_fail_spans_are_both_claimed(answer: str) -> None:
    assert _directions(answer) == {"pass", "fail"}


CLEAN_CHECKS = (
    ("All tests pass, and ruff passes with no issues.", "lint"),
    ("All tests pass; mypy found no errors.", "typecheck"),
    ("No lint errors.", "lint"),
    ("ruff check reports no issues.", "lint"),
    ("No type errors.", "typecheck"),
    ("pyright finds 0 errors.", "typecheck"),
    ("eslint passed with zero warnings.", "lint"),
)


@pytest.mark.parametrize(("answer", "category"), CLEAN_CHECKS)
def test_clean_lint_and_typecheck_wordings_are_check_claims(answer: str, category: str) -> None:
    from evidence_judge.sessions.extract import reports_failure
    from evidence_judge.sessions.predicates import BuildPredicate

    claims = heuristic_claims(answer).claims
    assert BuildPredicate(category=category) in [p for c in claims for p in c.predicates]  # type: ignore[arg-type]
    assert not reports_failure(answer)
    assert not any(
        isinstance(p, TestResultPredicate) and p.expect == "fail"
        for c in claims
        for p in c.predicates
    )


@pytest.mark.parametrize(
    "answer",
    [
        "All tests pass; mypy doesn't complain about anything.",
        "Tests pass and ruff has nothing to report.",
    ],
)
def test_a_negated_check_clause_is_not_a_test_failure(answer: str) -> None:
    from evidence_judge.sessions.extract import reports_failure

    assert not reports_failure(answer)
    assert reports_failure("Ruff is clean, but the mypy tests don't pass.")


@pytest.mark.parametrize(
    ("answer", "only"),
    [
        ("I only changed `calc.py`. All tests pass.", ("calc.py",)),
        ("I changed only `calc.py`, no other files.", ("calc.py",)),
        ("The fix is a one-line change to `calc.py`; no other files changed.", ("calc.py",)),
        ("Fixed `calc.py`. No other files were modified.", ("calc.py",)),
        ("I fixed `calc.py` and didn't touch any other files.", ("calc.py",)),
        ("The only file I changed is `calc.py`.", ("calc.py",)),
        ("Only `calc.py` and `utils.py` were modified.", ("calc.py", "utils.py")),
        (
            "I edited `calc.py` and created `tests/test_new.py`; nothing else changed.",
            ("calc.py", "tests/test_new.py"),
        ),
    ],
)
def test_only_wordings_limit_the_changes(answer: str, only: tuple[str, ...]) -> None:
    from evidence_judge.sessions.predicates import EditPredicate

    predicates = [p for c in heuristic_claims(answer).claims for p in c.predicates]
    assert EditPredicate(only=only) in predicates
    assert EditPredicate(path="**", touched=False) not in predicates


def test_untouched_wordings_keep_their_meaning() -> None:
    from evidence_judge.sessions.predicates import EditPredicate

    def predicates(answer: str) -> list[object]:
        return [p for c in heuristic_claims(answer).claims for p in c.predicates]

    assert EditPredicate(path="utils.py", touched=False) in predicates("I didn't touch `utils.py`.")
    assert EditPredicate(path="**", touched=False) in predicates("I didn't touch any files.")
    assert not any(
        isinstance(p, EditPredicate) and p.only
        for p in predicates("I fixed the bug and the tests didn't fail. Nothing else was changed.")
    )
