"""Running the suite: a clean environment, its own process group and a deadline."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from evidence_judge.sessions import fingerprint
from evidence_judge.sessions.rerun.config import config_changes, configuration_above
from evidence_judge.sessions.rerun.copy import CopyError, copy_tree, git, resolve_base
from evidence_judge.sessions.rerun.reports import PARSERS, ReportError
from evidence_judge.sessions.rerun.spec import (
    RerunResult,
    RerunSpec,
    RerunStatus,
    Runner,
    infer_runner,
)

MAX_REPORT_BYTES: Final = 64 * 1024 * 1024


_CLOCK_SLACK_S: Final = 2.0


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
    runner option variable reaches the run unless the spec names it. Python keeps its bytecode
    under `scratch`, so no `__pycache__` file planted in the linked virtualenv is loaded."""
    home = scratch / "home"
    tmp = scratch / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = {
        "PATH": judge_path(),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": str(tmp),
        "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
    }
    if sys.platform == "win32":
        env |= {k: v for k in ("SYSTEMROOT", "COMSPEC", "PATHEXT") if (v := os.environ.get(k))}
    env |= _tool_dirs(spec.command, runner)
    return env | dict(spec.env)


def head_revision(root: Path) -> str | None:
    """The commit `root`'s git tree is at, or None."""
    env = {"PATH": judge_path(), "HOME": str(_real_home()), "LANG": "C.UTF-8"}
    done = git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", env=env)
    if done is None or done.returncode != 0:
        return None
    return done.stdout.decode("ascii", "replace").strip() or None


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


def _command(
    spec: RerunSpec, argv: list[str], copy: Path, env: dict[str, str], places: dict[str, Path]
) -> list[str] | None:
    """The argv to start: the command, or the spec's sandbox prefix with its placeholders filled
    in followed by the command, which the sandbox then finds on its own."""
    if spec.sandbox is None:
        return _resolve_program(argv, copy, env["PATH"])
    prefix = list(spec.sandbox)
    for name, place in places.items():
        prefix = [part.replace(f"{{{name}}}", str(place)) for part in prefix]
    resolved = _resolve_program(prefix, copy, env["PATH"])
    return None if resolved is None else [*resolved, *argv]


def _same_dir(a: str | None, b: Path) -> bool:
    if a is None:
        return True
    try:
        return Path(a).resolve() == b
    except OSError:
        return False


def _environment_changes(root: Path, baseline: Path | None) -> list[str]:
    if baseline is None:
        return []
    saved = fingerprint.load(baseline)
    if saved is None:
        return ["the environment fingerprint from the start of the turn is missing or unreadable"]
    return fingerprint.changes(saved, fingerprint.fingerprint(root))


def execute(
    spec: RerunSpec,
    root: Path,
    *,
    base: str | None = None,
    session_cwd: str | None = None,
    baseline: Path | None = None,
) -> RerunResult:
    """Copy `root`, run the spec's command on the copy and read the runner's report. Runs once;
    never raises for a problem with the project, the command or the report. `baseline` is the
    fingerprint of the dependency directories the turn started with (see `fingerprint`)."""
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
    with (
        tempfile.TemporaryDirectory(prefix="evj-rerun-") as tmp,
        tempfile.TemporaryDirectory() as reports,
    ):
        scratch = Path(tmp).resolve()
        workspace = Path(reports).resolve()
        copy = scratch / "tree"
        copy.mkdir()
        env = environment(spec, runner, scratch)
        resolved = resolve_base(root, base or "HEAD", env)
        try:
            copied = copy_tree(root, copy, env, resolved, spec.include)
        except (CopyError, OSError) as exc:
            return done(RerunStatus.UNKNOWN, f"the re-run could not copy the working tree: {exc}")
        problems.extend(copied.problems)
        summary = f"copied {len(copied.paths)} files from {root} by {copied.how}"
        notes = list(copied.notes)
        changes = list(copied.config_changes)
        if resolved is None:
            notes.append(
                f"runner configuration was not compared: {base or 'HEAD'} is not a revision of "
                f"a git tree at {root}"
            )
        else:
            changes = [*config_changes(root, copy, copied.paths, resolved, env), *changes]
        sha = resolved.sha if resolved is not None else None
        try:
            plan = _plan(spec.command, runner, workspace)
        except ReportError as exc:
            return done(RerunStatus.UNKNOWN, f"the re-run could not ask for a report: {exc}")
        places = {"tree": copy, "report_dir": workspace, "home": Path(env["HOME"]),
                  "tmp": Path(env["TMPDIR"])}  # fmt: skip
        argv = _command(spec, plan.argv, copy, env, places)
        if argv is None:
            program = (spec.sandbox or spec.command)[0]
            return done(
                RerunStatus.UNKNOWN,
                f"the re-run could not find {program!r} on the judge's PATH",
                config_changes=changes,
                base=sha,
                notes=notes,
                copied=summary,
            )
        extra: dict[str, object] = {
            "config_changes": changes,
            "base": sha,
            "notes": notes,
            "copied": summary,
        }
        found = configuration_above(copy, runner, env)
        drift = _environment_changes(root, baseline)
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
        found.extend(p for p in configuration_above(copy, runner, env) if p not in found)
        drift.extend(d for d in _environment_changes(root, baseline) if d not in drift)
        if found:
            problems.append(
                f"{', '.join(found[:3])} sits above the re-run's copy, where {runner} reads it as "
                "the project's configuration"
            )
        if drift:
            problems.append(
                f"the environment changed during the session ({'; '.join(drift)}); the re-run "
                "used the changed environment"
            )
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
        counts = PARSERS[runner](report.read_bytes())
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
