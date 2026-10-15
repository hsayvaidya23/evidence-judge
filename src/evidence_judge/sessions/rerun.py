"""Independent re-run: the judge runs the project's test suite itself and reads the runner's own
machine-readable report, instead of trusting the test output the session recorded.

An agent running as the user controls everything a transcript shows about a test run: the
environment, shell rc files, runner options, the directory the run happens in and the text it
prints. The re-run takes all of that out of its hands. The command comes from the spec, which the
user owns and the hooks snapshot when the turn starts; the working tree is copied into a
directory the judge owns; the command runs without a shell, in an environment built from
scratch; and the result is read from a report written outside the copy, never from stdout.

What the re-run cannot do is make weakened tests strong: a re-run of a suite whose assertions
were deleted still passes. Transcript analysis keeps deciding that, and runner configuration
that differs from the base revision is reported here as well.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

Runner = Literal["pytest", "go", "jest", "vitest", "cargo", "node", "unittest"]

_ENV_NAME: Final = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
MAX_FILES: Final = 200_000
MAX_BYTES: Final = 2 * 1024**3
MAX_REPORT_BYTES: Final = 64 * 1024 * 1024
_MAX_NAMES: Final = 20_000
_CLOCK_SLACK_S: Final = 2.0
_DEPENDENCY_DIRS: Final = (".venv", "venv", "node_modules")
_SKIPPED_DIRS: Final = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
     ".hypothesis", ".evidence-judge", *_DEPENDENCY_DIRS}
)  # fmt: skip


class RerunSpec(BaseModel):
    """The spec's `rerun:` block: how the judge runs the suite itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: list[str] = Field(
        min_length=1,
        description="The test command as an argv list, run without a shell (no pipes, globs or "
        "variables); the judge appends its report options at the end",
    )
    runner: Runner | None = Field(
        default=None,
        description="How the judge gets a machine-readable report; inferred from the command "
        "when omitted",
    )
    timeout: float = Field(
        default=600.0, gt=0, le=3600, description="Seconds before the run is cut"
    )
    env: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="Variables to set on top of the judge's minimal environment",
    )
    min_tests: int | None = Field(
        default=None, ge=1, description="A pass needs at least this many tests in the report"
    )
    base: str | None = Field(
        default=None,
        description="Git revision the runner configuration is compared with (default: HEAD, or "
        "the revision the hooks saw when the turn started)",
    )

    @field_validator("command")
    @classmethod
    def _argv(cls, value: list[str]) -> list[str]:
        if any(not part or "\0" in part for part in value):
            raise ValueError("command arguments must be non-empty and contain no NUL")
        return value

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        for name, text in value.items():
            if not _ENV_NAME.match(name) or "\0" in text:
                raise ValueError(f"invalid environment variable {name!r}")
        return value


class RerunStatus(StrEnum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    UNKNOWN = "unknown"


class RerunResult(BaseModel):
    """What one re-run showed. `status` is the suite's result as far as the report shows it;
    `problems` and `config_changes` are reasons the run cannot pass whatever its status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: RerunStatus
    reason: str
    runner: str | None = None
    command: list[str] = Field(default_factory=list[str])
    workdir: str = ""
    base: str | None = Field(
        default=None, description="The revision the configuration was compared with"
    )
    exit_code: int | None = None
    duration_s: float = 0.0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failing: list[str] = Field(default_factory=list[str], description="Failing tests, by name")
    passing: list[str] = Field(default_factory=list[str], exclude=True)
    report_read: bool = Field(
        default=False, description="The runner's report was read and consistent"
    )
    config_changes: list[str] = Field(default_factory=list[str])
    problems: list[str] = Field(default_factory=list[str])
    notes: list[str] = Field(default_factory=list[str])

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    def describe(self) -> str:
        parts = [f"{self.passed} passed"]
        for label, count in (
            ("failed", self.failed),
            ("errors", self.errors),
            ("skipped", self.skipped),
        ):
            if count:
                parts.append(f"{count} {label}")
        return ", ".join(parts)

    def at_least(self, minimum: int | None, source: str) -> RerunResult:
        """The result, no longer a pass when the report counted fewer than `minimum` tests."""
        if self.status is not RerunStatus.PASS or minimum is None or self.total >= minimum:
            return self
        return self.model_copy(
            update={
                "status": RerunStatus.UNKNOWN,
                "reason": f"the re-run reported {self.total} tests, fewer than the {minimum} of "
                f"{source}",
            }
        )


class ReportError(Exception):
    """The runner's report is missing, unreadable or contradicts itself."""


@dataclass(slots=True)
class Counts:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failing: list[str] = field(default_factory=list[str])
    passing: list[str] = field(default_factory=list[str])

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    def add(self, name: str, state: Literal["pass", "fail", "error", "skip"]) -> None:
        if state == "pass":
            self.passed += 1
            if len(self.passing) < _MAX_NAMES:
                self.passing.append(name)
        elif state == "skip":
            self.skipped += 1
        else:
            if state == "fail":
                self.failed += 1
            else:
                self.errors += 1
            if len(self.failing) < _MAX_NAMES:
                self.failing.append(name)


def infer_runner(argv: list[str]) -> Runner | None:
    names = [PurePosixPath(a).name for a in argv]
    for i, name in enumerate(names):
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if (
            name in ("pytest", "py.test")
            or (name == "-m" and nxt == "pytest")
            or name == "-mpytest"
        ):
            return "pytest"
        if (name == "-m" and nxt == "unittest") or name == "-munittest":
            return "unittest"
        if name == "go" and nxt == "test":
            return "go"
        if name == "cargo" and nxt == "test":
            return "cargo"
        if name in ("jest", "vitest"):
            return cast(Runner, name)
    if names and names[0] in ("node", "nodejs") and "--test" in argv:
        return "node"
    return None


_UNITTEST_HELPER: Final = """\
import json, os, sys, unittest
report = sys.argv[1]
sys.path[0] = os.getcwd()
program = unittest.main(module=None, argv=["python -m unittest", *sys.argv[2:]], exit=False)
result = program.result
def names(items):
    return [str(test) for test, _ in items]
with open(report, "w", encoding="utf-8") as out:
    json.dump({
        "tests_run": result.testsRun,
        "failures": names(result.failures),
        "errors": names(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": [str(t) for t in result.unexpectedSuccesses],
    }, out)
sys.exit(0 if result.wasSuccessful() else 1)
"""


@dataclass(frozen=True, slots=True)
class _Plan:
    argv: list[str]
    report: Path
    from_stdout: bool = False
    env: dict[str, str] = field(default_factory=dict[str, str])


def _index_after(argv: list[str], program: str, sub: str) -> int | None:
    for i, part in enumerate(argv[:-1]):
        if PurePosixPath(part).name == program and argv[i + 1] == sub:
            return i + 2
    return None


def _plan(argv: list[str], runner: Runner, workspace: Path) -> _Plan:
    """The command with the options that make the runner write its report into `workspace`.

    Raises:
        ReportError: the command has no place for the report options.
    """
    report = workspace / "report"
    match runner:
        case "pytest":
            return _Plan(
                [
                    *argv,
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={report}",
                    "-o",
                    "junit_family=xunit2",
                ],
                report,
            )
        case "node":
            return _Plan(
                [
                    argv[0],
                    "--test-reporter=junit",
                    f"--test-reporter-destination={report}",
                    *argv[1:],
                ],
                report,
            )
        case "jest" | "vitest":
            extra = (
                ["--json", f"--outputFile={report}"]
                if runner == "jest"
                else ["--reporter=json", f"--outputFile={report}"]
            )
            npm = PurePosixPath(argv[0]).name == "npm" and "--" not in argv
            return _Plan([*argv, *(["--"] if npm else []), *extra], report)
        case "go":
            at = _index_after(argv, "go", "test")
            if at is None:
                raise ReportError("the command has no `go test` to add -json to")
            return _Plan([*argv[:at], "-json", *argv[at:]], report, from_stdout=True)
        case "cargo":
            if _index_after(argv, "cargo", "test") is None:
                raise ReportError("the command has no `cargo test` to add a JSON format to")
            libtest = ["-Z", "unstable-options", "--format", "json"]
            full = [*argv, *libtest] if "--" in argv else [*argv, "--", *libtest]
            return _Plan(full, report, from_stdout=True, env={"RUSTC_BOOTSTRAP": "1"})
        case "unittest":
            helper = workspace / "unittest_report.py"
            helper.write_text(_UNITTEST_HELPER, encoding="utf-8")
            for i, part in enumerate(argv):
                if part == "-m" and i + 1 < len(argv) and argv[i + 1] == "unittest":
                    return _Plan([*argv[:i], str(helper), str(report), *argv[i + 2 :]], report)
                if part == "-munittest":
                    return _Plan([*argv[:i], str(helper), str(report), *argv[i + 1 :]], report)
            raise ReportError("the command has no `-m unittest` to replace")


def _safe_xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ReportError("the report declares a DTD or entities")
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))  # noqa: S314 - no DTD
        parser.feed(data)
        return parser.close()
    except ET.ParseError as exc:
        raise ReportError(f"the report is not valid XML ({exc})") from exc


def _int_attr(element: ET.Element, name: str) -> int | None:
    value = element.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise ReportError(f"the report's {name}={value!r} is not a number") from None


def _case_state(case: ET.Element) -> Literal["pass", "fail", "error", "skip"]:
    tags = {child.tag for child in case}
    if "error" in tags:
        return "error"
    if "failure" in tags:
        return "fail"
    if "skipped" in tags:
        return "skip"
    return "pass"


_COMMENT_COUNT: Final = re.compile(r"\A\s*(tests|pass|fail|skipped|cancelled|todo)\s+(\d+)\s*\Z")


def parse_junit(data: bytes) -> Counts:
    """Counts from a JUnit XML report, checked against the totals the report itself states."""
    root = _safe_xml(data)
    counts = Counts()
    parents = {child: parent for parent in root.iter() for child in parent}
    for case in root.iter("testcase"):
        name = "::".join(filter(None, (case.get("classname"), case.get("name"))))
        counts.add(name or "?", _case_state(case))
    for suite in root.iter("testsuite"):
        if parents.get(suite) is not None and parents[suite].tag == "testsuite":
            continue
        cases = [_case_state(c) for c in suite.iter("testcase")]
        stated = {k: _int_attr(suite, k) for k in ("tests", "failures", "errors", "skipped")}
        found = {
            "tests": len(cases),
            "failures": cases.count("fail"),
            "errors": cases.count("error"),
            "skipped": cases.count("skip"),
        }
        for key, value in stated.items():
            if value is not None and value != found[key]:
                raise ReportError(
                    f"the report is inconsistent: suite {suite.get('name')!r} states {key}="
                    f"{value} but lists {found[key]}"
                )
    comments: dict[str, int] = {}
    for node in root:
        if node.tag is ET.Comment and (m := _COMMENT_COUNT.match(node.text or "")):
            comments[m.group(1)] = int(m.group(2))
    if comments:
        expected = {
            "tests": counts.total,
            "pass": counts.passed,
            "fail": counts.failed + counts.errors,
            "skipped": counts.skipped,
        }
        for key, value in expected.items():
            if key in comments and comments[key] != value:
                raise ReportError(
                    f"the report is inconsistent: it states {key} {comments[key]} but lists {value}"
                )
        if comments.get("cancelled"):
            counts.errors += comments["cancelled"]
    return counts


def _json_lines(data: bytes) -> Iterator[dict[str, object]]:
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield cast(dict[str, object], value)


def parse_go(data: bytes) -> Counts:
    """Counts from `go test -json`: the last terminal action of each test, subtests included."""
    final: dict[tuple[str, str], str] = {}
    started: set[tuple[str, str]] = set()
    packages_failed: list[str] = []
    events = 0
    for event in _json_lines(data):
        action, package, test = event.get("Action"), event.get("Package"), event.get("Test")
        if not isinstance(action, str) or not isinstance(package, str):
            continue
        events += 1
        if isinstance(test, str):
            if action == "run":
                started.add((package, test))
            elif action in ("pass", "fail", "skip"):
                final[(package, test)] = action
        elif action == "fail":
            packages_failed.append(package)
    if not events:
        raise ReportError("the report has no go test events")
    counts = Counts()
    for (package, test), action in final.items():
        state: Literal["pass", "fail", "skip"] = cast(Literal["pass", "fail", "skip"], action)
        counts.add(f"{package}.{test}", state)
    unfinished = sorted(started - final.keys())
    if unfinished and not packages_failed:
        raise ReportError(f"the report has no result for {unfinished[0][1]}")
    for package, test in unfinished:
        counts.add(f"{package}.{test}", "error")
    for package in packages_failed:
        if not any(p == package and a == "fail" for (p, _), a in final.items()):
            counts.add(package, "error")
    return counts


def parse_jest(data: bytes) -> Counts:
    """Counts from a jest or vitest JSON report, checked against the totals it states."""
    try:
        raw: object = json.loads(data)
    except ValueError as exc:
        raise ReportError(f"the report is not JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ReportError("the report is not a JSON object")
    report = cast(dict[str, object], raw)
    counts = Counts()
    suites = report.get("testResults")
    if not isinstance(suites, list):
        raise ReportError("the report has no testResults")
    for suite in cast(list[object], suites):
        if not isinstance(suite, dict):
            raise ReportError("the report has a malformed test file entry")
        entry = cast(dict[str, object], suite)
        results = entry.get("assertionResults")
        cases = cast(list[object], results) if isinstance(results, list) else []
        for case in cases:
            if not isinstance(case, dict):
                raise ReportError("the report has a malformed test entry")
            item = cast(dict[str, object], case)
            name = str(item.get("fullName") or item.get("title") or "?")
            status = item.get("status")
            state = {"passed": "pass", "failed": "fail"}.get(str(status), "skip")
            counts.add(name, cast(Literal["pass", "fail", "skip"], state))
        if entry.get("status") == "failed" and not any(
            isinstance(c, dict) and cast(dict[str, object], c).get("status") == "failed"
            for c in cases
        ):
            counts.add(str(entry.get("name") or "a test file"), "error")
    for key, value in (
        ("numTotalTests", counts.passed + counts.failed + counts.skipped),
        ("numPassedTests", counts.passed),
        ("numFailedTests", counts.failed),
    ):
        stated = report.get(key)
        if isinstance(stated, int) and stated != value:
            raise ReportError(f"the report is inconsistent: {key} is {stated} but it lists {value}")
    errors = report.get("numRuntimeErrorTestSuites")
    if isinstance(errors, int) and errors > counts.errors:
        counts.errors = errors
    return counts


def parse_cargo(data: bytes) -> Counts:
    """Counts from libtest's JSON events, checked against each suite's own totals."""
    counts = Counts()
    suite_totals = [0, 0, 0]
    suites = 0
    for event in _json_lines(data):
        kind, what = event.get("type"), event.get("event")
        if kind == "test" and what in ("ok", "failed", "ignored", "timeout"):
            name = str(event.get("name") or "?")
            state = {"ok": "pass", "ignored": "skip"}.get(str(what), "fail")
            counts.add(name, cast(Literal["pass", "fail", "skip"], state))
        elif kind == "suite" and what in ("ok", "failed"):
            suites += 1
            for i, key in enumerate(("passed", "failed", "ignored")):
                value = event.get(key)
                suite_totals[i] += value if isinstance(value, int) else 0
    if not suites:
        raise ReportError("the report has no libtest suite results")
    if suite_totals != [counts.passed, counts.failed, counts.skipped]:
        raise ReportError("the report is inconsistent: its suite totals differ from its tests")
    return counts


def parse_unittest(data: bytes) -> Counts:
    try:
        raw: object = json.loads(data)
    except ValueError as exc:
        raise ReportError(f"the report is not JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ReportError("the report is not a JSON object")
    report = cast(dict[str, object], raw)
    run, skipped, expected = (report.get(k) for k in ("tests_run", "skipped", "expected_failures"))
    lists = [report.get(k) for k in ("failures", "errors", "unexpected_successes")]
    if not all(isinstance(v, int) for v in (run, skipped, expected)) or not all(
        isinstance(v, list) for v in lists
    ):
        raise ReportError("the report is malformed")
    failures, errors, unexpected = (cast(list[object], v) for v in lists)
    counts = Counts()
    for name in [*failures, *unexpected]:
        counts.add(str(name), "fail")
    for name in errors:
        counts.add(str(name), "error")
    counts.skipped = cast(int, skipped) + cast(int, expected)
    counts.passed = cast(int, run) - counts.failed - counts.errors - counts.skipped
    if counts.passed < 0:
        raise ReportError("the report is inconsistent: more failures than tests run")
    return counts


_PARSERS: Final[dict[Runner, Callable[[bytes], Counts]]] = {
    "pytest": parse_junit,
    "node": parse_junit,
    "go": parse_go,
    "jest": parse_jest,
    "vitest": parse_jest,
    "cargo": parse_cargo,
    "unittest": parse_unittest,
}


def _real_home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _user_cache() -> Path:
    if sys.platform == "darwin":
        return _real_home() / "Library" / "Caches"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return Path(xdg) if xdg and Path(xdg).is_absolute() else _real_home() / ".cache"


def _tool_dirs(argv: list[str], runner: Runner) -> dict[str, str]:
    """Where the toolchain keeps installed dependencies and build caches. These are locations,
    not options: without them a fresh HOME would re-download every module."""
    home = _real_home()
    wanted: dict[str, Path] = {}
    names = {PurePosixPath(a).name for a in argv}
    if runner == "go":
        gopath = Path(os.environ.get("GOPATH") or home / "go")
        wanted |= {
            "GOPATH": gopath,
            "GOMODCACHE": gopath / "pkg" / "mod",
            "GOCACHE": _user_cache() / "go-build",
        }
    if runner == "cargo" or "cargo" in names:
        wanted |= {"CARGO_HOME": home / ".cargo", "RUSTUP_HOME": home / ".rustup"}
    if "uv" in names or "uvx" in names:
        wanted["UV_CACHE_DIR"] = home / ".cache" / "uv"
    return {name: os.environ.get(name) or str(default) for name, default in wanted.items()}


def judge_path() -> str:
    """The judge's own PATH without relative entries, which would resolve inside the copy."""
    entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    return os.pathsep.join(e for e in entries if e and Path(e).is_absolute())


def environment(spec: RerunSpec, runner: Runner, scratch: Path) -> dict[str, str]:
    """A minimal environment built from scratch: nothing the agent exported, no rc file, no
    runner option variable reaches the run unless the spec names it."""
    home = scratch / "home"
    tmp = scratch / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {"PATH": judge_path(), "HOME": str(home), "LANG": "C.UTF-8", "TMPDIR": str(tmp)}
    if sys.platform == "win32":
        env |= {k: v for k in ("SYSTEMROOT", "COMSPEC", "PATHEXT") if (v := os.environ.get(k))}
    env |= _tool_dirs(spec.command, runner)
    return env | dict(spec.env)


def _git(root: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes] | None:
    git = shutil.which("git", path=env["PATH"])
    if git is None:
        return None
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "-c", "core.fsmonitor=false", "-C", str(root), *args],
            env=env | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_files(root: Path, env: dict[str, str]) -> list[str] | None:
    """Tracked and untracked, not ignored, files under `root`, or None outside a git tree."""
    done = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", env=env)
    if done is None or done.returncode != 0:
        return None
    return sorted({p for p in done.stdout.decode("utf-8", "surrogateescape").split("\0") if p})


class CopyError(Exception):
    pass


@dataclass(slots=True)
class _Budget:
    files: int = 0
    size: int = 0

    def take(self, path: Path) -> None:
        self.files += 1
        with contextlib.suppress(OSError):
            self.size += path.lstat().st_size
        if self.files > MAX_FILES or self.size > MAX_BYTES:
            raise CopyError(
                f"the working tree is larger than the re-run copies ({MAX_FILES} files, "
                f"{MAX_BYTES // 1024**3} GiB)"
            )


def _copy_one(src: Path, dst: Path, budget: _Budget) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        budget.take(src)
        dst.symlink_to(src.readlink())
    elif src.is_file():
        budget.take(src)
        shutil.copy2(src, dst)
    elif src.is_dir():
        _copy_tree(src, dst, budget)


def _copy_tree(src: Path, dst: Path, budget: _Budget) -> None:
    for here, dirs, files in os.walk(src, followlinks=False):
        base = Path(here)
        target = dst / base.relative_to(src)
        target.mkdir(parents=True, exist_ok=True)
        keep: list[str] = []
        for name in sorted(dirs):
            if name in _SKIPPED_DIRS:
                continue
            if (base / name).is_symlink():
                _copy_one(base / name, target / name, budget)
            else:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            _copy_one(base / name, target / name, budget)


def copy_tree(root: Path, dest: Path, env: dict[str, str]) -> tuple[list[str], str]:
    """Copy the working tree into `dest`; return the copied paths and how they were chosen.
    In a git tree: every tracked file still present and every untracked file not ignored (so
    the agent's new files come along, caches and build output do not). Elsewhere: everything
    but VCS metadata, caches and dependency directories. Dependency directories (`.venv`,
    `venv`, `node_modules`) are linked, not copied."""
    budget = _Budget()
    listed = git_files(root, env)
    if listed is not None:
        how = "git ls-files (tracked and untracked, not ignored)"
        for rel in listed:
            src = root / rel
            if src.is_symlink() or src.exists():
                _copy_one(src, dest / rel, budget)
        copied = listed
    else:
        how = "the whole directory (not a git tree)"
        _copy_tree(root, dest, budget)
        copied = [p.relative_to(dest).as_posix() for p in dest.rglob("*") if not p.is_dir()]
    for name in _DEPENDENCY_DIRS:
        source = root / name
        if source.is_dir() and not (dest / name).exists() and not (dest / name).is_symlink():
            (dest / name).symlink_to(source, target_is_directory=True)
    return copied, how


_CONFIG_NAMES: Final = frozenset(
    {"pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg",
     "pyproject.toml", "conftest.py", "sitecustomize.py", "usercustomize.py", "package.json",
     ".npmrc", ".yarnrc", ".yarnrc.yml", "go.mod", "go.work", "Cargo.toml", ".mocharc.json",
     ".mocharc.yml", ".mocharc.yaml", ".mocharc.js", ".mocharc.cjs", "noxfile.py"}
)  # fmt: skip
_CONFIG_PREFIXES: Final = ("jest.config.", "vitest.config.", "vitest.workspace.", "vite.config.")


def is_runner_config(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.name in _CONFIG_NAMES or pure.name.startswith(_CONFIG_PREFIXES):
        return True
    return pure.parent.name == ".cargo" and pure.name in ("config", "config.toml")


def _section(path: str, data: bytes | None) -> tuple[str, object]:
    """The part of a configuration file that configures the tests, and its label."""
    if data is None:
        return "", None
    name = PurePosixPath(path).name
    try:
        if name == "pyproject.toml":
            tool: object = tomllib.loads(data.decode("utf-8")).get("tool")
            settings = (
                cast(dict[str, object], tool).get("pytest") if isinstance(tool, dict) else None
            )
            return " [tool.pytest]", settings
        if name == "setup.cfg":
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(data.decode("utf-8"))
            section = parser["tool:pytest"] if parser.has_section("tool:pytest") else None
            return " [tool:pytest]", dict(section) if section is not None else None
        if name == "package.json":
            loaded: object = json.loads(data)
            if isinstance(loaded, dict):
                keys = ("scripts", "jest", "mocha", "ava", "type")
                package = cast(dict[str, object], loaded)
                return " (scripts and test settings)", {k: package.get(k) for k in keys}
    except (ValueError, UnicodeDecodeError, configparser.Error):
        pass
    return "", data


def config_changes(
    root: Path, copy: Path, copied: list[str], base: str, env: dict[str, str]
) -> tuple[list[str], str | None]:
    """Runner configuration that differs between the copy and the base revision, and the
    revision compared with (None when there is none to compare with)."""
    resolved = _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", env=env)
    if resolved is None or resolved.returncode != 0:
        return [], None
    sha = resolved.stdout.decode().strip()
    listing = _git(root, "ls-tree", "-r", "--name-only", sha, "--", ".", env=env)
    if listing is None or listing.returncode != 0:
        return [], None
    before = {
        p
        for p in listing.stdout.decode("utf-8", "surrogateescape").splitlines()
        if is_runner_config(p)
    }
    now = {p for p in copied if is_runner_config(p) and (copy / p).is_file()}
    changes: list[str] = []
    for path in sorted(before | now):
        old: bytes | None = None
        if path in before:
            shown = _git(root, "show", f"{sha}:./{path}", env=env)
            old = shown.stdout if shown is not None and shown.returncode == 0 else None
        new = (copy / path).read_bytes() if path in now else None
        label, old_part = _section(path, old)
        new_label, new_part = _section(path, new)
        if old_part == new_part:
            continue
        if old is None:
            changes.append(f"{path} was added")
        elif new is None:
            changes.append(f"{path} was deleted")
        else:
            changes.append(f"{path}{label or new_label} changed")
    return changes, sha


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    if hasattr(os, "killpg"):
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _resolve_program(argv: list[str], copy: Path, path: str) -> list[str] | None:
    program = argv[0]
    if "/" in program or os.sep in program:
        candidate = Path(program) if Path(program).is_absolute() else copy / program
        return [str(candidate), *argv[1:]] if candidate.exists() else None
    found = shutil.which(program, path=path)
    return [found, *argv[1:]] if found else None


def _same_dir(a: str | None, b: Path) -> bool:
    if a is None:
        return True
    try:
        return Path(a).resolve() == b
    except OSError:
        return False


def execute(
    spec: RerunSpec, root: Path, *, base: str | None = None, session_cwd: str | None = None
) -> RerunResult:
    """Copy `root`, run the spec's command on the copy and read the runner's report. Runs once;
    never raises for a problem with the project, the command or the report."""
    root = root.resolve()
    runner = spec.runner or infer_runner(spec.command)
    started = time.monotonic()
    common: dict[str, object] = {
        "runner": runner,
        "command": list(spec.command),
        "workdir": str(root),
    }
    problems: list[str] = []
    if not _same_dir(session_cwd, root):
        problems.append(
            f"the session ran in {session_cwd}, but the re-run copied {root}; the suite that "
            "passed may not be the one the agent changed"
        )

    def done(status: RerunStatus, reason: str, **extra: object) -> RerunResult:
        values: dict[str, object] = {
            **common,
            "status": status,
            "reason": reason,
            "duration_s": round(time.monotonic() - started, 3),
            "problems": problems,
            **extra,
        }
        return RerunResult.model_validate(values)

    if not root.is_dir():
        return done(RerunStatus.UNKNOWN, f"the re-run could not start: {root} is not a directory")
    if runner is None:
        return done(
            RerunStatus.UNKNOWN,
            "the re-run needs a runner that writes a machine-readable report; set rerun.runner "
            "(pytest, go, jest, vitest, cargo, node or unittest)",
        )
    with tempfile.TemporaryDirectory(prefix="evj-rerun-") as tmp:
        scratch = Path(tmp)
        workspace = scratch / "judge"
        copy = scratch / "tree"
        workspace.mkdir()
        copy.mkdir()
        env = environment(spec, runner, scratch)
        try:
            copied, how = copy_tree(root, copy, env)
        except (CopyError, OSError) as exc:
            return done(RerunStatus.UNKNOWN, f"the re-run could not copy the working tree: {exc}")
        notes = [f"copied {len(copied)} files from {root} by {how}"]
        changes, sha = config_changes(root, copy, copied, base or "HEAD", env)
        if sha is None:
            notes.append(
                f"runner configuration was not compared: {base or 'HEAD'} is not a revision of "
                f"a git tree at {root}"
            )
        try:
            plan = _plan(spec.command, runner, workspace)
        except ReportError as exc:
            return done(RerunStatus.UNKNOWN, f"the re-run could not ask for a report: {exc}")
        argv = _resolve_program(plan.argv, copy, env["PATH"])
        if argv is None:
            return done(
                RerunStatus.UNKNOWN,
                f"the re-run could not find {spec.command[0]!r} on the judge's PATH",
                config_changes=changes,
                base=sha,
                notes=notes,
            )
        extra: dict[str, object] = {"config_changes": changes, "base": sha, "notes": notes}
        stdout = workspace / "stdout"
        report = stdout if plan.from_stdout else plan.report
        launched = time.time()
        with stdout.open("wb") as out, (workspace / "stderr").open("wb") as err:
            try:
                proc = subprocess.Popen(  # noqa: S603 - the spec's own argv, no shell
                    argv,
                    cwd=copy,
                    env=env | plan.env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            except OSError as exc:
                return done(RerunStatus.UNKNOWN, f"the re-run could not start: {exc}", **extra)
            try:
                code = proc.wait(timeout=spec.timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                proc.wait()
                return done(
                    RerunStatus.UNKNOWN,
                    f"the re-run timed out after {spec.timeout:g}s",
                    **extra,
                )
            _kill_group(proc)
        extra["exit_code"] = code
        return _decide(spec, runner, report, launched, code, done, extra)


def _decide(
    spec: RerunSpec,
    runner: Runner,
    report: Path,
    launched: float,
    code: int,
    done: Callable[..., RerunResult],
    extra: dict[str, object],
) -> RerunResult:
    try:
        info = report.stat()
    except OSError:
        return done(
            RerunStatus.UNKNOWN,
            f"the re-run exited {code} without writing its {runner} report",
            **extra,
        )
    if info.st_mtime < launched - _CLOCK_SLACK_S:
        return done(RerunStatus.UNKNOWN, "the re-run's report predates the run", **extra)
    if info.st_size > MAX_REPORT_BYTES:
        return done(RerunStatus.UNKNOWN, "the re-run's report is too large to read", **extra)
    try:
        counts = _PARSERS[runner](report.read_bytes())
    except (ReportError, OSError) as exc:
        return done(
            RerunStatus.UNKNOWN, f"the re-run's {runner} report is unusable: {exc}", **extra
        )
    extra |= {
        "passed": counts.passed,
        "failed": counts.failed,
        "errors": counts.errors,
        "skipped": counts.skipped,
        "failing": counts.failing,
        "passing": counts.passing,
        "report_read": True,
    }
    summary = f"{runner}: " + ", ".join(
        f"{n} {label}"
        for n, label in (
            (counts.passed, "passed"),
            (counts.failed, "failed"),
            (counts.errors, "errors"),
            (counts.skipped, "skipped"),
        )
        if n or label == "passed"
    )
    if counts.failed or counts.errors:
        named = f" ({', '.join(counts.failing[:3])})" if counts.failing else ""
        return done(
            RerunStatus.FAIL, f"the judge re-ran the suite: {summary}{named}, exit {code}", **extra
        )
    if code != 0:
        return done(
            RerunStatus.UNKNOWN,
            f"the re-run exited {code} although its report shows {summary}",
            **extra,
        )
    if counts.passed == 0:
        return done(RerunStatus.UNKNOWN, f"the re-run ran no passing tests ({summary})", **extra)
    result = done(RerunStatus.PASS, f"the judge re-ran the suite: {summary}, exit 0", **extra)
    return result.at_least(spec.min_tests, "the spec's min_tests")
