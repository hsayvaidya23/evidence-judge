"""Deciding `test_result` predicates from the test runs the session recorded."""

from __future__ import annotations

from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.sessions.checks.common import Result, RunOutcome, blocker, result
from evidence_judge.sessions.checks.index import SessionIndex
from evidence_judge.sessions.checks.rerun import check_rerun, rerun_applies
from evidence_judge.sessions.checks.runs import (
    failure_counts,
    incomplete,
    mentions,
    named_failure,
    not_failing,
)
from evidence_judge.sessions.predicates import TestResultPredicate


def check_test_result(predicate: TestResultPredicate, index: SessionIndex) -> PredicateResult:
    if index.rerun is not None and rerun_applies(predicate, index.rerun):
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
        return result(Support.NO_EVIDENCE, f"no {what}test run is recorded")
    fresh = [r for r in runs if index.fresh(r)]
    last_change = index.last_change
    if not fresh:
        assert last_change is not None
        return result(
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
        fails = [o for o in outcomes if o.result is Result.FAIL and named_failure(o.event, name)]
        passes = [
            o for o in outcomes if o.result is Result.PASS and (o.whole or mentions(o.event, name))
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
    if predicate.mentions and red and (spared := not_failing(red[-1], predicate.mentions)):
        return result(Support.CONTRADICTED, spared.reason, spared.event)
    if whole_pass:
        return result(Support.CONTRADICTED, whole_pass[-1].reason, whole_pass[-1].event)
    return _undecided(outcomes, predicate)


def _failing_count(predicate: TestResultPredicate, fail: RunOutcome) -> PredicateResult:
    """A failing run supports a claim that tests fail, if it failed as many as the claim
    says: a count needs a whole-suite run that ran to the end, since a filtered run or one
    that stopped early leaves some tests unrun."""
    if predicate.count is None:
        return result(Support.SUPPORTED, fail.reason, fail.event)
    if why := incomplete(fail):
        return result(
            Support.NO_EVIDENCE,
            f"{fail.reason}, but the run cannot show how many tests of the suite fail: {why}",
            fail.event,
        )
    counts = failure_counts(fail.event)
    if not counts:
        return result(
            Support.NO_EVIDENCE,
            f"{fail.reason}, but the run does not show how many tests failed",
            fail.event,
        )
    if predicate.count not in counts:
        return result(
            Support.CONTRADICTED,
            f"{fail.reason}: {' or '.join(map(str, counts))} tests failed, not {predicate.count}",
            fail.event,
        )
    return result(Support.SUPPORTED, fail.reason, fail.event)


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
        return result(Support.CONTRADICTED, fails[-1].reason, fails[-1].event)
    if not passes and (spared := _spared(predicate, outcomes)) is not None:
        passes = [spared]
    if not passes:
        return _undecided(outcomes, predicate)
    if index.tampering:
        source, finding = index.tampering[0]
        return result(
            Support.NO_EVIDENCE,
            f"{passes[-1].reason}, but {blocker(finding)}: {source.id} {finding}",
            passes[-1].event,
            source,
        )
    return result(Support.SUPPORTED, passes[-1].reason, passes[-1].event)


def _spared(predicate: TestResultPredicate, outcomes: list[RunOutcome]) -> RunOutcome | None:
    """A failing whole-suite run that ran to the end and names every test it failed, and not
    the one the claim says passes."""
    red = [o for o in outcomes if o.result is Result.FAIL]
    if not predicate.mentions or not red:
        return None
    return not_failing(red[-1], predicate.mentions)


def _flaky(fail: RunOutcome, passed: RunOutcome) -> PredicateResult:
    return result(
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
        return result(
            Support.NO_EVIDENCE,
            f"only a subset of the suite ran after the last change ({o.event.id}: {why}); a "
            "filtered run cannot prove the whole suite passes",
            o.event,
        )
    unknown = [o for o in outcomes if o.result is Result.UNKNOWN]
    if unknown:
        return result(Support.NO_EVIDENCE, unknown[-1].reason, unknown[-1].event)
    if predicate.mentions:
        return result(
            Support.NO_EVIDENCE, f"no test run after the last change mentions {predicate.mentions}"
        )
    return result(Support.NO_EVIDENCE, "no test run after the last change decides the claim")
