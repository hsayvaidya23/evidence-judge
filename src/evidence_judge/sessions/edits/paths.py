"""Which files are tests, test configuration, harness, session configuration or data."""

from __future__ import annotations

import posixpath
import re
from typing import Final

_TEST_PATH: Final = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?|testing|e2e|integration_tests|test[_-]?data|fixtures|"
    r"goldens?|__snapshots__|snapshots|expected|cassettes|baselines?)/|"
    r"(?:^|/)test[^/]*\.py$|_tests?\.[^/.]+$|(?:^|/)conftest\.py$|_spec\.[^/.]+$|"
    r"\.(?:test|spec)\.[^/]+$|Tests?\.(?:java|kt|cs|swift|php|scala|groovy)$|"
    r"(?:^|/)tests?\.[^/.]+$|(?:^|/)test_[^/]+$",
    re.IGNORECASE,
)


_TEST_CONFIG: Final = re.compile(
    r"(?:^|/)(?:\.?pytest\.(?:ini|toml)|tox\.ini|noxfile\.py|\.mocharc(?:\.[a-z]+)?|"
    r"karma\.conf\.[cm]?[jt]s|phpunit\.xml(?:\.dist)?|\.rspec|"
    r"(?:jest|vitest|playwright|cypress|ava|wdio|nightwatch)\.config\.[cm]?[jt]s|"
    r"(?:jest|vitest)\.config\.json|vitest\.workspace\.[a-z]+|\.nycrc(?:\.[a-z]+)?|"
    r"\.c8rc(?:\.[a-z]+)?|\.taprc|\.coveragerc|codecov\.ya?ml|nextest\.toml|"
    r"\.cargo/config(?:\.toml)?|"
    r"(?:GNU)?[Mm]akefile|[^/]+\.mk|\.?[Jj]ustfile|[Tt]askfile(?:\.dist)?\.ya?ml|Rakefile|"
    r"magefile\.go|\.envrc|\.env(?:\.[\w.-]+)?)$",
    re.IGNORECASE,
)
"""Files that only configure how tests are found and run, or say what `make test`, `just test`
or the shell environment runs: creating or changing one changes what a pass means."""


SHARED_CONFIG: Final = re.compile(
    r"(?:^|/)(?:pyproject\.toml|setup\.cfg|Cargo\.toml|pom\.xml|build\.gradle(?:\.kts)?|"
    r"deno\.jsonc?|composer\.json|\.github/workflows/[^/]+\.ya?ml)$",
    re.IGNORECASE,
)


PACKAGE_JSON: Final = re.compile(r"(?:^|/)package\.json$", re.IGNORECASE)


_RUNNER_NAMES: Final = frozenset(
    {"pytest", "py.test", "jest", "vitest", "mocha", "ava", "tap", "go", "cargo", "npm", "npx",
     "pnpm", "yarn", "bun", "make", "tox", "nox", "rspec", "phpunit", "dotnet", "mvn", "gradle",
     "python", "python3", "node", "deno", "unittest", "nose2"}
)  # fmt: skip


_HARNESS_NAMES: Final = frozenset({"conftest.py", "sitecustomize.py", "usercustomize.py"})


_HARNESS_DIRS: Final = re.compile(
    r"(?:^|/)(?:site-packages|dist-packages|node_modules|\.venv/bin|venv/bin|\.tox|\.nox)/"
)


def is_harness_path(path: str) -> bool:
    """A file the test runner itself loads or is: conftest and site hooks, `.pth` files, a file
    named like a runner (`./pytest`, `pytest.py`), installed packages and virtualenv scripts.
    Changing one can make any suite report a pass."""
    name = posixpath.basename(path.rstrip("/"))
    stem = name.removesuffix(".py").removesuffix(".js").removesuffix(".sh")
    return (
        name in _HARNESS_NAMES
        or name.endswith(".pth")
        or stem in _RUNNER_NAMES
        or _HARNESS_DIRS.search(path) is not None
    )


_SESSION_CONFIG: Final = re.compile(
    r"(?:^|/)(?:\.bashrc|\.bash_profile|\.bash_login|\.bash_logout|\.profile|\.zshenv|"
    r"\.zshrc|\.zprofile|\.zlogin|\.zlogout|\.kshrc|\.mkshrc|\.cshrc|\.tcshrc|\.npmrc|"
    r"\.yarnrc(?:\.ya?ml)?|\.pnpmrc|\.?bunfig\.toml)$|(?:^|/)\.config/fish/|"
    r"(?:^|/)\.config/go/env$|(?:^|/)Library/Application Support/go/env$|"
    r"(?:^|/)etc/(?:profile|bashrc|bash\.bashrc|zshenv|zshrc|zprofile|environment)$|"
    r"(?:^|/)etc/(?:profile\.d|zsh)/"
)
"""Shell startup files (`~/.bashrc`, `~/.zshenv`, fish's config) that every later shell of the
session runs, including the login shells harnesses start for each command, and settings files
that npm, yarn, pnpm, bun and go read on every run (`.npmrc` can set `node-options`, the go env
file `GOFLAGS`)."""


_GIT_METADATA: Final = re.compile(r"(?:^|/)\.git(?:/|$)")


def git_metadata_reason(path: str) -> str | None:
    """Why writing `path` inside `.git` changes the test harness, or None. Git's own commands
    write there; a file write that does can change what git lists as the project's files
    (`info/exclude`, `config`), which is what the judge's re-run copies."""
    if _GIT_METADATA.search(path) is None:
        return None
    return f"wrote {path} inside .git, which decides what git lists as the project's files"


def session_config_reason(path: str) -> str | None:
    """Why writing `path` changes how every later command of the session runs, or None."""
    if _SESSION_CONFIG.search(path) is None:
        return None
    return (
        f"changed {path}, a shell startup or tool settings file that every later command of "
        "the session can read"
    )


def is_test_path(path: str) -> bool:
    """A test file or test directory (`tests.py`, `test_x.py`, `x_test.go`, `x.spec.ts`,
    `tests/`, `__tests__/`, ...), named with or without a trailing slash."""
    return _TEST_PATH.search(path) is not None or _TEST_PATH.search(path + "/") is not None


def is_test_config(path: str) -> bool:
    return _TEST_CONFIG.search(path) is not None


def is_config_path(path: str) -> bool:
    """A file that can hold test configuration among other settings (pyproject.toml,
    package.json, ...); a shell write to one cannot be told apart from a test-config change."""
    return any(p.search(path) for p in (_TEST_CONFIG, SHARED_CONFIG, PACKAGE_JSON))


CODE_SUFFIX: Final = re.compile(
    r"\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|rb|java|kt|cs|swift|php|c|h|cc|cpp|hpp|scala)$"
)


def is_code_path(path: str) -> bool:
    """A source file of a language the judge knows (`.py`, `.js`, `.go`, ...)."""
    return CODE_SUFFIX.search(path) is not None


def _strip_up(literal: str) -> str:
    while literal.startswith(("./", "../")):
        literal = literal.split("/", 1)[1]
    return literal


def data_reader(path: str, opened: dict[str, set[str]]) -> str | None:
    """The test module that reads `path` as data (expected values, cases, recorded answers),
    or None. Code files are left to the other checks."""
    if is_test_path(path) or CODE_SUFFIX.search(path) or is_config_path(path):
        return None
    for module, literals in opened.items():
        for literal in literals:
            lit = _strip_up(literal).rstrip("/")
            if not lit:
                continue
            if literal.endswith("/"):
                if path.startswith(lit + "/") or f"/{lit}/" in path:
                    return module
            elif path == lit or path.endswith("/" + lit):
                return module
    return None


def expected_module(
    path: str, changed: set[str] | None, imports: dict[str, dict[str, set[str] | None]]
) -> tuple[str, list[str]] | None:
    """The test module that imports values from `path` the edit changed, and those names;
    None when no shown test module takes a changed value from it."""
    if is_test_path(path) or not path.endswith(".py"):
        return None
    for reader, modules in imports.items():
        for module, names in modules.items():
            stem = module.replace(".", "/")
            files = (f"{stem}.py", f"{stem}/__init__.py")
            if not any(path == f or path.endswith("/" + f) for f in files):
                continue
            if changed is None:
                continue
            hit = sorted(changed if names is None else changed & names)
            if hit:
                return reader, hit
    return None
