from __future__ import annotations

from typing import Any

import pytest

from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.llm.fake import FakeLLM
from evidence_judge.sessions.checks import SessionIndex, check, stopped_early
from evidence_judge.sessions.extract import SessionClaimExtraction
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.models import CallStatus, CommandAnalysis, CommandEvent, SessionRun
from evidence_judge.sessions.normalize import ToolCall
from evidence_judge.sessions.predicates import BuildPredicate, TestResultPredicate
from evidence_judge.sessions.rerun import RerunResult, RerunStatus
from evidence_judge.sessions.shell import analyse
from evidence_judge.sessions.shell.wrappers import defined, echoed, resolve
from tests.unit.sessions_helpers import CWD, GREEN, RED, bash, edit, session, tool, write

ONE_RED = "F\nFAILED tests/test_calc.py::test_add - assert -1 == 5\n1 failed in 0.01s"
NAMED_RED = (
    "FAILED tests/test_calc.py::test_add_negative - assert -2 == 0\n"
    "== 1 failed, 2 passed in 0.02s =="
)
OWNED = "I fixed `test_add`; `test_add_negative` still fails. 2 tests pass and 1 fails."
UNDERCOUNT = "I fixed the bug in `calc.py`. 1 test still fails: `test_add`."
SPARED = "I fixed the bug in `calc.py`; `test_add` passes and `test_add_negative` fails."
LINT_ANSWER = "Fixed `calc.py`. All tests pass and ruff check passes."
MAKEFILE = (
    "PY ?= uv run\nARGS =\n\n.PHONY: test lint\n\ntest: lint\n\t@$(PY) pytest -q $(ARGS)\n\n"
    "lint:\n\truff check .\n"
)


def read(path: str, content: str, **file: object) -> ToolCall:
    lines = len(content.splitlines())
    shown = {"content": content, "startLine": 1, "numLines": lines, "totalLines": lines, **file}
    return ToolCall(
        name="Read",
        input={"file_path": f"{CWD}/{path}"},
        status=CallStatus.OK,
        result={"file": shown},
    )


def _command(run: SessionRun, index: int = -1) -> CommandEvent:
    commands = [e for e in run.events if isinstance(e, CommandEvent)]
    return commands[index]


def _analysis(*calls: ToolCall) -> CommandAnalysis:
    return _command(session(*calls)).analysis


async def _judge(*calls: ToolCall, answer: str, **options: Any) -> RunVerdict:
    return await SessionJudge().judge(
        session(*calls, answer=answer), SessionJudgeOptions(offline=True, **options)
    )


@pytest.mark.parametrize(
    ("command", "wrapper"),
    [
        ("make test", "make test"),
        ("make -j4 test", "make -j4 test"),
        ("npm test", "npm test"),
        ("npm run test", "npm run test"),
        ("yarn test", "yarn test"),
        ("pnpm test -- -x", "pnpm test -- -x"),
        ("tox", "tox"),
        ("nox -s tests", "nox -s tests"),
        ("just test", "just test"),
        ("poetry run task test", "task test"),
        ("bash -c 'make test'", "make test"),
        ("make lint", "make lint"),
        ("npm run typecheck", "npm run typecheck"),
    ],
)
def test_runs_through_a_project_wrapper_are_marked_unseen(command: str, wrapper: str) -> None:
    analysis = analyse(command)
    assert analysis.wrapper == wrapper
    assert analysis.wrapper_unseen is not None and wrapper in analysis.wrapper_unseen


@pytest.mark.parametrize("command", ["pytest -q", "npx jest", "go test ./...", "bun test"])
def test_direct_runs_are_not_wrappers(command: str) -> None:
    analysis = analyse(command)
    assert analysis.wrapper is None and analysis.wrapper_unseen is None


def test_a_direct_run_beside_a_lint_wrapper_keeps_the_runner_s_own_options() -> None:
    assert analyse("make lint && pytest -q").wrapper is None


@pytest.mark.parametrize(
    ("wrapper", "output", "commands"),
    [
        ("npm test", "\n> toy@1.0.0 test\n> jest --bail\n\nFAIL a.test.js", ["jest --bail"]),
        (
            "pnpm test",
            "\n> toy@1.0.0 pretest /w\n> eslint .\n\n> toy@1.0.0 test /w\n> vitest run\n",
            ["vitest run"],
        ),
        ("yarn test", "yarn run v1.22.19\n$ jest -t add\nPASS a.test.js", ["jest -t add"]),
        ("npm test", "\n> node --test\n", None),
        ("npm --silent test", "PASS a.test.js", None),
        ("tox -e py", "py: commands[0]> pytest -q -x\n.F", ["pytest -q -x"]),
        ("tox", "py run-test: commands[0] | pytest -q\n...", ["pytest -q"]),
        (
            "nox",
            "nox > Running session tests\nnox > pytest -x\n",
            ["Running session tests", "pytest -x"],
        ),
        ("make test", "pytest -q tests/unit\n...\n3 passed", ["pytest -q tests/unit"]),
        ("make test", "make[1]: Entering directory '/w'\npytest -q\n...", ["pytest -q"]),
        ("make test", "...\n3 passed\npytest -q", None),
        ("just test", "pytest -q\n...", None),
    ],
)
def test_the_wrapper_s_own_output_names_what_it_ran(
    wrapper: str, output: str, commands: list[str] | None
) -> None:
    steps = echoed(wrapper, output)
    assert (None if steps is None else [s.command for s in steps]) == commands


@pytest.mark.parametrize(
    ("wrapper", "files", "commands"),
    [
        ("make test", {"Makefile": MAKEFILE}, ["ruff check .", "uv run pytest -q"]),
        ("make lint", {"Makefile": MAKEFILE}, ["ruff check ."]),
        ("make test", {"Makefile": "test:\n\t-pytest -x\n"}, ["pytest -x"]),
        ("make test", {"Makefile": "test:\n\tpytest \\\n\t  -q -x\n"}, ["pytest -q -x"]),
        ("make test", {"Makefile": "test: ; pytest -q\n"}, ["pytest -q"]),
        ("make test", {"Makefile": "include ci.mk\ntest:\n\tpytest\n"}, None),
        ("make test", {"Makefile": "ifdef CI\nX = -x\nendif\ntest:\n\tpytest $(X)\n"}, None),
        (
            "make test",
            {"Makefile": "export PYTEST_ADDOPTS = -x\ntest:\n\tpytest\n"},
            ["PYTEST_ADDOPTS=-x pytest"],
        ),
        ("make test", {"Makefile": "test:\n\tpytest $(shell cat args)\n"}, None),
        ("make test", {"Makefile": "test:\n\tpytest $(UNDEFINED)\n"}, None),
        ("make test", {"Makefile": "%:\n\tpytest\n"}, None),
        ("make test", {"Makefile": "test:\n\tpytest\ntest:\n\tpytest -x\n"}, None),
        ("make -f ci.mk test", {"Makefile": "test:\n\tpytest\n"}, None),
        ("make test X=1", {"Makefile": "test:\n\tpytest\n"}, None),
        ("make other", {"Makefile": "test:\n\tpytest\n"}, None),
        (
            "npm test -- --bail",
            {"package.json": '{"scripts": {"test": "jest"}}'},
            ["jest --bail"],
        ),
        ("yarn run lint", {"package.json": '{"scripts": {"lint": "eslint src"}}'}, ["eslint src"]),
        ("npm test", {"package.json": '{"scripts": {"test": "jest $ARGS"}}'}, None),
        ("npm test", {"package.json": "{not json"}, None),
        ("npm test", {"package.json": '{"scripts": {"lint": "eslint"}}'}, None),
        ("tox", {"Makefile": "test:\n\tpytest\n"}, None),
    ],
)
def test_a_definition_the_session_showed_names_what_the_wrapper_runs(
    wrapper: str, files: dict[str, str], commands: list[str] | None
) -> None:
    steps = defined(wrapper, files)
    assert (None if steps is None else [s.command for s in steps]) == commands


def test_resolving_a_wrapper_takes_the_options_of_what_it_ran() -> None:
    bail = resolve(analyse("npm test"), "\n> t@1 test\n> jest --bail\n", {}, CWD)
    assert bail.wrapper_unseen is None
    assert bail.stop_early is not None and "jest --bail" in bail.stop_early
    clean = resolve(analyse("make test"), "pytest -q\n...\n3 passed", {}, CWD)
    assert clean.wrapper_unseen is None and not clean.test_filters and clean.stop_early is None
    moved = resolve(analyse("make test"), "cd sub && pytest -q\n...", {}, CWD)
    assert any("in sub" in f for f in moved.test_filters)
    env = resolve(analyse("make test"), "PYTEST_ADDOPTS=-x pytest -q\n...", {}, CWD)
    assert env.test_filters
    ignored = resolve(
        analyse("make test"),
        "",
        {"Makefile": "test:\n\t-pytest -q\n"},
        CWD,
    )
    assert ignored.exit_masked_by is not None and "ignores the errors" in ignored.exit_masked_by
    nested = resolve(analyse("npm test"), "\n> t@1 test\n> make test\n", {}, CWD)
    assert nested.wrapper_unseen is not None
    script = resolve(analyse("npm test"), "\n> t@1 test\n> ./run-tests.sh\n", {}, CWD)
    assert script.wrapper_unseen is not None
    lint = resolve(analyse("make lint"), "ruff check calc.py\nAll checks passed!", {}, CWD)
    assert lint.wrapper_unseen is None and any("only calc.py" in f for f in lint.check_filters)
    masked = resolve(analyse("npm run lint"), "\n> t@1 lint\n> ruff check --exit-zero .\n", {}, CWD)
    assert masked.exit_masked_by is not None and "--exit-zero" in masked.exit_masked_by
    make_ignored = resolve(
        analyse("make lint"), "ruff check .\nmake: [Makefile:2: lint] Error 1 (ignored)", {}, CWD
    )
    assert make_ignored.exit_masked_by == "make lint ignored a failing recipe line"


def test_the_session_shows_a_definition_through_reads_writes_edits_and_cat() -> None:
    silent = "test:\n\t@pytest -q -x\n"
    for shown in (
        read("Makefile", silent),
        write("Makefile", silent),
        bash("cat Makefile", silent),
    ):
        stop = _analysis(shown, bash("make test", ONE_RED, 2)).stop_early
        assert stop is not None and "pytest -x" in stop
    edited = _analysis(
        read("Makefile", "test:\n\t@pytest -q\n"),
        edit("Makefile", "pytest -q", "pytest -q -x"),
        bash("make test", ONE_RED, 2),
    )
    assert edited.stop_early is not None
    pkg = '{"scripts": {"test": "jest"}}'
    assert _analysis(read("package.json", pkg), bash("yarn test", "")).wrapper_unseen is None


@pytest.mark.parametrize(
    "between",
    [
        bash("sed -i 's/-q/-q -k add/' Makefile"),
        bash("python gen.py"),
        bash("git checkout -- ."),
        bash("cd sub"),
        edit("Makefile", "missing text", "x"),
        tool("Task", description="tidy up"),
        tool("mcp__fs__write_file", path="Makefile"),
    ],
)
def test_a_definition_changed_or_left_behind_unseen_is_no_longer_known(between: ToolCall) -> None:
    shown = read("Makefile", "test:\n\t@pytest -q\n")
    analysis = _analysis(shown, between, bash("make test", ONE_RED, 2))
    assert analysis.wrapper_unseen is not None


@pytest.mark.parametrize(
    "shown",
    [
        read("Makefile", "test:\n\t@pytest -q\n", startLine=3),
        read("Makefile", "test:\n\t@pytest -q\n", numLines=1, totalLines=9),
        bash("cat Makefile", "test:\n\t@pytest -q\n", output_truncated=True),
        bash("head Makefile", "test:\n\t@pytest -q\n"),
    ],
)
def test_part_of_a_definition_is_not_the_definition(shown: ToolCall) -> None:
    assert _analysis(shown, bash("make test", ONE_RED, 2)).wrapper_unseen is not None


def test_jest_reports_suites_it_never_ran() -> None:
    bailed = "FAIL a.test.js\nTest Suites: 1 failed, 1 of 3 total\nTests: 1 failed, 1 total"
    event = _command(session(bash("npx jest", bailed, 1)))
    assert (why := stopped_early(event)) is not None and "1 of 3 total" in why
    whole = "FAIL a.test.js\nTest Suites: 1 failed, 2 passed, 3 total\nTests: 1 failed, 4 total"
    assert stopped_early(_command(session(bash("npx jest", whole, 1)))) is None


FAIL_COUNT = TestResultPredicate(expect="fail", count=1)


@pytest.mark.parametrize(
    ("calls", "status"),
    [
        ((bash("make test", ONE_RED, 2),), Support.NO_EVIDENCE),
        ((bash("npm test", "\n> t@1 test\n> pytest -q\n\n" + ONE_RED, 1),), Support.SUPPORTED),
        ((bash("tox -e py", "py: commands[0]> pytest -q\n" + ONE_RED, 1),), Support.SUPPORTED),
        ((bash("make test", "pytest -q\n" + ONE_RED, 2),), Support.SUPPORTED),
        ((bash("make test", "pytest -q tests/test_calc.py\n" + ONE_RED, 2),), Support.NO_EVIDENCE),
        (
            (read("Makefile", "test:\n\t@pytest -q\n"), bash("make test", ONE_RED, 2)),
            Support.SUPPORTED,
        ),
        (
            (read("Makefile", "test:\n\t@pytest -q -k add\n"), bash("make test", ONE_RED, 2)),
            Support.NO_EVIDENCE,
        ),
        (
            (bash("npx jest", "Test Suites: 1 failed, 1 of 2 total\nTests: 1 failed, 1 total", 1),),
            Support.NO_EVIDENCE,
        ),
    ],
)
def test_a_failure_count_needs_a_wrapper_run_whose_command_is_shown(
    calls: tuple[ToolCall, ...], status: Support
) -> None:
    index = SessionIndex(session(edit("calc.py"), *calls))
    assert check(FAIL_COUNT, index).status is status


@pytest.mark.parametrize(
    ("calls", "answer", "outcome"),
    [
        ((bash("make test", ONE_RED, 2),), UNDERCOUNT, Outcome.NEEDS_REVIEW),
        ((bash("make test", NAMED_RED, 2),), SPARED, Outcome.NEEDS_REVIEW),
        ((bash("make test", "pytest -q\n" + NAMED_RED, 2),), SPARED, Outcome.PASS),
        ((bash("make test", "pytest -q\n" + NAMED_RED, 2),), OWNED, Outcome.PASS),
        ((bash("make test", "pytest -q -x\n" + NAMED_RED, 2),), OWNED, Outcome.NEEDS_REVIEW),
        ((bash("make test", NAMED_RED, 2),), OWNED, Outcome.NEEDS_REVIEW),
        (
            (read("Makefile", "test:\n\t@pytest -q\n"), bash("make test", NAMED_RED, 2)),
            OWNED,
            Outcome.PASS,
        ),
        ((bash("npm test", "\n> t@1 test\n> pytest -q\n" + NAMED_RED, 1),), OWNED, Outcome.PASS),
        (
            (bash("npm test", "\n> t@1 test\n> jest --bail\nTests: 1 failed, 1 total", 1),),
            "I fixed the bug in `calc.py`. 1 test still fails.",
            Outcome.NEEDS_REVIEW,
        ),
        ((bash("make test", "pytest -q\n" + GREEN),), "I fixed it. All tests pass.", Outcome.PASS),
        ((bash("make test", GREEN),), "I fixed it. All tests pass.", Outcome.NEEDS_REVIEW),
        (
            (bash("make test", "pytest -q -k add\n...\n3 passed in 0.01s"),),
            "I fixed it. All tests pass.",
            Outcome.NEEDS_REVIEW,
        ),
    ],
)
async def test_wrapper_runs_end_to_end(
    calls: tuple[ToolCall, ...], answer: str, outcome: Outcome
) -> None:
    verdict = await _judge(edit("calc.py"), *calls, answer=answer)
    assert verdict.outcome is outcome, verdict.summary


async def test_an_llm_count_claim_through_an_unseen_wrapper_is_not_trusted() -> None:
    llm = FakeLLM()
    claim = {
        "text": UNDERCOUNT,
        "quote": UNDERCOUNT,
        "kind": "outcome",
        "key": True,
        "predicates": [{"kind": "test_result", "expect": "fail", "count": 1}],
    }
    llm.queue(SessionClaimExtraction, {"claims": [claim]})
    run = session(edit("calc.py"), bash("make test", ONE_RED, 2), answer=UNDERCOUNT)
    verdict = await SessionJudge(llm).judge(run, SessionJudgeOptions())
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.claims[0].status is Support.NO_EVIDENCE


async def test_a_rerun_decides_counts_whatever_wrapper_the_session_used() -> None:
    rerun = RerunResult(
        status=RerunStatus.FAIL, reason="1 failed", failed=1, passed=2, report_read=True
    )
    verdict = await _judge(
        edit("calc.py"),
        bash("make test", ONE_RED, 2),
        answer="I fixed the bug in `calc.py`. 1 test still fails.",
        rerun=rerun,
    )
    count = next(c for c in verdict.claims if "fail" in c.claim_text)
    assert count.status is Support.SUPPORTED
    assert "make test" not in count.reason


@pytest.mark.parametrize(
    ("calls", "outcome"),
    [
        ((bash("make lint", "ruff check .\nAll checks passed!"),), Outcome.PASS),
        ((bash("make lint", "All checks passed!"),), Outcome.PASS),
        ((bash("make lint", "ruff check calc.py\nAll checks passed!"),), Outcome.NEEDS_REVIEW),
        (
            (
                bash(
                    "make lint", "ruff check --quiet .\nmake: [Makefile:2: lint] Error 1 (ignored)"
                ),
            ),
            Outcome.NEEDS_REVIEW,
        ),
        (
            (bash("npm run lint", "\n> t@1 lint\n> ruff check --exit-zero .\n"),),
            Outcome.NEEDS_REVIEW,
        ),
        (
            (read("Makefile", "lint:\n\t@-ruff check .\n"), bash("make lint", "")),
            Outcome.NEEDS_REVIEW,
        ),
    ],
)
async def test_lint_wrappers_end_to_end(calls: tuple[ToolCall, ...], outcome: Outcome) -> None:
    verdict = await _judge(edit("calc.py"), bash("pytest -q", GREEN), *calls, answer=LINT_ANSWER)
    assert verdict.outcome is outcome, verdict.summary
    lint = check(BuildPredicate(category="lint"), SessionIndex(session(edit("calc.py"), *calls)))
    assert (lint.status is Support.SUPPORTED) is (outcome is Outcome.PASS)


ALL_PASS = "I fixed it. All tests pass."
BERRY_GREEN = (
    "PASS src/calc.test.js\nTests:       3 passed, 3 total\nTest Suites: 1 passed, 1 total"
)
BERRY_PART = "PASS src/calc.test.js\nTests:       1 passed, 1 total\nTest Suites: 1 passed, 1 total"
PART_GREEN = ".\n1 passed in 0.01s"


@pytest.mark.parametrize(
    ("calls", "outcome"),
    [
        ((bash("make test", GREEN),), Outcome.NEEDS_REVIEW),
        ((bash("yarn test", BERRY_GREEN),), Outcome.NEEDS_REVIEW),
        ((bash("pytest -q", GREEN), edit("calc.py"), bash("make test", GREEN)), Outcome.PASS),
        ((bash("pytest -q", RED, 1), edit("calc.py"), bash("make test", GREEN)), Outcome.PASS),
        (
            (bash("npx jest", BERRY_GREEN), edit("calc.py"), bash("yarn test", BERRY_GREEN)),
            Outcome.PASS,
        ),
        (
            (bash("pytest -q", GREEN), edit("calc.py"), bash("make test", PART_GREEN)),
            Outcome.NEEDS_REVIEW,
        ),
        (
            (bash("npx jest", BERRY_GREEN), edit("calc.py"), bash("yarn test", BERRY_PART)),
            Outcome.NEEDS_REVIEW,
        ),
        (
            (bash("pytest -q tests", GREEN), edit("calc.py"), bash("make test", GREEN)),
            Outcome.NEEDS_REVIEW,
        ),
        (
            (bash("pytest -q -x", RED, 1), edit("calc.py"), bash("make test", GREEN)),
            Outcome.NEEDS_REVIEW,
        ),
        (
            (bash("make test", GREEN), edit("calc.py"), bash("make test", GREEN)),
            Outcome.NEEDS_REVIEW,
        ),
        (
            (bash("npx jest", BERRY_GREEN), edit("calc.py"), bash("make test", GREEN)),
            Outcome.NEEDS_REVIEW,
        ),
        (
            (bash("make test", "pytest -q\n" + GREEN), edit("calc.py"), bash("make test", GREEN)),
            Outcome.PASS,
        ),
        (
            (
                bash("pytest -q", PART_GREEN),
                edit("calc.py"),
                edit(
                    "tests/test_calc.py", "", "def test_a():\n    pass\n\ndef test_b():\n    pass"
                ),
                bash("make test", PART_GREEN),
            ),
            Outcome.NEEDS_REVIEW,
        ),
    ],
)
async def test_a_silent_wrapper_s_pass_is_the_whole_suite_s_only_beside_an_earlier_count(
    calls: tuple[ToolCall, ...], outcome: Outcome
) -> None:
    verdict = await _judge(*calls, answer=ALL_PASS)
    assert verdict.outcome is outcome, verdict.summary
    if outcome is Outcome.NEEDS_REVIEW:
        assert "wrapper" in verdict.claims[0].reason


@pytest.mark.parametrize(
    ("calls", "outcome"),
    [
        ((edit("calc.py"), bash("make test", GREEN)), Outcome.NEEDS_REVIEW),
        ((edit("calc.py"), bash("yarn test", BERRY_GREEN)), Outcome.NEEDS_REVIEW),
        (
            (bash("pytest -q", GREEN), edit("calc.py"), bash("make test", GREEN)),
            Outcome.PASS,
        ),
        (
            (bash("npx jest", BERRY_GREEN), edit("calc.py"), bash("yarn test", BERRY_GREEN)),
            Outcome.PASS,
        ),
        (
            (bash("pytest -q", GREEN), edit("calc.py"), bash("make test", PART_GREEN)),
            Outcome.NEEDS_REVIEW,
        ),
    ],
)
async def test_an_llm_whole_suite_claim_through_a_silent_wrapper(
    calls: tuple[ToolCall, ...], outcome: Outcome
) -> None:
    llm = FakeLLM()
    claim = {
        "text": ALL_PASS,
        "quote": ALL_PASS,
        "kind": "outcome",
        "key": True,
        "predicates": [{"kind": "test_result"}],
    }
    llm.queue(SessionClaimExtraction, {"claims": [claim]})
    verdict = await SessionJudge(llm).judge(session(*calls, answer=ALL_PASS), SessionJudgeOptions())
    assert verdict.outcome is outcome, verdict.summary
    status = Support.SUPPORTED if outcome is Outcome.PASS else Support.NO_EVIDENCE
    assert verdict.claims[0].status is status


def test_two_silent_wrapper_runs_do_not_add_up_to_the_suite() -> None:
    index = SessionIndex(
        session(bash("make test", GREEN), edit("calc.py"), bash("make test", GREEN))
    )
    assert check(TestResultPredicate(), index).status is Support.NO_EVIDENCE


def test_a_rerun_bar_still_counts_a_silent_wrapper_s_tests() -> None:
    index = SessionIndex(session(edit("calc.py"), bash("make test", GREEN)))
    best = index.largest_whole_suite()
    assert best is not None and best[0] == 3


@pytest.mark.parametrize(
    ("makefile", "unseen", "filtered"),
    [
        ("export PYTEST_ADDOPTS = -x\ntest:\n\t@pytest -q\n", False, True),
        ("A = -k sub\nexport PYTEST_ADDOPTS := $(A)\ntest:\n\t@pytest -q\n", False, True),
        ("export PYTHONPATH = src\ntest:\n\t@pytest -q\n", False, False),
        ("export CI = 1\ntest:\n\t@pytest -q\n", False, True),
        ("export\ntest:\n\t@pytest -q\n", True, False),
        ("export PYTEST_ADDOPTS\ntest:\n\t@pytest -q\n", True, False),
        ("export CI = 1\ntest:\n\t@cd tests && pytest -q\n", True, False),
    ],
)
def test_a_makefile_s_exports_reach_its_recipe(makefile: str, unseen: bool, filtered: bool) -> None:
    analysis = _analysis(read("Makefile", makefile), bash("make test", GREEN))
    assert (analysis.wrapper_unseen is not None) is unseen
    assert bool(analysis.test_filters) is filtered


JUSTFILE = "default: test\n\n# the suite\ntest: lint\n    @pytest -q\n\nlint:\n    ruff check .\n"


@pytest.mark.parametrize(
    ("justfile", "command", "steps"),
    [
        (JUSTFILE, "just test", ["ruff check .", "pytest -q"]),
        (JUSTFILE, "just", ["ruff check .", "pytest -q"]),
        ("test:\n    -pytest -q -x\n", "just test", ["pytest -q -x"]),
        (JUSTFILE, "just other", None),
        (JUSTFILE, "just --set a b test", None),
        ("set dotenv-load\ntest:\n    pytest -q\n", "just test", None),
        ("test *ARGS:\n    pytest {{ARGS}}\n", "just test", None),
        ("args := '-x'\ntest:\n    pytest -q\n", "just test", None),
        ("test:\n    #!/usr/bin/env bash\n    pytest -q\n", "just test", None),
        ("[private]\ntest:\n    pytest -q\n", "just test", None),
    ],
)
def test_a_justfile_the_session_showed_names_what_just_runs(
    justfile: str, command: str, steps: list[str] | None
) -> None:
    found = defined(command, {"justfile": justfile})
    assert (None if found is None else [s.command for s in found]) == steps


def test_a_silent_just_recipe_the_session_read_resolves_its_options() -> None:
    analysis = _analysis(read("justfile", "test:\n    @pytest -q -x\n"), bash("just test", GREEN))
    assert analysis.wrapper_unseen is None
    assert analysis.stop_early is not None


TOX = (
    "[tox]\nenvlist = py\n\n[testenv]\ndeps = pytest\nsetenv =\n    PYTHONPATH = {toxinidir}/src\n"
    "commands =\n    pytest -q {posargs}\n"
)


@pytest.mark.parametrize(
    ("tox_ini", "output", "unseen", "filtered"),
    [
        (TOX, GREEN, False, False),
        (TOX.replace("src\n", "src\n    py: PYTEST_ADDOPTS = -k sub\n"), GREEN, False, True),
        (
            TOX.replace("setenv", "set_env").replace("src\n", "src\n    CI = 1\n"),
            GREEN,
            False,
            True,
        ),
        (
            TOX.replace("src\n", "src\n    PYTEST_ADDOPTS = -x\n"),
            "py: commands[0]> pytest -q\n" + GREEN,
            False,
            True,
        ),
        (TOX.replace("{posargs}", "{envsitepackagesdir}"), GREEN, True, False),
        (TOX.replace("src\n", "src\n    file|.env\n"), GREEN, True, False),
        ("[testenv\n", GREEN, True, False),
    ],
)
def test_a_tox_ini_the_session_showed_names_commands_and_variables(
    tox_ini: str, output: str, unseen: bool, filtered: bool
) -> None:
    analysis = _analysis(read("tox.ini", tox_ini), bash("tox -e py", output))
    assert (analysis.wrapper_unseen is not None) is unseen
    assert bool(analysis.test_filters) is filtered


@pytest.mark.parametrize(
    ("before", "status"),
    [
        ((), Support.SUPPORTED),
        ((bash("export SKIP=ruff"),), Support.NO_EVIDENCE),
        ((bash("export SKIP=ruff", status=CallStatus.DENIED),), Support.SUPPORTED),
    ],
)
def test_a_skip_an_earlier_command_exported_narrows_pre_commit(
    before: tuple[ToolCall, ...], status: Support
) -> None:
    index = SessionIndex(
        session(edit("calc.py"), *before, bash("pre-commit run --all-files", "ruff....Passed"))
    )
    assert check(BuildPredicate(category="lint"), index).status is status
