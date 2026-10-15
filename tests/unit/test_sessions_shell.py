from __future__ import annotations

import pytest

from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell import analyse, tokenize, unwrap_shell


@pytest.mark.parametrize(
    ("command", "runner"),
    [
        ("pytest", "pytest"),
        (".venv/bin/python -m pytest -q", "pytest"),
        ("python3 -mpytest", "pytest"),
        ("uv run --with pytest-xdist pytest -n 4", "pytest"),
        ("timeout 120 poetry run pytest", "pytest"),
        ("python -m unittest discover", "unittest"),
        ("npx jest", "jest"),
        ("npx vitest run", "vitest"),
        ("npm test", "npm test"),
        ("pnpm run test", "pnpm test"),
        ("yarn test", "yarn test"),
        ("bun test", "bun test"),
        ("go test ./...", "go test"),
        ("cargo test", "cargo test"),
        ("cargo nextest run", "cargo nextest"),
        ("bundle exec rspec", "rspec"),
        ("make test", "make test"),
        ("tox", "tox"),
        ("dotnet test", "dotnet test"),
        ("mvn test", "mvn"),
        ("bash -lc 'pytest -q'", "pytest"),
        ("env CI=1 pytest", "pytest"),
    ],
)
def test_recognises_test_runners(command: str, runner: str) -> None:
    analysis = analyse(command)
    assert analysis.runners == [runner]
    assert analysis.is_test
    assert analysis.test_filters == []
    assert analysis.exit_masked_by is None


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ("pytest -k add", "-k"),
        ("pytest -kadd", "-k"),
        ("pytest -m 'not slow'", "-m"),
        ("pytest --deselect tests/test_a.py::test_b", "--deselect"),
        ("pytest tests/test_calc.py", "only tests/test_calc.py"),
        ("pytest tests/test_calc.py::test_add", "only tests/test_calc.py::test_add"),
        ("pytest --lf", "--lf"),
        ("pytest -c other.ini", "a different config (-c)"),
        ("pytest -o addopts=''", "a config override (-o)"),
        ("pytest --ignore=tests/slow", "--ignore"),
        ("PYTEST_ADDOPTS='-k add' pytest", "PYTEST_ADDOPTS"),
        ("cd sub && pytest", "runs in sub"),
        ("jest -t 'adds'", "-t"),
        ("jest src/calc.test.ts", "only src/calc.test.ts"),
        ("vitest run --changed", "--changed"),
        ("npm test -- -t foo", "arguments passed to npm test: -t foo"),
        ("npm run test:unit", "script test:unit (not the project's test script)"),
        ("go test ./pkg/calc", "only ./pkg/calc"),
        ("go test", "only the current package (no ./...)"),
        ("go test -run TestAdd ./...", "-run"),
        ("go test -short ./...", "-short"),
        ("cargo test add", "only tests matching add"),
        ("cargo test -- --skip slow", "--skip"),
        ("cargo test -p core", "-p"),
        ("make test-unit", "target test-unit"),
        ("tox -e py312", "-e"),
        ("python -m unittest tests.test_calc", "only tests.test_calc"),
    ],
)
def test_filters_make_a_run_a_subset(command: str, fragment: str) -> None:
    filters = analyse(command, "/work").test_filters
    assert any(fragment in f for f in filters), filters


def test_cd_into_the_session_directory_is_not_a_filter() -> None:
    assert analyse("cd /work && pytest", "/work").test_filters == []
    assert analyse("cd . && pytest", None).test_filters == []


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ("pytest || true", "'||'"),
        ("pytest; exit 0", "';'"),
        ("pytest ; true", "';'"),
        ("pytest\necho done", "a newline"),
        ("pytest | grep passed", "piped into grep"),
        ("pytest 2>&1 | tail -n 5", "piped into tail"),
        ("pytest | tail -3 | grep -v FAIL", "piped into grep"),
        ("pytest &", "background"),
        ("! pytest", "negated"),
        ("pytest && python report.py", "other commands run after it"),
        ("bash -c 'pytest || true'", "'||'"),
    ],
)
def test_masked_exit_status(command: str, fragment: str) -> None:
    masked = analyse(command).exit_masked_by
    assert masked is not None
    assert fragment in masked


@pytest.mark.parametrize(
    "command",
    ["pytest && echo done", "pytest 2>/dev/null", "set -e; pytest", "pytest -x --tb=short"],
)
def test_reliable_exit_status(command: str) -> None:
    assert analyse(command).exit_masked_by is None


def test_pipefail_keeps_the_runner_status() -> None:
    analysis = analyse("set -o pipefail; pytest | tee log.txt")
    assert analysis.exit_masked_by is None
    assert analysis.output_filter == "tee"
    assert analysis.mutation == "tee changes files"


def test_output_filter_is_tail_only_when_every_stage_is_tail() -> None:
    assert analyse("pytest | tail -5").output_filter == "tail"
    assert analyse("pytest | tail -50 | head -3").output_filter == "head"


@pytest.mark.parametrize(
    "command",
    [
        'echo "12 passed in 0.1s"',
        "printf '3 passed\\n'",
        "pytest -q; echo '== 5 passed in 0.02s =='",
        "cat <<EOF\nTests: 4 passed, 4 total\nEOF",
        "echo 'test result: ok. 3 passed'",
    ],
)
def test_summary_text_in_the_command_is_agent_text(command: str) -> None:
    assert analyse(command).agent_text


@pytest.mark.parametrize(
    ("command", "category", "mutates"),
    [
        ("ls -la && cat calc.py", Category.READ_ONLY, False),
        ("grep -rn add src", Category.READ_ONLY, False),
        ("git status && git diff", Category.READ_ONLY, False),
        ("sed -n 1,20p calc.py", Category.READ_ONLY, False),
        ("sed -i 's/a/b/' calc.py", Category.MUTATING, True),
        ("echo hi > notes.txt", Category.MUTATING, True),
        ("echo hi > /dev/null", Category.READ_ONLY, False),
        ("rm -rf build", Category.MUTATING, True),
        ("git checkout -- calc.py", Category.MUTATING, True),
        ("git stash list", Category.READ_ONLY, False),
        ("python script.py", Category.UNKNOWN, True),
        ("ruff check .", Category.LINT, False),
        ("ruff check --fix .", Category.LINT, True),
        ("ruff format --check .", Category.FORMAT, False),
        ("ruff format .", Category.FORMAT, True),
        ("black --check .", Category.FORMAT, False),
        ("prettier --write .", Category.FORMAT, True),
        ("pyright", Category.TYPECHECK, False),
        ("tsc --noEmit", Category.TYPECHECK, False),
        ("tsc", Category.BUILD, False),
        ("npm run build", Category.BUILD, False),
        ("npm run lint", Category.LINT, False),
        ("npm install", Category.MUTATING, True),
        ("uv sync", Category.MUTATING, True),
        ("pip install -e .", Category.MUTATING, True),
        ("cargo build", Category.BUILD, False),
        ("cargo clippy", Category.LINT, False),
        ("go vet ./...", Category.LINT, False),
        ("find . -name '*.pyc' -delete", Category.MUTATING, True),
        ("find . -name '*.py'", Category.READ_ONLY, False),
        ("curl -o out.json https://example.test", Category.MUTATING, True),
        ("awk '{print $1}' data.txt", Category.READ_ONLY, False),
        ("xargs rm", Category.UNKNOWN, True),
        ("jest -u", Category.TEST, True),
    ],
)
def test_categories_and_mutation(command: str, category: Category, mutates: bool) -> None:
    analysis = analyse(command)
    assert analysis.categories[-1] is category
    assert analysis.mutates is mutates


def test_deleted_and_touched_paths() -> None:
    analysis = analyse("rm tests/test_calc.py && cp a.py b.py && echo x > c.txt")
    assert analysis.deleted_paths == ["tests/test_calc.py"]
    assert {"a.py", "b.py", "c.txt"} <= set(analysis.touched_paths)
    assert analyse("git rm -q tests/test_x.py").deleted_paths == ["tests/test_x.py"]
    assert analyse("mv tests/test_x.py /tmp/").deleted_paths == ["tests/test_x.py"]


def test_mutation_before_or_after_the_check() -> None:
    assert not analyse("uv sync && pytest").mutation_after_check
    assert analyse("pytest && sed -i 's/x/y/' a.py").mutation_after_check
    assert not analyse("ruff check --fix .").mutation_after_check
    assert not analyse("ls").mutation_after_check


def test_session_store_is_flagged() -> None:
    assert analyse("cat ~/.claude/projects/x/y.jsonl").session_store
    assert analyse("sed -i 's/failed/passed/' ~/.codex/sessions/2026/r.jsonl").session_store
    assert not analyse("pytest").session_store


def test_unparseable_command_is_unknown_and_mutating() -> None:
    analysis = analyse('pytest "unclosed')
    assert analysis.parse_error is not None
    assert analysis.mutates
    assert analysis.exit_masked_by is not None


def test_overlong_command_is_not_parsed() -> None:
    analysis = analyse("x" * 100_001)
    assert analysis.parse_error is not None


def test_comments_and_heredocs() -> None:
    assert analyse("# run the tests\npytest").runners == ["pytest"]
    heredoc = analyse("cat > fix.py <<'EOF'\nprint('rm -rf /')\nEOF\npython fix.py")
    assert heredoc.touched_paths == ["fix.py"]
    assert "rm" not in heredoc.programs


def test_collect_only_is_not_a_run() -> None:
    assert not analyse("pytest --collect-only").is_test
    assert not analyse("go test -list . ./...").is_test
    assert not analyse("cargo test --no-run").is_test


def test_tokenize_splits_operators() -> None:
    assert tokenize("a&&b||c;d|e") == ["a", "&&", "b", "||", "c", ";", "d", "|", "e"]
    assert tokenize("pytest 2>&1") == ["pytest", "2", ">&", "1"]


def test_unwrap_shell() -> None:
    assert unwrap_shell(["/bin/zsh", "-lc", "pytest -q"]) == "pytest -q"
    assert unwrap_shell(["bash", "-c", "ls"]) == "ls"
    assert unwrap_shell(["pytest", "-k", "a b"]) == "pytest -k 'a b'"
