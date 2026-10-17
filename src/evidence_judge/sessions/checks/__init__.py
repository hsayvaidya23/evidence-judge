"""Deterministic checkers for session predicates.

Each returns a `PredicateResult`: supported, contradicted, or no evidence, with the reason and
the events it rests on. The soundness rules are in docs/design/coding-agent-sessions.md; in
short, a passing test run proves a pass only when it ran after the last workspace change,
without filters, with a parsed summary, with its own exit status, with its whole output, from a
command line that does not itself print summary text, and with no test-weakening edit anywhere
in the session.
"""

from __future__ import annotations

from evidence_judge.core.verdict import PredicateResult
from evidence_judge.sessions.checks.build import check_build, judge_check_run
from evidence_judge.sessions.checks.commands import check_command
from evidence_judge.sessions.checks.common import (
    CHECKER,
    Result,
    RunOutcome,
    path_matches,
)
from evidence_judge.sessions.checks.edits import check_edit, check_only
from evidence_judge.sessions.checks.index import SessionIndex
from evidence_judge.sessions.checks.rerun import check_rerun, ran_with
from evidence_judge.sessions.checks.results import check_test_result
from evidence_judge.sessions.checks.runs import (
    compile_failure,
    every_failing_test,
    failure_count,
    failure_counts,
    judge_test_run,
    names_test,
    stopped_early,
    suite_size,
)
from evidence_judge.sessions.predicates import (
    BuildPredicate,
    CommandPredicate,
    EditPredicate,
    SessionPredicate,
    TestResultPredicate,
)

__all__ = [
    "CHECKER",
    "Result",
    "RunOutcome",
    "SessionIndex",
    "check",
    "check_build",
    "check_command",
    "check_edit",
    "check_only",
    "check_rerun",
    "check_test_result",
    "compile_failure",
    "every_failing_test",
    "failure_count",
    "failure_counts",
    "judge_check_run",
    "judge_test_run",
    "names_test",
    "path_matches",
    "ran_with",
    "stopped_early",
    "suite_size",
]


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
