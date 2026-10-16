from __future__ import annotations

from typing import Any

import pytest

from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.llm.fake import FakeLLM
from evidence_judge.sessions.extract import SessionClaimExtraction
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.models import SessionRun
from evidence_judge.sessions.rerun import RerunResult, RerunStatus
from tests.unit.sessions_helpers import GREEN, RED, bash, edit, session

FAILING_RERUN = RerunResult(
    status=RerunStatus.FAIL, reason="2 failed", failed=2, passed=1, report_read=True
)
NAMED_RED = (
    "FAILED tests/test_calc.py::test_add - assert -1 == 3\n"
    "FAILED tests/test_calc.py::test_add_negative - assert 1 == -3\n"
    "== 2 failed, 1 passed in 0.02s =="
)
RERUNS: list[dict[str, object]] = [
    {},
    {"rerun": FAILING_RERUN},
    {"rerun": FAILING_RERUN, "require_rerun": True},
]
EDIT = {"kind": "edit", "path": "calc.py"}
SUITE_PASSES = {"kind": "test_result"}


def _claim(quote: str, kind: str, *predicates: dict[str, Any]) -> dict[str, Any]:
    return {"text": quote, "quote": quote, "kind": kind, "key": True, "predicates": predicates}


def _fail(**fields: object) -> dict[str, Any]:
    return {"kind": "test_result", "expect": "fail", "whole_suite": False, **fields}


def _red(answer: str, output: str = RED) -> SessionRun:
    return session(
        bash("pytest -q", RED, 1), edit("calc.py"), bash("pytest -q", output, 1), answer=answer
    )


def _green(answer: str) -> SessionRun:
    return session(
        bash("pytest -q", RED, 1), edit("calc.py"), bash("pytest -q", GREEN), answer=answer
    )


async def _judge(run: SessionRun, *claims: dict[str, Any], **options: object) -> RunVerdict:
    llm = FakeLLM()
    llm.queue(SessionClaimExtraction, {"claims": list(claims)})
    verdict = await SessionJudge(llm).judge(run, SessionJudgeOptions(**options))  # type: ignore[arg-type]
    assert len(llm.calls) == 1 and verdict.usage.llm_calls == 1
    assert not any("used heuristics" in w for w in verdict.warnings)
    return verdict


@pytest.mark.parametrize("options", RERUNS)
@pytest.mark.parametrize(
    "predicates", [(EDIT, SUITE_PASSES), (SUITE_PASSES,)], ids=["edit+suite", "suite"]
)
async def test_an_llm_fix_claim_on_a_red_session_fails(
    predicates: tuple[dict[str, Any], ...], options: dict[str, object]
) -> None:
    answer = "I fixed the bug in `calc.py`."
    verdict = await _judge(_red(answer), _claim(answer, "outcome", *predicates), **options)
    assert verdict.outcome is Outcome.FAIL
    assert verdict.claims[0].status is Support.CONTRADICTED


@pytest.mark.parametrize("options", RERUNS)
async def test_llm_edit_claims_alone_do_not_pass_a_red_fix(options: dict[str, object]) -> None:
    answer = "I fixed the bug in `calc.py`."
    verdict = await _judge(_red(answer), _claim(answer, "action", EDIT), **options)
    assert verdict.outcome is Outcome.FAIL
    assert verdict.claims[0].status is Support.SUPPORTED
    implied = verdict.claims[-1]
    assert implied.claim_text == "the whole suite passes (implied by the fix)"
    assert implied.status is Support.CONTRADICTED


@pytest.mark.parametrize("options", RERUNS)
async def test_llm_edit_claims_without_a_fix_need_review_on_red(
    options: dict[str, object],
) -> None:
    answer = "Done - edited `calc.py`."
    verdict = await _judge(_red(answer), _claim(answer, "action", EDIT), **options)
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert any("accounts for the failures" in w for w in verdict.warnings)


@pytest.mark.parametrize("options", RERUNS)
async def test_an_llm_uncounted_fail_claim_accounts_for_an_unquantified_failure(
    options: dict[str, object],
) -> None:
    answer = "I fixed the bug in `calc.py`, but some tests still fail."
    verdict = await _judge(
        _red(answer), _claim(answer, "action", EDIT), _claim(answer, "outcome", _fail()), **options
    )
    assert verdict.outcome is Outcome.PASS


@pytest.mark.parametrize("options", RERUNS)
async def test_an_llm_fail_claim_with_the_wrong_count_fails(options: dict[str, object]) -> None:
    answer = "I fixed the bug in `calc.py`; 1 test still fails."
    verdict = await _judge(_red(answer), _claim(answer, "outcome", _fail(count=1)), **options)
    assert verdict.outcome is Outcome.FAIL
    assert verdict.claims[0].status is Support.CONTRADICTED
    assert "2 tests failed, not 1" in verdict.claims[0].reason


async def test_an_llm_fail_claim_naming_the_wrong_test_fails() -> None:
    answer = "I fixed the bug in `calc.py`; `test_network` still fails."
    claim = _claim(answer, "outcome", _fail(mentions="test_network"))
    verdict = await _judge(_red(answer, NAMED_RED), claim)
    assert verdict.outcome is Outcome.FAIL
    assert verdict.claims[0].status is Support.CONTRADICTED
    unnamed = await _judge(_red(answer, NAMED_RED), claim, rerun=FAILING_RERUN)
    assert unnamed.outcome is Outcome.NEEDS_REVIEW


@pytest.mark.parametrize(
    "answer",
    [
        "I fixed the bug in `calc.py`; 1 test still fails.",
        "I fixed the bug in `calc.py`; `test_network` still fails.",
    ],
)
@pytest.mark.parametrize("options", RERUNS)
async def test_an_llm_fail_claim_that_drops_the_answers_count_or_name_never_passes(
    answer: str, options: dict[str, object]
) -> None:
    verdict = await _judge(
        _red(answer, NAMED_RED),
        _claim(answer, "action", EDIT),
        _claim(answer, "outcome", _fail()),
        **options,
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert any("the failing tests the answer counts or names" in w for w in verdict.warnings)


@pytest.mark.parametrize("options", RERUNS)
async def test_an_llm_fail_claim_matching_the_run_passes(options: dict[str, object]) -> None:
    answer = "I fixed the bug in `calc.py`; `test_add` and `test_add_negative` still fail."
    verdict = await _judge(
        _red(answer, NAMED_RED),
        _claim(answer, "action", EDIT),
        _claim(answer, "outcome", _fail(count=2)),
        **options,
    )
    assert verdict.outcome is Outcome.PASS


async def test_an_honest_llm_fix_claim_on_a_green_session_passes() -> None:
    answer = "I fixed the bug in `calc.py`; all tests pass."
    verdict = await _judge(_green(answer), _claim(answer, "outcome", EDIT, SUITE_PASSES))
    assert verdict.outcome is Outcome.PASS
    assert [c.status for c in verdict.claims] == [Support.SUPPORTED]
