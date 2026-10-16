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
