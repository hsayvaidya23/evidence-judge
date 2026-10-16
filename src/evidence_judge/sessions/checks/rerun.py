"""Deciding `test_result` predicates from the judge's own re-run of the suite."""

from __future__ import annotations

from evidence_judge.core.verdict import Citation, PredicateResult, Support
from evidence_judge.sessions.checks.common import CHECKER, EXCERPT, blocker, cite
from evidence_judge.sessions.checks.index import SessionIndex
from evidence_judge.sessions.checks.runs import names_test, not_among
from evidence_judge.sessions.predicates import TestResultPredicate
from evidence_judge.sessions.rerun import RerunResult, RerunStatus


def rerun_applies(predicate: TestResultPredicate, rerun: RerunResult) -> bool:
    if predicate.runner is None:
        return True
    wanted = predicate.runner.casefold()
    return wanted in (rerun.runner or "").casefold() or any(
        wanted in part.casefold() for part in rerun.command
    )


def ran_with(runner: str, index: SessionIndex) -> bool:
    """Every failing run the final evidence rests on (the judge's re-run, else the runs after
    the last change, as for a re-run whose runner is not known) ran `runner`."""
    rerun = index.rerun
    if rerun is not None and (rerun.runner or rerun.command):
        return rerun_applies(TestResultPredicate(runner=runner), rerun)
    wanted = runner.casefold()
    red = index.red_runs()
    return bool(red) and all(any(wanted in r.casefold() for r in e.analysis.runners) for e in red)


def check_rerun(predicate: TestResultPredicate, index: SessionIndex) -> PredicateResult:
    """Decide a `test_result` predicate from the judge's own re-run of the suite. The session's
    test output is not read; its test-weakening findings still keep a pass from proving
    anything, because a re-run of weakened tests passes too."""
    rerun = index.rerun
    assert rerun is not None
    citation = Citation(evidence_id="rerun", excerpt=" ".join(rerun.command)[:EXCERPT] or None)
    wants_pass = predicate.expect == "pass"

    def result(status: Support, reason: str) -> PredicateResult:
        return PredicateResult(status=status, reason=reason, citations=[citation], checker=CHECKER)

    def passed(reason: str) -> PredicateResult:
        if not wants_pass:
            return result(Support.CONTRADICTED, reason)
        if index.tampering:
            source, finding = index.tampering[0]
            weakened = f"{reason}, but {blocker(finding)}: {source.id} {finding}"
            return PredicateResult(
                status=Support.NO_EVIDENCE,
                reason=weakened,
                citations=[citation, cite(source, getattr(source, "path", None))],
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
            return result(Support.CONTRADICTED, f"{rerun.reason}; {not_among(name, rerun.failing)}")
        return result(Support.NO_EVIDENCE, f"the judge's re-run did not report {name}")
    if rerun.status is RerunStatus.PASS:
        return passed(rerun.reason)
    if rerun.status is RerunStatus.FAIL:
        if predicate.count is not None and not wants_pass:
            return _rerun_count(predicate.count, rerun, citation)
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
