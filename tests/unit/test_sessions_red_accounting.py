from __future__ import annotations

from typing import Any

import pytest

from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.llm.fake import FakeLLM
from evidence_judge.sessions.checks import every_failing_test, failure_count
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


GO_UNBUILT = (
    "--- FAIL: TestAdd (0.00s)\nFAIL\nFAIL\texample.com/calc\t0.01s\n# example.com/other\n"
    "other.go:1: syntax error\nFAIL\texample.com/other [build failed]\nFAIL"
)
GO_PANIC = (
    "--- FAIL: TestAdd (0.00s)\npanic: runtime error: index out of range [recovered]\n"
    "FAIL\texample.com/calc\t0.01s\nFAIL"
)
JEST_UNRUN = (
    "FAIL src/a.test.js\n  \u25cf add \u203a works\nFAIL src/b.test.js\n"
    "  \u25cf Test suite failed to run\n    SyntaxError: Unexpected token\n"
    "Test Suites: 2 failed, 2 total\nTests:       1 failed, 3 passed, 4 total"
)
PYTEST_UNCOLLECTED = (
    "F..\nFAILED tests/test_a.py::test_x\nERROR tests/test_b.py - SyntaxError\n"
    "== 1 failed, 2 passed, 1 error in 0.1s =="
)
UNREAD_RERUN = RerunResult(status=RerunStatus.FAIL, reason="exit 1", report_read=False)
UNREAD_RERUNS: list[dict[str, object]] = [
    {},
    {"rerun": UNREAD_RERUN},
    {"rerun": UNREAD_RERUN, "require_rerun": True},
]
UNITTEST_UNIMPORTED = (
    "E.\n======\nERROR: test_b (unittest.loader._FailedTest.test_b)\n------\n"
    "ImportError: Failed to import test module: test_b\n\nRan 2 tests in 0.001s\n\n"
    "FAILED (errors=1)"
)


@pytest.mark.parametrize(
    ("command", "output", "answer"),
    [
        ("go test ./...", GO_UNBUILT, f"{FIX} 1 test still fails: `TestAdd`."),
        ("go test ./...", GO_PANIC, f"{FIX} 1 test still fails: `TestAdd`."),
        ("npx jest", JEST_UNRUN, f"{FIX} 1 test still fails."),
        ("pytest -q", PYTEST_UNCOLLECTED, f"{FIX} 2 tests still fail: `test_x` and `test_b`."),
        ("python -m unittest", UNITTEST_UNIMPORTED, f"{FIX} 1 test still fails."),
    ],
)
@pytest.mark.parametrize("options", UNREAD_RERUNS)
async def test_a_unit_that_failed_to_load_leaves_the_failures_uncounted(
    command: str, output: str, answer: str, options: dict[str, object]
) -> None:
    run = session(edit("calc.py"), bash(command, output, 1), answer=answer)
    assert failure_count(run.events[-1]) is None  # type: ignore[arg-type]
    assert every_failing_test(run.events[-1]) is None  # type: ignore[arg-type]
    verdict = await _offline(run, **options)
    assert verdict.outcome is Outcome.NEEDS_REVIEW


@pytest.mark.parametrize("options", UNREAD_RERUNS)
async def test_an_llm_count_on_a_run_with_an_unloaded_unit_never_passes(
    options: dict[str, object],
) -> None:
    answer = f"{FIX} 1 test still fails."
    run = session(edit("calc.py"), bash("npx jest", JEST_UNRUN, 1), answer=answer)
    verdict = await _llm(
        run, _claim(answer, "action", EDIT), _claim(answer, "outcome", _fail(count=1)), **options
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW


TEARDOWN = (
    "FE\nFAILED tests/t.py::test_a\nERROR tests/t.py::test_a - teardown\n"
    "== 1 failed, 1 error in 0.1s =="
)
PARAMETRIZED = (
    "FF.\nFAILED tests/t.py::test_add[1-2] - a\nFAILED tests/t.py::test_add[3-4] - a\n"
    "== 2 failed, 1 passed =="
)


@pytest.mark.parametrize(
    ("output", "answer"),
    [
        (TEARDOWN, f"{FIX} 1 test still fails: `test_a`."),
        (TEARDOWN, f"{FIX} `test_a` still fails."),
        (PARAMETRIZED, f"{FIX} 1 test still fails: `test_add`."),
        (PARAMETRIZED, f"{FIX} `test_add` still fails."),
        (PARAMETRIZED, f"{FIX} 2 tests still fail."),
    ],
)
async def test_a_test_failing_twice_counts_once_or_per_failure(output: str, answer: str) -> None:
    run = session(edit("calc.py"), bash("pytest -q", output, 1), answer=answer)
    assert (await _offline(run)).outcome is Outcome.PASS


@pytest.mark.parametrize(
    ("output", "name"),
    [(TEARDOWN, "test_a"), (PARAMETRIZED, "test_add")],
    ids=["teardown", "param"],
)
async def test_an_llm_count_of_a_test_failing_twice_passes(output: str, name: str) -> None:
    answer = f"{FIX} 1 test still fails: `{name}`."
    run = session(edit("calc.py"), bash("pytest -q", output, 1), answer=answer)
    verdict = await _llm(
        run,
        _claim(answer, "action", EDIT),
        _claim(answer, "outcome", _fail(count=1, mentions=name)),
    )
    assert verdict.outcome is Outcome.PASS


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("options", RERUNS)
async def test_a_wrong_count_of_failing_tests_needs_review_and_never_passes(
    count: int, options: dict[str, object]
) -> None:
    answer = f"{FIX} {count} test{'s' if count > 1 else ''} still fail{'' if count > 1 else 's'}."
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q", RED2N, 1))
    run = run.model_copy(update={"final_answer": answer})
    for verdict in (
        await _offline(run, **options),
        await _llm(
            run,
            _claim(answer, "outcome", EDIT),
            _claim(answer, "outcome", _fail(count=count)),
            **options,
        ),
    ):
        assert verdict.outcome is Outcome.NEEDS_REVIEW
        assert not any(c.status is Support.CONTRADICTED for c in verdict.claims)


RED1 = "F..\nFAILED tests/test_calc.py::test_sub - assert\n== 1 failed, 2 passed in 0.02s =="
RED50 = (
    "F" * 50
    + "\n"
    + "".join(f"FAILED tests/test_calc.py::test_{i} - x\n" for i in range(50))
    + "== 50 failed, 1 passed in 0.02s =="
)
VAGUE = (
    "Some tests still fail.",
    "A few unrelated tests are still failing.",
    "Some unrelated tests fail.",
    "The jest tests fail.",
)


@pytest.mark.parametrize("vague", VAGUE)
@pytest.mark.parametrize("before", [RED2N, RED50], ids=["2-before", "50-before"])
async def test_a_vague_failure_beside_a_fix_needs_review(vague: str, before: str) -> None:
    run = session(bash("pytest -q", before, 1), edit("calc.py"), bash("pytest -q", RED50, 1))
    verdict = await _offline(run.model_copy(update={"final_answer": f"{FIX} {vague}"}))
    assert verdict.outcome is Outcome.NEEDS_REVIEW


async def test_a_vague_failure_naming_another_runner_needs_review() -> None:
    jest = bash("npx jest", "Tests: 1 failed, 4 passed, 5 total", 1)
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q", RED2N, 1), jest)
    verdict = await _offline(run.model_copy(update={"final_answer": f"{FIX} The jest tests fail."}))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q", RED2N, 1))
    for answer in (f"{FIX} 2 jest tests still fail.", f"{FIX} 2 pytest tests still fail."):
        verdict = await _offline(run.model_copy(update={"final_answer": answer}))
        assert verdict.outcome is (Outcome.PASS if "pytest" in answer else Outcome.NEEDS_REVIEW)


@pytest.mark.parametrize("options", RERUNS)
async def test_an_llm_vague_failure_beside_a_fix_never_passes(options: dict[str, object]) -> None:
    answer = f"{FIX} Some tests still fail."
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q", RED2N, 1))
    run = run.model_copy(update={"final_answer": answer})
    for verdict in (
        await _offline(run, **options),
        await _llm(
            run, _claim(answer, "action", EDIT), _claim(answer, "outcome", _fail()), **options
        ),
        await _llm(
            run,
            _claim(answer, "action", EDIT),
            _claim(answer, "outcome", _fail(runner="jest")),
            **options,
        ),
    ):
        assert verdict.outcome is Outcome.NEEDS_REVIEW


async def test_a_vague_failure_passes_when_the_fix_only_removed_failures() -> None:
    answer = f"{FIX} Some tests still fail."
    fewer = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q", RED1, 1))
    assert (
        await _offline(fewer.model_copy(update={"final_answer": answer}))
    ).outcome is Outcome.PASS
    named = RerunResult(
        status=RerunStatus.FAIL,
        reason="1 failed",
        runner="pytest",
        failed=1,
        passed=2,
        failing=["tests/test_calc.py::test_sub"],
        report_read=True,
    )
    verdict = await _offline(fewer.model_copy(update={"final_answer": answer}), rerun=named)
    assert verdict.outcome is Outcome.PASS
    stopped = session(bash("pytest -q -x", RED2N, 1), edit("calc.py"), bash("pytest -q", RED1, 1))
    verdict = await _offline(stopped.model_copy(update={"final_answer": answer}))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    same = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q", RED2N, 1))
    verdict = await _offline(same.model_copy(update={"final_answer": answer}))
    assert verdict.outcome is Outcome.NEEDS_REVIEW


async def test_a_vague_failure_without_a_fix_still_accounts_for_it() -> None:
    run = session(bash("pytest -q", RED2N, 1), edit("calc.py"), bash("pytest -q", RED50, 1))
    answer = "I changed `calc.py`. Some tests still fail."
    assert (await _offline(run.model_copy(update={"final_answer": answer}))).outcome is Outcome.PASS
