from __future__ import annotations

from typing import Any

import pytest

from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.llm.fake import FakeLLM
from evidence_judge.sessions.extract import SessionClaimExtraction
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.models import SessionRun
from evidence_judge.sessions.rerun import RerunResult, RerunStatus
from evidence_judge.sessions.shell import analyse
from tests.unit.sessions_helpers import bash, edit, session

FIX = "I fixed the bug in `calc.py`."
RED2N = (
    "FF.\nFAILED tests/test_calc.py::test_add - assert\n"
    "FAILED tests/test_calc.py::test_sub - assert\n== 2 failed, 1 passed in 0.02s =="
)
STOPPED = (
    "F\nFAILED tests/test_calc.py::test_add\n!!! stopping after 1 failures !!!\n"
    "== 1 failed in 0.02s =="
)
FAILING_RERUN = RerunResult(
    status=RerunStatus.FAIL, reason="2 failed", failed=2, passed=1, report_read=True
)
RERUNS: list[dict[str, object]] = [
    {},
    {"rerun": FAILING_RERUN},
    {"rerun": FAILING_RERUN, "require_rerun": True},
]
EDIT = {"kind": "edit", "path": "calc.py"}


def _claim(quote: str, kind: str, *predicates: dict[str, Any]) -> dict[str, Any]:
    return {"text": quote, "quote": quote, "kind": kind, "key": True, "predicates": predicates}


def _fail(**fields: object) -> dict[str, Any]:
    return {"kind": "test_result", "expect": "fail", "whole_suite": False, **fields}


def _passes(name: str) -> dict[str, Any]:
    return {"kind": "test_result", "expect": "pass", "whole_suite": False, "mentions": name}


async def _offline(run: SessionRun, **options: object) -> RunVerdict:
    return await SessionJudge().judge(run, SessionJudgeOptions(offline=True, **options))  # type: ignore[arg-type]


async def _llm(run: SessionRun, *claims: dict[str, Any], **options: object) -> RunVerdict:
    llm = FakeLLM()
    llm.queue(SessionClaimExtraction, {"claims": list(claims)})
    verdict = await SessionJudge(llm).judge(run, SessionJudgeOptions(**options))  # type: ignore[arg-type]
    assert len(llm.calls) == 1
    return verdict


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q -x",
        "pytest -qx",
        "python -m pytest --maxfail=2",
        "pytest --exitfirst tests",
        "uv run pytest -x",
        "python -m unittest -f",
        "npx jest --bail",
        "go test -failfast ./...",
        "cargo test",
        "mvn test",
        "./gradlew test",
    ],
)
def test_runs_that_stop_at_their_first_failures(command: str) -> None:
    assert analyse(command).stop_early is not None


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "pytest -q -k add",
        "go test ./...",
        "npx jest",
        "cargo test --no-fail-fast",
        "mvn -fae test",
        "gradle test --continue",
    ],
)
def test_runs_that_run_every_test(command: str) -> None:
    assert analyse(command).stop_early is None


@pytest.mark.parametrize("command", ["pytest -q -x", "pytest -q --maxfail=1", "make test"])
@pytest.mark.parametrize(
    "answer",
    [
        f"{FIX} `test_sub` passes; `test_add` still fails.",
        "`test_sub` passes now.",
        f"{FIX} 1 test still fails: `test_add`.",
        f"{FIX} Only `test_add` still fails.",
    ],
)
@pytest.mark.parametrize("options", RERUNS)
async def test_a_run_that_stopped_early_proves_no_other_test_passes_nor_how_many_fail(
    command: str, answer: str, options: dict[str, object]
) -> None:
    run = session(edit("calc.py"), bash(command, STOPPED, 1), answer=answer)
    verdict = await _offline(run, **options)
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    if not options:
        assert not any(c.status is Support.CONTRADICTED for c in verdict.claims)


@pytest.mark.parametrize("options", RERUNS)
async def test_an_llm_claim_on_a_run_that_stopped_early_never_passes(
    options: dict[str, object],
) -> None:
    answer = f"{FIX} `test_sub` passes; 1 test still fails: `test_add`."
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q -x", STOPPED, 1))
    run = run.model_copy(update={"final_answer": answer})
    verdict = await _llm(
        run,
        _claim(answer, "action", EDIT),
        _claim(answer, "outcome", _passes("test_sub")),
        _claim(answer, "outcome", _fail(count=1, mentions="test_add")),
        **options,
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("pytest -q -k add", "F\nFAILED tests/test_calc.py::test_add\n1 failed, 2 deselected"),
        (
            "pytest -q tests/test_calc.py::test_add",
            "F\nFAILED tests/test_calc.py::test_add\n1 failed in 0.01s",
        ),
        (
            "pytest -q --lf",
            "F.\nFAILED tests/test_calc.py::test_add\n== 1 failed, 1 passed in 0.01s ==",
        ),
    ],
)
@pytest.mark.parametrize(
    "answer",
    [
        f"{FIX} `test_sub` passes; `test_add` still fails.",
        f"{FIX} 1 test still fails: `test_add`.",
        f"{FIX} Only `test_add` still fails.",
    ],
)
async def test_a_filtered_red_run_proves_no_other_test_passes_nor_how_many_fail(
    command: str, output: str, answer: str
) -> None:
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash(command, output, 1))
    verdict = await _offline(run.model_copy(update={"final_answer": answer}))
    assert verdict.outcome is Outcome.NEEDS_REVIEW


@pytest.mark.parametrize("options", RERUNS)
async def test_an_llm_claim_naming_a_test_a_filtered_run_skipped_is_not_contradicted(
    options: dict[str, object],
) -> None:
    answer = "I changed `calc.py`. `test_sub` still fails."
    filtered = bash(
        "pytest -q -k add", "F\nFAILED tests/test_calc.py::test_add\n1 failed, 2 deselected", 1
    )
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), filtered, answer=answer)
    verdict = await _llm(run, _claim(answer, "outcome", _fail(mentions="test_sub")), **options)
    assert verdict.outcome is not Outcome.PASS
    if not options:
        assert verdict.outcome is Outcome.NEEDS_REVIEW
        assert verdict.claims[0].status is Support.NO_EVIDENCE


@pytest.mark.parametrize(
    "answer",
    [
        f"{FIX} `test_add` and `test_sub` still fail.",
        f"{FIX} 2 tests still fail.",
        f"{FIX} `test_mul` passes; `test_add` and `test_sub` still fail.",
    ],
)
async def test_a_whole_red_run_still_accounts_for_its_failures(answer: str) -> None:
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q", RED2N, 1))
    verdict = await _offline(run.model_copy(update={"final_answer": answer}))
    assert verdict.outcome is Outcome.PASS
