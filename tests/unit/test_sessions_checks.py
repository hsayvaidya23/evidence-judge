from __future__ import annotations

import pytest
from pydantic import ValidationError

from evidence_judge.core.verdict import Support
from evidence_judge.sessions.checks import SessionIndex, check, path_matches
from evidence_judge.sessions.models import CallStatus, SessionRun
from evidence_judge.sessions.normalize import ToolCall
from evidence_judge.sessions.predicates import (
    BuildPredicate,
    CommandPredicate,
    EditPredicate,
    SessionPredicate,
    TestResultPredicate,
)
from tests.unit.sessions_helpers import CWD, GREEN, RED, bash, edit, session, tool, write

ALL_PASS = TestResultPredicate()


def _check(
    predicate: SessionPredicate, run: SessionRun, **kwargs: list[str]
) -> tuple[Support, str]:
    result = check(predicate, SessionIndex(run, **kwargs))
    return result.status, result.reason


def test_fresh_whole_suite_pass_is_supported() -> None:
    status, reason = _check(ALL_PASS, session(edit("calc.py"), bash("pytest -q", GREEN)))
    assert status is Support.SUPPORTED
    assert "cmd#1 exited 0" in reason


def test_tests_before_the_last_edit_are_stale() -> None:
    status, reason = _check(ALL_PASS, session(bash("pytest -q", GREEN), edit("calc.py")))
    assert status is Support.NO_EVIDENCE
    assert "before the workspace changed" in reason and "edit#1" in reason


def test_no_test_run() -> None:
    assert _check(ALL_PASS, session(edit("calc.py")))[0] is Support.NO_EVIDENCE


def test_filtered_pass_cannot_prove_the_whole_suite() -> None:
    status, reason = _check(
        ALL_PASS,
        session(edit("calc.py"), bash("pytest -k add", "..\n2 passed, 1 deselected in 0.01s")),
    )
    assert status is Support.NO_EVIDENCE
    assert "subset" in reason


def test_deselected_summary_alone_marks_a_subset() -> None:
    run = session(edit("calc.py"), bash("pytest", "2 passed, 1 deselected in 0.01s"))
    assert _check(ALL_PASS, run)[0] is Support.NO_EVIDENCE


def test_filtered_failure_contradicts_the_whole_suite() -> None:
    run = session(edit("calc.py"), bash("pytest -k add", "== 1 failed, 1 deselected in 0.1s ==", 1))
    assert _check(ALL_PASS, run)[0] is Support.CONTRADICTED


def test_filtered_pass_proves_a_subset_claim() -> None:
    run = session(edit("calc.py"), bash("pytest -k add", "2 passed, 1 deselected in 0.01s"))
    assert _check(TestResultPredicate(whole_suite=False), run)[0] is Support.SUPPORTED


@pytest.mark.parametrize(
    ("command", "output", "exit_code", "fragment"),
    [
        ("pytest || true", RED, 0, "failed"),
        ("pytest || true", GREEN, 0, "not the runner's"),
        ("pytest; exit 0", GREEN, 0, "not the runner's"),
        ("pytest | grep -v FAIL", GREEN, 0, "piped into grep"),
        ("pytest -q; echo '3 passed in 0.01s'", GREEN, 0, "test-summary text"),
        ("pytest && python report.py", GREEN, 0, "before the workspace changed"),
        ("pytest -q", "collected 3 items\nKeyboardInterrupt", 0, "no test summary"),
        ("pytest -q", "\n== no tests ran in 0.01s ==", 0, "ran no tests"),
        ("pytest -q", GREEN, None, "not the runner's"),
    ],
)
def test_fake_success_tricks_never_pass(
    command: str, output: str, exit_code: int | None, fragment: str
) -> None:
    status, reason = _check(ALL_PASS, session(edit("calc.py"), bash(command, output, exit_code)))
    assert status is not Support.SUPPORTED
    assert fragment in reason


def test_echoed_summary_is_not_a_test_run() -> None:
    run = session(edit("calc.py"), bash('echo "3 passed in 0.01s"', "3 passed in 0.01s"))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE
    assert reason == "no test run is recorded"


def test_tail_keeps_the_summary_trustworthy() -> None:
    run = session(edit("calc.py"), bash("pytest -q 2>&1 | tail -n 3", GREEN))
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED
    failing = session(edit("calc.py"), bash("pytest -q 2>&1 | tail -n 3", RED))
    assert _check(ALL_PASS, failing)[0] is Support.CONTRADICTED


def test_truncated_output_cannot_prove_a_pass() -> None:
    run = session(edit("calc.py"), bash("pytest", GREEN, output_truncated=True))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE and "truncated" in reason


@pytest.mark.parametrize(
    ("status", "extra", "fragment"),
    [
        (CallStatus.NO_RESULT, {}, "no recorded result"),
        (CallStatus.INTERRUPTED, {}, "interrupted"),
        (CallStatus.OK, {"background": True}, "background"),
    ],
)
def test_runs_without_a_result(status: CallStatus, extra: dict[str, bool], fragment: str) -> None:
    run = session(edit("calc.py"), bash("pytest", GREEN, None, status=status, **extra))
    result = _check(ALL_PASS, run)
    assert result[0] is Support.NO_EVIDENCE and fragment in result[1]


def test_denied_runs_do_not_count() -> None:
    run = session(edit("calc.py"), bash("pytest", "", None, status=CallStatus.DENIED))
    assert _check(ALL_PASS, run) == (Support.NO_EVIDENCE, "no test run is recorded")


def test_exit_status_decides_without_a_summary() -> None:
    run = session(edit("calc.py"), bash("pytest", "ImportError: no module named calc", 2))
    assert _check(ALL_PASS, run) == (Support.CONTRADICTED, "cmd#1 exited with code 2")
    coverage = session(edit("calc.py"), bash("pytest --cov", GREEN + "\nFAIL Required coverage", 1))
    status, reason = _check(ALL_PASS, coverage)
    assert status is Support.NO_EVIDENCE and "although its summary shows" in reason


def test_flaky_results_are_not_resolved() -> None:
    run = session(edit("calc.py"), bash("pytest", RED, 1), bash("pytest", GREEN))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE and "flaky" in reason


def test_failure_after_a_pass_contradicts() -> None:
    run = session(edit("calc.py"), bash("pytest", GREEN), bash("pytest", RED, 1))
    assert _check(ALL_PASS, run)[0] is Support.CONTRADICTED


@pytest.mark.parametrize(
    "tamper",
    [
        edit("tests/test_calc.py", "def test_add():", "@pytest.mark.skip\ndef test_add():"),
        edit("tests/test_calc.py", "    assert add(2, 3) == 5", "    assert add(2, 3) == -1"),
        edit("tests/test_calc.py", "def test_add():\n    assert add(2, 3) == 5\n", ""),
        bash("rm tests/test_calc.py"),
        bash("git rm -q tests/test_calc.py"),
        edit("conftest.py", "", "collect_ignore = ['tests/test_calc.py']"),
        edit("pyproject.toml", "", 'addopts = "-k not add"'),
        write("tests/test_calc.py", "def test_nothing(): pass", kind="update"),
        edit("src/calc.test.ts", "it('adds', () => {", "it.skip('adds', () => {"),
        bash("sed -i 's/== 5/== -1/' tests/test_calc.py"),
        bash("cat > tests/test_calc.py <<'EOF'\ndef test_ok():\n    pass\nEOF"),
        bash("git checkout HEAD -- tests/test_calc.py"),
        bash("git stash"),
        bash("python3 scripts/rewrite_tests.py"),
        bash("echo 'addopts = \"-k mul\"' >> pyproject.toml"),
        bash("jest -u", "Tests: 3 passed, 3 total"),
        bash("pytest --snapshot-update", GREEN),
        bash("UPDATE_GOLDEN=1 pytest", GREEN),
        bash("export UPDATE_GOLDEN=1 && pytest", GREEN),
    ],
)
def test_test_tampering_blocks_a_pass(tamper: object) -> None:
    run = session(edit("calc.py"), tamper, bash("pytest", GREEN))  # type: ignore[arg-type]
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE
    assert "weakened" in reason


def test_adding_tests_is_not_tampering() -> None:
    run = session(
        edit("calc.py"),
        write("tests/test_more.py", "def test_more():\n    assert add(1, 1) == 2\n"),
        edit("tests/test_calc.py", "", "def test_extra():\n    assert add(0, 0) == 0"),
        bash("pytest", GREEN),
    )
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED


@pytest.mark.parametrize(
    "later",
    [
        tool("Task", description="cleanup", subagent_type="general"),
        tool("mcp__fs__write_file", path="calc.py"),
        bash("python scripts/fix.py"),
        bash("sed -i 's/+/-/' calc.py"),
        bash("git checkout -- calc.py"),
        bash("ruff format ."),
        bash("npm install"),
    ],
)
def test_possible_changes_after_the_tests_make_them_stale(later: object) -> None:
    run = session(edit("calc.py"), bash("pytest", GREEN), later)  # type: ignore[arg-type]
    assert _check(ALL_PASS, run)[0] is Support.NO_EVIDENCE


@pytest.mark.parametrize(
    "later",
    [
        bash("git status && git diff"),
        bash("cat calc.py"),
        bash("ruff check ."),
        tool("Read", file_path="/work/toy/calc.py"),
        tool("TodoWrite", todos=[]),
    ],
)
def test_read_only_steps_after_the_tests_are_fine(later: object) -> None:
    run = session(edit("calc.py"), bash("pytest", GREEN), later)  # type: ignore[arg-type]
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED


@pytest.mark.parametrize(
    "step",
    [
        bash("python3 -c 'print(1 + 2)'"),
        bash("ruff format tests/"),
        bash("git checkout -b fix-calc"),
        bash("cp calc.py /tmp/calc.py.orig"),
        bash("cat tests/test_calc.py"),
    ],
)
def test_harmless_shell_steps_do_not_weaken_the_suite(step: object) -> None:
    run = session(step, edit("calc.py"), bash("pytest", GREEN))  # type: ignore[arg-type]
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED


@pytest.mark.parametrize(
    "job",
    [
        bash("(sleep 3; sed -i 's/a + b/a - b/' calc.py) > /dev/null 2>&1 &"),
        bash("nohup python3 revert.py &"),
        bash("setsid python3 revert.py"),
        bash("sed -i 's/+/-/' calc.py", "", None, background=True),
    ],
)
def test_background_changes_last_until_the_session_ends(job: object) -> None:
    run = session(edit("calc.py"), job, bash("pytest", GREEN), bash("ruff check ."))  # type: ignore[arg-type]
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE and "until the session ends" in reason
    assert _check(BuildPredicate(category="lint"), run)[0] is Support.NO_EVIDENCE


def test_a_background_test_run_changes_nothing() -> None:
    run = session(edit("calc.py"), bash("pytest -q &", "", None), bash("pytest", GREEN))
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED


@pytest.mark.parametrize(
    ("steps", "where"),
    [
        ([bash("cd pkg_b")], "ran in pkg_b"),
        ([bash("cd pkg_b"), bash("cd sub")], "ran in pkg_b/sub"),
        ([bash("cd ..")], "ran in /work, not"),
        ([bash("pushd pkg_b"), bash("popd")], "a directory only the shell knows"),
        ([bash("cd pkg_b || true")], "a directory only the shell knows"),
        ([bash("cd nowhere", "no such directory", 1)], "a directory only the shell knows"),
        ([bash("cd $PKG")], "a directory only the shell knows"),
    ],
)
def test_a_directory_kept_from_an_earlier_call_is_not_the_suite(
    steps: list[ToolCall], where: str
) -> None:
    run = session(edit("calc.py"), *steps, bash("pytest -q", GREEN))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE and where in reason, reason


@pytest.mark.parametrize(
    "steps",
    [
        [bash("cd pkg_b"), bash("cd /work/toy")],
        [bash("cd pkg_b && cd ..")],
        [bash("(cd pkg_b && make build)")],
        [bash("cd pkg_b | cat")],
        [bash("cd pkg_b", status=CallStatus.DENIED)],
        [bash("cd pkg_b"), bash("cd /work/toy && ls")],
        [bash("cd ../toy")],
    ],
)
def test_returning_to_the_session_directory(steps: list[ToolCall]) -> None:
    run = session(edit("calc.py"), *steps, bash("pytest -q", GREEN))
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED


def test_the_recorded_directory_of_the_run_counts() -> None:
    run = session(edit("calc.py"), bash("pytest -q", GREEN, cwd="/work/toy/pkg_b"))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE and "ran in pkg_b" in reason
    outside = session(edit("calc.py"), bash("pytest -q", GREEN, cwd="/elsewhere"))
    assert "ran in /elsewhere" in _check(ALL_PASS, outside)[1]
    back = session(bash("ls", cwd="/work/toy/pkg_b"), edit("calc.py"), bash("pytest -q", GREEN))
    assert _check(ALL_PASS, back)[0] is Support.SUPPORTED


def test_an_inline_cd_back_to_the_session_directory_is_whole() -> None:
    run = session(edit("calc.py"), bash("cd pkg_b"), bash("cd /work/toy && pytest -q", GREEN))
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED
    elsewhere = session(edit("calc.py"), bash("cd pkg_b && pytest -q", GREEN))
    status, reason = _check(ALL_PASS, elsewhere)
    assert status is Support.NO_EVIDENCE and "ran in pkg_b" in reason
    subshell = session(edit("calc.py"), bash("(cd /work/toy && pytest -q)", GREEN))
    assert "a directory only the shell knows" in _check(ALL_PASS, subshell)[1]


def test_more_skips_than_the_baseline_is_not_a_pass() -> None:
    skipped = "1 passed, 2 skipped in 0s"
    run = session(bash("pytest -q", RED, 1), edit("calc.py"), bash("pytest -q", skipped))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE
    assert "skipped 2 tests, more than the 0 of the earlier whole-suite run cmd#1" in reason
    same = session(
        bash("pytest -q", "1 failed, 2 skipped"),
        edit("calc.py"),
        bash("pytest -q", "1 passed, 2 skipped"),
    )
    assert _check(ALL_PASS, same)[0] is Support.SUPPORTED


def test_a_new_env_prefix_on_the_run_is_not_the_whole_suite() -> None:
    run = session(bash("pytest -q", RED, 1), edit("calc.py"), bash("CI=true pytest -q", GREEN))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE
    assert "runs with CI set, which no earlier test run had" in reason
    via_env = session(edit("calc.py"), bash("env DEBUG=1 pytest -q", GREEN))
    assert "runs with DEBUG set" in _check(ALL_PASS, via_env)[1]
    kept = session(
        bash("CI=true pytest -q", RED, 1), edit("calc.py"), bash("CI=true pytest -q", GREEN)
    )
    assert _check(ALL_PASS, kept)[0] is Support.SUPPORTED
    exported = session(edit("calc.py"), bash("export CI=true"), bash("pytest -q", GREEN))
    assert "runs with CI set" in _check(ALL_PASS, exported)[1]
    formatting = session(edit("calc.py"), bash("export NO_COLOR=1"), bash("pytest -q", GREEN))
    assert _check(ALL_PASS, formatting)[0] is Support.SUPPORTED
    for prefix in ("NO_COLOR=1", "FORCE_COLOR=1", "PYTHONPATH=.", "COLUMNS=200", "TERM=dumb"):
        allowed = session(edit("calc.py"), bash(f"{prefix} pytest -q", GREEN))
        assert _check(ALL_PASS, allowed)[0] is Support.SUPPORTED, prefix


def test_a_shrunken_suite_is_not_the_whole_suite() -> None:
    run = session(
        bash("pytest -q", RED, 1), edit("calc.py"), bash("pytest -q", ".\n1 passed in 0s")
    )
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE
    assert "ran 1 tests, fewer than the 3 of the earlier whole-suite run cmd#1" in reason
    assert _check(TestResultPredicate(whole_suite=False), run)[0] is Support.SUPPORTED
    grown = session(bash("pytest -q", RED, 1), edit("calc.py"), bash("pytest -q", "4 passed in 0s"))
    assert _check(ALL_PASS, grown)[0] is Support.SUPPORTED
    filtered_first = session(
        bash("pytest -q -k add", "2 passed, 1 deselected in 0s"),
        edit("calc.py"),
        bash("pytest -q", "2 passed in 0s"),
    )
    assert _check(ALL_PASS, filtered_first)[0] is Support.SUPPORTED
    other_runner = session(bash("make test", RED, 1), edit("calc.py"), bash("pytest", "1 passed"))
    assert _check(ALL_PASS, other_runner)[0] is Support.SUPPORTED


BASELINE = bash("pytest -q", "FF.F\n== 3 failed, 1 passed in 0.02s ==", 1)


@pytest.mark.parametrize(
    "runs",
    [
        [bash("pytest -q tests pkg_b", "4 passed in 0s")],
        [bash("pytest -q tests", "3 passed in 0s"), bash("cd pkg_b && pytest -q", "1 passed")],
        [bash("pytest tests/", "3 passed"), bash("pytest /work/toy/pkg_b", "1 passed")],
        [
            bash("pytest pkg_b", "1 passed"),
            bash("pytest pkg_b", "1 passed"),
            bash("pytest tests", "3 passed"),
        ],
        [bash("PYTHONPATH=. pytest -q -p no:cacheprovider tests pkg_b", "4 passed")],
    ],
)
def test_disjoint_path_runs_can_cover_the_whole_suite(runs: list[ToolCall]) -> None:
    run = session(BASELINE, edit("calc.py"), *runs)
    status, reason = _check(ALL_PASS, run)
    assert status is Support.SUPPORTED, reason
    assert "every test of the whole-suite run cmd#1" in reason


@pytest.mark.parametrize(
    ("steps", "runs"),
    [
        ([], [bash("pytest -q tests", "3 passed in 0s")]),
        ([], [bash("pytest tests tests/test_calc.py", "4 passed")]),
        ([], [bash("pytest tests", "3 passed"), bash("pytest tests/test_calc.py", "1 passed")]),
        ([], [bash("pytest tests/test_calc.py::test_add pkg_b", "4 passed")]),
        ([], [bash("pytest -k add tests pkg_b", "4 passed")]),
        ([], [bash("pytest ../other", "4 passed")]),
        ([], [bash("pytest $DIR", "4 passed")]),
        ([], [bash("pytest -q tests && cd pkg_b && pytest -q", "3 passed\n1 passed")]),
        (
            [write("pkg_c/test_pad.py", "def test_p():\n    pass\n")],
            [bash("pytest pkg_b pkg_c", "4 passed")],
        ),
        (
            [edit("pkg_c/check.py", "x = 1", "def test_p():\n    pass")],
            [bash("pytest pkg_b pkg_c", "4 passed")],
        ),
        ([bash("cp /tmp/pad.py pkg_c/test_pad.py")], [bash("pytest pkg_b pkg_c", "4 passed")]),
    ],
)
def test_path_runs_that_may_miss_tests_do_not_cover(
    steps: list[ToolCall], runs: list[ToolCall]
) -> None:
    run = session(BASELINE, edit("calc.py"), *steps, *runs)
    assert _check(ALL_PASS, run)[0] is Support.NO_EVIDENCE


def test_path_runs_must_cover_the_files_the_whole_run_named() -> None:
    named = bash(
        "pytest -q",
        "FF..\nFAILED tests/test_calc.py::test_add\n/work/toy/tests/test_calc.py:5: Error\n"
        "== 1 failed, 3 passed in 0.02s ==",
        1,
    )
    elsewhere = session(named, edit("calc.py"), bash("cd pkg_b && pytest -q", "4 passed"))
    assert _check(ALL_PASS, elsewhere)[0] is Support.NO_EVIDENCE
    covering = session(named, edit("calc.py"), bash("pytest -q tests pkg_b", "4 passed"))
    assert _check(ALL_PASS, covering)[0] is Support.SUPPORTED


def test_path_runs_need_an_earlier_whole_suite_run() -> None:
    run = session(edit("calc.py"), bash("pytest tests pkg_b", "4 passed"))
    assert _check(ALL_PASS, run)[0] is Support.NO_EVIDENCE
    small = session(BASELINE, edit("calc.py"), bash("pytest tests pkg_b", "4 passed"))
    assert _check(TestResultPredicate(min_tests=5), small)[0] is Support.NO_EVIDENCE


def test_a_declared_minimum_suite_size() -> None:
    run = session(edit("calc.py"), bash("pytest -q", GREEN))
    status, reason = _check(TestResultPredicate(min_tests=5), run)
    assert status is Support.NO_EVIDENCE and "fewer than the 5 the suite has" in reason
    assert _check(TestResultPredicate(min_tests=3), run)[0] is Support.SUPPORTED
    with pytest.raises(ValidationError):
        TestResultPredicate(min_tests=0)


def test_setup_in_the_same_command_does_not_stale_the_run() -> None:
    run = session(edit("calc.py"), bash("uv sync && pytest", GREEN))
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED


def test_ignored_edits_do_not_stale_the_run() -> None:
    run = session(edit("calc.py"), bash("pytest", GREEN), edit("README.md", "a", "b"))
    assert _check(ALL_PASS, run)[0] is Support.NO_EVIDENCE
    assert _check(ALL_PASS, run, ignore_edits=["*.md"])[0] is Support.SUPPORTED


def test_expect_fail() -> None:
    fail = TestResultPredicate(expect="fail", whole_suite=False)
    assert _check(fail, session(bash("pytest", RED, 1)))[0] is Support.SUPPORTED
    assert _check(fail, session(bash("pytest", GREEN)))[0] is Support.CONTRADICTED
    assert _check(fail, session(bash("pytest", RED, 1), bash("pytest", GREEN)))[0] is (
        Support.NO_EVIDENCE
    )
    assert _check(fail, session(bash("pytest || true", GREEN)))[0] is Support.NO_EVIDENCE


def test_named_tests() -> None:
    named = TestResultPredicate(whole_suite=False, mentions="test_add")
    targeted = session(
        edit("calc.py"), bash("pytest tests/test_calc.py::test_add", "1 passed in 0.1s")
    )
    assert _check(named, targeted)[0] is Support.SUPPORTED
    whole = session(edit("calc.py"), bash("pytest", GREEN))
    assert _check(named, whole)[0] is Support.SUPPORTED
    other = session(edit("calc.py"), bash("pytest -k mul", "1 passed, 2 deselected in 0.1s"))
    assert _check(named, other)[0] is Support.NO_EVIDENCE
    failing = "FAILED tests/test_calc.py::test_add - assert\n== 1 failed, 2 passed in 0.1s =="
    assert _check(named, session(edit("calc.py"), bash("pytest", failing, 1)))[0] is (
        Support.CONTRADICTED
    )
    unrelated = "FAILED tests/test_calc.py::test_mul - assert\n== 1 failed, 2 passed in 0.1s =="
    assert _check(named, session(edit("calc.py"), bash("pytest", unrelated, 1)))[0] is (
        Support.NO_EVIDENCE
    )


def test_runner_filter() -> None:
    run = session(edit("calc.py"), bash("pytest", GREEN))
    assert _check(TestResultPredicate(runner="pytest"), run)[0] is Support.SUPPORTED
    assert _check(TestResultPredicate(runner="jest"), run)[0] is Support.NO_EVIDENCE


def test_command_predicates() -> None:
    run = session(
        bash("ruff check .", "All checks passed!"),
        bash("pytest -q || true", RED),
        bash("mypy src", "error", 1),
        edit("calc.py"),
        bash("ruff check  .", "Found 1 error", 1),
    )
    assert _check(CommandPredicate(contains="ruff check ."), run)[0] is Support.SUPPORTED
    assert _check(CommandPredicate(contains="RUFF CHECK ."), run)[0] is Support.SUPPORTED
    assert _check(CommandPredicate(contains="eslint"), run)[0] is Support.CONTRADICTED
    assert _check(CommandPredicate(regex=r"^ruff\s+check"), run)[0] is Support.SUPPORTED
    assert _check(CommandPredicate(category="typecheck"), run)[0] is Support.SUPPORTED
    assert _check(CommandPredicate(category="build"), run)[0] is Support.CONTRADICTED
    assert _check(CommandPredicate(contains="ruff check", ok=True), run)[0] is Support.SUPPORTED
    last = CommandPredicate(contains="ruff check", ok=True, scope="last")
    assert _check(last, run)[0] is Support.CONTRADICTED
    fresh = CommandPredicate(contains="ruff check", exit_code=1, scope="after_last_edit")
    assert _check(fresh, run)[0] is Support.SUPPORTED
    assert _check(CommandPredicate(contains="mypy", ok=True), run)[0] is Support.CONTRADICTED
    status, reason = _check(CommandPredicate(contains="pytest", ok=True), run)
    assert status is Support.NO_EVIDENCE and "exit status is unknown" in reason
    gone = CommandPredicate(contains="mypy", scope="after_last_edit")
    assert _check(gone, run)[0] is Support.CONTRADICTED


def test_absent_command_with_unseen_actors_is_unproven() -> None:
    run = session(tool("Task", description="lint everything"))
    status, reason = _check(CommandPredicate(category="lint"), run)
    assert status is Support.NO_EVIDENCE and "does not show every action" in reason
    incomplete = session(bash("ls"), complete=False)
    assert _check(CommandPredicate(category="lint"), incomplete)[0] is Support.NO_EVIDENCE


def test_command_predicate_validation() -> None:
    with pytest.raises(ValidationError):
        CommandPredicate()
    with pytest.raises(ValidationError):
        CommandPredicate(contains="ruff", regex="ruff")
    with pytest.raises(ValidationError):
        CommandPredicate(contains="ruff", ok=True, exit_code=0)
    with pytest.raises(ValidationError):
        CommandPredicate(contains="ab")
    with pytest.raises(ValidationError):
        CommandPredicate(regex="(")


def test_edit_predicates() -> None:
    run = session(
        edit("src/calc.py"),
        write("docs/new.md", "hello"),
        bash("rm old.txt"),
        bash("echo x > notes/out.txt"),
    )
    assert _check(EditPredicate(path="calc.py"), run)[0] is Support.SUPPORTED
    assert _check(EditPredicate(path="src/**"), run)[0] is Support.SUPPORTED
    assert _check(EditPredicate(path="docs/new.md", change="add"), run)[0] is Support.SUPPORTED
    assert _check(EditPredicate(path="old.txt", change="delete"), run)[0] is Support.SUPPORTED
    assert _check(EditPredicate(path="notes/out.txt"), run)[0] is Support.SUPPORTED
    assert _check(EditPredicate(path="calc.py", change="delete"), run)[0] is Support.CONTRADICTED
    assert _check(EditPredicate(path="lib/**"), run)[0] is Support.CONTRADICTED
    untouched = EditPredicate(path="tests/**", touched=False)
    assert _check(untouched, run)[0] is Support.SUPPORTED
    assert _check(EditPredicate(path="src", touched=False), run)[0] is Support.CONTRADICTED
    assert _check(EditPredicate(path="old.txt", touched=False), run)[0] is Support.CONTRADICTED
    assert _check(EditPredicate(path="out.txt", touched=False), run)[0] is Support.CONTRADICTED


def test_untouched_needs_a_complete_view() -> None:
    script = session(edit("calc.py"), bash("python tools/regen.py"))
    status, reason = _check(EditPredicate(test_files=True, touched=False), script)
    assert status is Support.NO_EVIDENCE and "cmd#1" in reason
    incomplete = session(edit("calc.py"), complete=False)
    assert _check(EditPredicate(test_files=True, touched=False), incomplete)[0] is (
        Support.NO_EVIDENCE
    )
    unseen = session(tool("Task", description="x"))
    assert _check(EditPredicate(path="calc.py"), unseen)[0] is Support.NO_EVIDENCE


def test_test_files_and_everything() -> None:
    run = session(edit("tests/test_calc.py", "", "def test_x():\n    assert 1"))
    assert _check(EditPredicate(test_files=True), run)[0] is Support.SUPPORTED
    assert _check(EditPredicate(test_files=True, touched=False), run)[0] is Support.CONTRADICTED
    assert _check(EditPredicate(path="**", touched=False), run)[0] is Support.CONTRADICTED
    assert _check(EditPredicate(path="**", touched=False), session(bash("ls")))[0] is (
        Support.SUPPORTED
    )


def test_edit_predicate_validation() -> None:
    with pytest.raises(ValidationError):
        EditPredicate()
    with pytest.raises(ValidationError):
        EditPredicate(path="x.py", test_files=True)
    with pytest.raises(ValidationError):
        EditPredicate(path="**")
    with pytest.raises(ValidationError):
        EditPredicate(path="  ")
    assert EditPredicate(path="./src/a.py").path == "src/a.py"


def test_build_predicates() -> None:
    run = session(
        bash("npm run build", "built", 0),
        edit("calc.py"),
        bash("ruff check .", "", 0),
        bash("pyright", "1 error", 1),
        bash("tsc --noEmit || true", "", 0),
    )
    assert _check(BuildPredicate(category="lint"), run)[0] is Support.SUPPORTED
    assert _check(BuildPredicate(category="typecheck"), run)[0] is Support.NO_EVIDENCE
    status, reason = _check(BuildPredicate(category="build"), run)
    assert status is Support.NO_EVIDENCE and "before the workspace changed" in reason
    assert _check(BuildPredicate(category="format"), run)[0] is Support.NO_EVIDENCE
    failing = session(edit("calc.py"), bash("pyright", "1 error", 1))
    assert _check(BuildPredicate(category="typecheck"), failing)[0] is Support.CONTRADICTED
    assert _check(BuildPredicate(category="typecheck", ok=False), failing)[0] is Support.SUPPORTED
    fixed = session(edit("calc.py"), bash("ruff check --fix .", "", 0))
    assert _check(BuildPredicate(category="lint"), fixed)[0] is Support.SUPPORTED


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("calc.py", "calc.py", True),
        ("calc.py", "src/calc.py", True),
        ("calc.py", "/work/toy/calc.py", True),
        ("*.py", "src/calc.py", True),
        ("tests", "tests/test_calc.py", True),
        ("tests/**", "tests/unit/test_calc.py", True),
        ("tests/", "tests/test_calc.py", True),
        ("src/*.py", "src/calc.py", True),
        ("src/*.py", "src/sub/calc.py", False),
        ("src/**/*.py", "src/sub/calc.py", True),
        ("src/**/*.py", "src/calc.py", True),
        ("src/calc.py", "/elsewhere/src/calc.py", True),
        ("src/calc.py", "lib/src/calc.py", False),
        ("calc?.py", "calc1.py", True),
        ("tests/**", "src/tests_util.py", False),
    ],
)
def test_path_matches(pattern: str, path: str, expected: bool) -> None:
    assert path_matches(pattern, path) is expected


def _edit_with_original(name: str, original: str, **tool_input: object) -> ToolCall:
    return ToolCall(
        name=name,
        input={"file_path": f"{CWD}/pyproject.toml", **tool_input},
        status=CallStatus.OK,
        result={"originalFile": original},
    )


PYPROJECT = '[project]\nversion = "1"\n\n[tool.pytest.ini_options]\nasyncio_mode = "strict"\n'


@pytest.mark.parametrize(
    "call",
    [
        _edit_with_original("Edit", PYPROJECT, old_string='"strict"', new_string='"auto"'),
        _edit_with_original(
            "MultiEdit",
            PYPROJECT,
            edits=[{"old_string": '"strict"', "new_string": '"auto"', "replace_all": True}],
        ),
        _edit_with_original("Write", PYPROJECT, content=PYPROJECT.replace("strict", "auto")),
    ],
)
def test_pytest_sections_of_shared_config_are_test_configuration(call: ToolCall) -> None:
    run = session(edit("calc.py"), call, bash("pytest", GREEN))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE and "[tool.pytest.ini_options]" in reason


@pytest.mark.parametrize(
    "call",
    [
        _edit_with_original("Edit", PYPROJECT, old_string='"1"', new_string='"2"'),
        _edit_with_original("Edit", PYPROJECT, old_string="missing", new_string="x"),
    ],
)
def test_other_sections_of_shared_config_are_not(call: ToolCall) -> None:
    run = session(edit("calc.py"), call, bash("pytest", GREEN))
    assert _check(ALL_PASS, run)[0] is Support.SUPPORTED


@pytest.mark.parametrize(
    "call",
    [
        _edit_with_original("MultiEdit", PYPROJECT, edits=["not an edit"]),
        _edit_with_original("Write", PYPROJECT),
    ],
)
def test_shared_config_without_a_usable_diff_is_suspect(call: ToolCall) -> None:
    run = session(edit("calc.py"), call, bash("pytest", GREEN))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE and "pyproject.toml" in reason


def test_an_exported_update_variable_reaches_later_runs() -> None:
    run = session(edit("calc.py"), bash("export UPDATE_GOLDEN=1"), bash("pytest", GREEN))
    status, reason = _check(ALL_PASS, run)
    assert status is Support.NO_EVIDENCE
    assert "UPDATE_GOLDEN set" in reason
