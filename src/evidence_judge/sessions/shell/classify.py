"""Classifying simple commands and analysing whole command lines."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from evidence_judge.sessions.models import Category, CommandAnalysis
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.env import (
    GO_REGEN,
    command_env,
    exports,
    harmless_env,
    harness_change,
    is_harness_env,
    off,
    pytest_regenerates,
    regenerates_env,
    run_env_allowed,
)
from evidence_judge.sessions.shell.guard_paths import (
    AGENT_CONFIG,
    JUDGE_NAME,
    SESSION_STORE,
    guard_tamper,
    writes_session_store,
)
from evidence_judge.sessions.shell.lexer import (
    MAX_NESTING,
    PYTHON,
    SHELLS,
    Pipeline,
    Simple,
    Wrapped,
    parse,
    program_name,
    strip_comments,
    tokenize,
    unspelled,
    unwrap,
)
from evidence_judge.sessions.shell.runners.cargo import cargo_filters
from evidence_judge.sessions.shell.runners.generic import generic_filters
from evidence_judge.sessions.shell.runners.go import go_filters
from evidence_judge.sessions.shell.runners.js import js_filters
from evidence_judge.sessions.shell.runners.make import make_like
from evidence_judge.sessions.shell.runners.node import node_test_filters
from evidence_judge.sessions.shell.runners.pm import FIX_FLAGS, pm_script
from evidence_judge.sessions.shell.runners.pytest import pytest_filters
from evidence_judge.sessions.shell.runners.tox import tox_filters
from evidence_judge.sessions.shell.runners.unittest import unittest_filters
from evidence_judge.sessions.shell.writes import (
    PM,
    SCRATCH,
    git,
    maybe_directory,
    perl,
    run_writes,
    sed,
    tool_config_change,
    written_code,
)

MAX_COMMAND_CHARS: Final = 100_000


_CHECKS: Final = frozenset({Category.BUILD, Category.LINT, Category.TYPECHECK, Category.FORMAT})


_AGENT_SUMMARY_TEXT: Final = re.compile(
    r"\b\d+\s+(?:passed|passing|failed|failing|failures?|errors?)\b|"
    r"\btest result:\s*(?:ok|FAILED)|\bTests?:?\s+\d+|\bOK\s*\(\d+\s+tests?|"
    r"\bRan\s+\d+\s+tests?|\bBUILD\s+SUCCESS|\ball\s+tests?\s+pass",
    re.IGNORECASE,
)

READ_ONLY_PROGRAMS: Final = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "wc",
        "echo",
        "printf",
        "pwd",
        "which",
        "type",
        "file",
        "stat",
        "diff",
        "cmp",
        "tree",
        "sort",
        "uniq",
        "cut",
        "tr",
        "jq",
        "yq",
        "du",
        "df",
        "date",
        "true",
        "false",
        "test",
        "[",
        "[[",
        "sleep",
        "cd",
        "pushd",
        "popd",
        "export",
        "set",
        "unset",
        "alias",
        ":",
        "exit",
        "return",
        "nl",
        "column",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "whoami",
        "id",
        "uname",
        "hostname",
        "ps",
        "xxd",
        "od",
        "hexdump",
        "md5",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "shasum",
        "env",
        "printenv",
        "fd",
        "locate",
        "comm",
        "paste",
        "fold",
        "fmt",
        "rev",
        "tac",
        "seq",
        "yes",
        "expr",
        "bc",
        "history",
        "lsof",
        "top",
        "free",
        "uptime",
        "nproc",
        "tput",
        "clear",
        "wait",
        "jobs",
        "read",
        "shopt",
        "ulimit",
        "umask",
        "trap",
        "command",
        "hash",
        "declare",
        "local",
        "typeset",
        "getconf",
        "sw_vers",
        "lscpu",
        "pgrep",
        "kill",
        "pkill",
        "open",
        "awk",
        "gawk",
        "sed",
        "find",
        "curl",
        "git",
        "pip",
        "pip3",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "cargo",
        "go",
        "uv",
    }
)
"""Programs that do not change files (the last group only for some subcommands, decided below)."""


_MUTATING: Final = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "mkdir",
        "touch",
        "ln",
        "chmod",
        "chown",
        "chgrp",
        "truncate",
        "dd",
        "tee",
        "patch",
        "install",
        "unzip",
        "tar",
        "gunzip",
        "gzip",
        "bunzip2",
        "zip",
        "rsync",
        "scp",
        "wget",
        "shred",
        "unlink",
        "brew",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "apk",
        "conda",
        "mamba",
        "pipx",
        "gem",
        "composer",
        "sync",
        "mkfifo",
        "split",
    }
)


_LINTERS: Final = frozenset(
    {
        "flake8",
        "pylint",
        "eslint",
        "golangci-lint",
        "rubocop",
        "shellcheck",
        "stylelint",
        "markdownlint",
        "markdownlint-cli2",
        "yamllint",
        "hadolint",
        "swiftlint",
        "ktlint",
        "standard",
        "xo",
        "oxlint",
        "pyflakes",
        "pycodestyle",
        "pydocstyle",
        "bandit",
        "vulture",
        "staticcheck",
        "revive",
        "tflint",
        "actionlint",
        "codespell",
        "cspell",
        "djlint",
    }
)


_TYPECHECKERS: Final = frozenset(
    {"pyright", "basedpyright", "mypy", "pyre", "pytype", "vue-tsc", "flow", "ty"}
)


_BUILDERS: Final = frozenset(
    {
        "webpack",
        "rollup",
        "esbuild",
        "parcel",
        "ninja",
        "msbuild",
        "javac",
        "gcc",
        "g++",
        "clang",
        "clang++",
        "rustc",
        "tsup",
        "swc",
        "turbo",
        "nx",
    }
)


_OPAQUE_WRITERS: Final = frozenset(
    {"tar", "unzip", "patch", "dd", "rsync", "wget", "split", "bunzip2", "gunzip", "7z", "unrar"}
)
"""File-changing programs whose targets are not (all) the words on their command line."""


_DETACHERS: Final = frozenset(
    {"setsid", "disown", "at", "batch", "screen", "tmux", "daemonize", "start-stop-daemon",
     "systemd-run", "launchctl", "crontab", "watchexec", "entr", "fswatch", "inotifywait"}
)  # fmt: skip
"""Programs that leave something running, or scheduled, after the command returns."""


_COPIERS: Final = frozenset({"cp", "mv", "install", "ln", "scp"})


_VERSION_FLAGS: Final = frozenset({"--version", "-V", "-VV", "--help", "-h"})


_PYTHON_SIDE_EFFECTS: Final = re.compile(
    r"import|open|exec|eval|compile|__|getattr|setattr|delattr|globals|locals|vars|"
    r"breakpoint|help|input|memoryview"
)
"""Without these a `python -c` program reaches no module, file or builtin that can write."""


@dataclass(frozen=True, slots=True)
class _Call:
    """One simple command, unwrapped, as the per-program handlers see it."""

    words: list[str]
    prog: str
    args: list[str]
    env: dict[str, str]
    depth: int
    cwd: str | None

    @property
    def sub(self) -> str:
        return self.args[0] if self.args else ""


_Handler = Callable[[_Call], Classified | None]
"""Classifies a command of the programs it is registered for, or None to leave it to the
read-only, version-flag and inline-Python fallbacks."""


def _shell(c: _Call) -> Classified | None:
    if c.depth >= MAX_NESTING:
        return None
    args = c.args
    script = next(
        (args[i + 1] for i, a in enumerate(args[:-1]) if re.fullmatch(r"-[a-z]*c[a-z]*", a)),
        None,
    )
    if script is None:
        return Classified(Category.UNKNOWN, c.prog, mutation=f"{c.prog} runs a script")
    nested = _analyse(script, c.cwd, c.depth + 1)
    if nested.runners:
        category = Category.TEST
    elif nested.mutation is not None:
        category = Category.MUTATING
    else:
        category = nested.categories[-1] if nested.categories else Category.READ_ONLY
    return Classified(category, c.prog, nested=nested)


def _pytest(c: _Call) -> Classified:
    filters, collect, paths = pytest_filters(c.args, c.env)
    regen = next((a for a in c.args if pytest_regenerates(a)), None)
    written, emptied, after = run_writes("pytest", c.args, c.cwd)
    why = f"pytest {regen} rewrites expected outputs" if regen else None
    if written or emptied:
        option, path = (written or emptied)[0]
        why = why or f"pytest {option} writes {path}"
    return Classified(
        Category.TEST,
        c.prog,
        "pytest",
        filters,
        collect,
        paths=paths,
        mutation=why,
        opaque=f"pytest {regen} rewrites expected outputs" if regen else None,
        touched=[path for _, path in written],
        deleted=[path for _, path in emptied],
        writes_after=after,
    )


def _unittest(c: _Call) -> Classified:
    filters, collect = unittest_filters(c.args)
    return Classified(Category.TEST, c.prog, "unittest", filters, collect)


def _js_runner(c: _Call) -> Classified:
    prog, args = c.prog, c.args
    filters, collect = js_filters(prog, args)
    if prog == "vitest" and args[:1] in (["bench"], ["list"]):
        collect = True
    mutation = (
        "updates snapshots"
        if any(a in ("-u", "--updateSnapshot", "--update") for a in args)
        else None
    )
    return Classified(
        Category.TEST,
        prog,
        prog,
        filters,
        collect,
        mutation=mutation,
        opaque=f"{prog} -u rewrites the expected snapshots" if mutation else None,
    )


def _browser_runner(c: _Call) -> Classified | None:
    sub = {"playwright": "test", "cypress": "run"}[c.prog]
    if c.args[:1] != [sub]:
        return None
    filters, collect = js_filters(c.prog, c.args[1:])
    return Classified(Category.TEST, c.prog, f"{c.prog} {sub}", filters, collect)


_SELECTING_OPTIONS: Final = frozenset(
    {"-e", "--example", "-t", "--tag", "--filter", "--group", "--testsuite", "-R", "-L", "-k",
     "--only-failures"}
)  # fmt: skip


def _plain_runner(c: _Call) -> Classified:
    filters = [f"only {a}" for a in c.args if not a.startswith("-")]
    filters.extend(a.partition("=")[0] for a in c.args if a.partition("=")[0] in _SELECTING_OPTIONS)
    return Classified(Category.TEST, c.prog, c.prog, filters)


def _node(c: _Call) -> Classified | None:
    if "--test" not in c.args:
        return None
    filters, update = node_test_filters(c.args)
    why = "node --test --test-update-snapshots rewrites the expected snapshots"
    return Classified(
        Category.TEST,
        c.prog,
        "node --test",
        filters,
        mutation=why if update else None,
        opaque=why if update else None,
    )


def _tox(c: _Call) -> Classified:
    return Classified(Category.TEST, c.prog, c.prog, tox_filters(c.prog, c.args))


def _go_test(c: _Call) -> Classified:
    args = c.args[1:]
    filters, collect = go_filters(args)
    regen = next((a for a in args if GO_REGEN.fullmatch(a) and not off(a)), None)
    why = f"go test {regen} rewrites expected outputs" if regen else None
    written, _, after = run_writes("go test", args, c.cwd)
    wrote = f"go test {written[0][0]} writes {written[0][1]}" if written else None
    return Classified(
        Category.TEST,
        c.prog,
        "go test",
        filters,
        collect,
        mutation=why or wrote,
        opaque=why,
        touched=[path for _, path in written],
        writes_after=after,
    )


def _go(c: _Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub == "test":
        return _go_test(c)
    if sub in ("build", "install"):
        return Classified(Category.BUILD, prog)
    if sub == "vet":
        return Classified(Category.LINT, prog)
    if sub in ("fmt", "generate", "get") or args[:2] == ["mod", "tidy"]:
        return Classified(Category.MUTATING, prog, mutation=f"go {sub} changes files")
    if why := tool_config_change(c.words):
        return Classified(Category.MUTATING, prog, mutation=why, touched=[])
    if sub in ("version", "env", "list", "doc", "help") or args[:2] == ["mod", "graph"]:
        return Classified(Category.READ_ONLY, prog)
    return Classified(Category.UNKNOWN, prog, mutation=f"go {sub} runs a program")


def _cargo_insta(c: _Call) -> Classified | None:
    args = c.args
    action = args[1] if len(args) > 1 else ""
    if action in ("accept", "review", "reject") or (
        action == "test" and any(a in ("--accept", "--review") for a in args)
    ):
        why = f"cargo insta {action} rewrites expected outputs"
        return Classified(Category.MUTATING, c.prog, mutation=why, opaque=why)
    if action == "test":
        filters, collect = cargo_filters(args[2:])
        return Classified(Category.TEST, c.prog, "cargo insta test", filters, collect)
    return None


def _cargo(c: _Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub in ("test", "t"):
        filters, collect = cargo_filters(args[1:])
        return Classified(Category.TEST, prog, "cargo test", filters, collect)
    if sub == "insta" and (insta := _cargo_insta(c)) is not None:
        return insta
    if sub == "nextest":
        filters, collect = cargo_filters(args[1:])
        return Classified(Category.TEST, prog, "cargo nextest", filters, collect)
    if sub in ("build", "b"):
        return Classified(Category.BUILD, prog)
    if sub in ("check", "c"):
        return Classified(Category.TYPECHECK, prog)
    if sub == "clippy":
        fix = "--fix" in args
        return Classified(
            Category.LINT, prog, mutation="clippy --fix rewrites files" if fix else None
        )
    if sub == "fmt":
        check = "--check" in args
        return Classified(
            Category.FORMAT, prog, mutation=None if check else "cargo fmt rewrites files"
        )
    if sub in ("tree", "metadata", "search", "version", "--version", "help", "doc"):
        return Classified(Category.READ_ONLY, prog)
    return Classified(Category.UNKNOWN, prog, mutation=f"cargo {sub} may change files")


def _jvm(c: _Call) -> Classified:
    args = c.args
    goals = [a for a in args if not a.startswith("-")]
    filters = [
        a.partition("=")[0]
        for a in args
        if a.startswith(("-Dtest", "-pl", "--tests", "--projects"))
    ]
    if any(g in ("test", "verify", "check", "integration-test") for g in goals):
        return Classified(Category.TEST, c.prog, c.prog, filters)
    if any(g in ("build", "package", "compile", "install", "assemble") for g in goals):
        return Classified(Category.BUILD, c.prog)
    return Classified(Category.UNKNOWN, c.prog, mutation=f"{c.prog} runs a project task")


def _dotnet(c: _Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub == "test":
        filters = [a for a in args[1:] if a in ("--filter",)] + [
            f"only {a}" for a in args[1:2] if not a.startswith("-")
        ]
        return Classified(Category.TEST, prog, "dotnet test", filters)
    if sub == "build":
        return Classified(Category.BUILD, prog)
    if sub == "format":
        return Classified(
            Category.FORMAT,
            prog,
            mutation=None if "--verify-no-changes" in args else "dotnet format rewrites files",
        )
    return Classified(Category.UNKNOWN, prog, mutation=f"dotnet {sub} may change files")


def _toolchain(c: _Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub == "test":
        filters = [f"only {a}" for a in args[1:] if not a.startswith("-")]
        filters.extend(
            a for a in args[1:] if a.partition("=")[0] in ("--filter", "--only", "--exclude")
        )
        return Classified(Category.TEST, prog, f"{prog} test", filters)
    if sub in ("build", "compile"):
        return Classified(Category.BUILD, prog)
    if sub == "lint":
        return Classified(Category.LINT, prog)
    if sub == "check":
        return Classified(Category.TYPECHECK, prog)
    if sub in ("fmt", "format"):
        return Classified(
            Category.FORMAT,
            prog,
            mutation=None if "--check" in args else f"{prog} {sub} rewrites files",
        )
    return Classified(Category.UNKNOWN, prog, mutation=f"{prog} {sub} may change files")


def _pm(c: _Call) -> Classified:
    return pm_script(c.prog, c.words)


def _make(c: _Call) -> Classified:
    return make_like(c.prog, c.words)


def _hatch(c: _Call) -> Classified | None:
    if c.args[:1] == ["test"]:
        return Classified(
            Category.TEST, c.prog, "hatch test", generic_filters("hatch test", c.args[1:])
        )
    return _package_build(c)


def _package_build(c: _Call) -> Classified | None:
    return Classified(Category.BUILD, c.prog) if c.args[:1] == ["build"] else None


def _uv(c: _Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub == "build":
        return Classified(Category.BUILD, prog)
    if sub in ("sync", "add", "remove", "lock", "venv", "init", "self", "tool") or args[:2] in (
        ["pip", "install"],
        ["pip", "uninstall"],
        ["pip", "sync"],
    ):
        return Classified(Category.MUTATING, prog, mutation=f"uv {sub} changes the environment")
    if sub in ("pip", "tree", "version", "--version", "help", "python"):
        return Classified(Category.READ_ONLY, prog)
    return Classified(Category.UNKNOWN, prog, mutation=f"uv {sub} may change files")


def _pip(c: _Call) -> Classified:
    if c.sub in ("install", "uninstall", "download", "wheel"):
        return Classified(
            Category.MUTATING, c.prog, mutation=f"pip {c.sub} changes the environment"
        )
    return Classified(Category.READ_ONLY, c.prog)


def _ruff(c: _Call) -> Classified:
    args = c.args
    if c.sub == "format":
        check = any(a in ("--check", "--diff") for a in args)
        return Classified(
            Category.FORMAT, c.prog, mutation=None if check else "ruff format rewrites files"
        )
    fix = any(
        a.partition("=")[0] in ("--fix", "--unsafe-fixes", "--fix-only")
        and not a.endswith("=false")
        for a in args
    )
    return Classified(Category.LINT, c.prog, mutation="ruff --fix rewrites files" if fix else None)


def _python_formatter(c: _Call) -> Classified:
    prog, args = c.prog, c.args
    check = any(a in ("--check", "--diff", "--check-only", "-c") for a in args)
    if prog in ("autopep8", "yapf", "autoflake"):
        check = not any(a in ("-i", "--in-place") for a in args)
    return Classified(Category.FORMAT, prog, mutation=None if check else f"{prog} rewrites files")


def _web_formatter(c: _Call) -> Classified:
    prog, args = c.prog, c.args
    writes = any(
        a in ("--write", "-w", "--apply", "--apply-unsafe", "--fix") for a in args
    ) or args[:1] == ["fmt"]
    category = (
        Category.LINT
        if prog == "biome" and args[:1] in (["lint"], ["check"], ["ci"])
        else Category.FORMAT
    )
    return Classified(category, prog, mutation=f"{prog} rewrites files" if writes else None)


def _native_formatter(c: _Call) -> Classified:
    prog, args = c.prog, c.args
    writes = any(a in ("-w", "-i", "--write") for a in args) or (
        prog == "rustfmt" and "--check" not in args
    )
    if prog == "terraform":
        writes = args[:1] == ["fmt"] and "-check" not in args
    return Classified(Category.FORMAT, prog, mutation=f"{prog} rewrites files" if writes else None)


def _pre_commit(c: _Call) -> Classified:
    return Classified(Category.LINT, c.prog, mutation="pre-commit hooks may rewrite files")


def _linter(c: _Call) -> Classified:
    fix = any(a.partition("=")[0] in FIX_FLAGS for a in c.args)
    return Classified(
        Category.LINT, c.prog, mutation=f"{c.prog} fixes rewrite files" if fix else None
    )


def _typechecker(c: _Call) -> Classified:
    return Classified(Category.TYPECHECK, c.prog)


def _tsc(c: _Call) -> Classified:
    no_emit = "--noEmit" in c.args or "--noemit" in c.args
    return Classified(Category.TYPECHECK if no_emit else Category.BUILD, c.prog)


def _builder(c: _Call) -> Classified:
    return Classified(Category.BUILD, c.prog)


def _docker(c: _Call) -> Classified | None:
    return _package_build(c)


def _cmake(c: _Call) -> Classified | None:
    return Classified(Category.BUILD, c.prog) if "--build" in c.args else None


def _git(c: _Call) -> Classified:
    return git(c.words)


def _sed(c: _Call) -> Classified:
    return sed(c.args)


def _perl(c: _Call) -> Classified:
    return perl(c.args)


def _awk(c: _Call) -> Classified:
    program_text = " ".join(c.args)
    writes = "system(" in program_text or re.search(r"print[^|;]*>\s*\"", program_text) is not None
    return Classified(
        Category.MUTATING if writes else Category.READ_ONLY,
        c.prog,
        mutation="awk writes files or runs commands" if writes else None,
        touched=[],
        opaque="awk writes files or runs commands" if writes else None,
    )


_FIND_ACTIONS: Final = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
)


def _find(c: _Call) -> Classified:
    acting = [a for a in c.args if a in _FIND_ACTIONS]
    return Classified(
        Category.MUTATING if acting else Category.READ_ONLY,
        c.prog,
        mutation=f"find {acting[0]} acts on files" if acting else None,
        touched=[],
        opaque=f"find {acting[0]} acts on the files it finds" if acting else None,
    )


def _curl(c: _Call) -> Classified:
    args = c.args
    named = [
        args[i + 1] if a in ("-o", "--output") else a.partition("=")[2]
        for i, a in enumerate(args)
        if (a in ("-o", "--output") and i + 1 < len(args)) or a.startswith("--output=")
    ]
    remote = any(a in ("-O", "--remote-name", "--remote-name-all", "-J") for a in args)
    writes = bool(named) or remote
    return Classified(
        Category.MUTATING if writes else Category.READ_ONLY,
        c.prog,
        mutation="curl writes a file" if writes else None,
        touched=named,
        opaque="curl -O names the file after the URL" if remote else None,
    )


def _copier(c: _Call) -> Classified:
    prog, args = c.prog, c.args
    named = [a for a in args if not a.startswith("-")]
    target = named[-1:] or [""]
    into_directory = (maybe_directory(target[0]) and not target[0].startswith(SCRATCH)) or any(
        a in ("-t", "--target-directory") or a.startswith("-t") for a in args
    )
    return Classified(
        Category.MUTATING,
        prog,
        mutation="mv moves files" if prog == "mv" else f"{prog} changes files",
        deleted=named[:-1] if prog == "mv" else [],
        touched=named if prog == "mv" else target,
        opaque=f"{prog} writes into a directory" if into_directory else None,
    )


def _file_writer(c: _Call) -> Classified:
    prog = c.prog
    if prog in ("rm", "unlink", "shred"):
        deleted = [a for a in c.args if not a.startswith("-")]
        return Classified(
            Category.MUTATING, prog, mutation=f"{prog} deletes files", deleted=deleted
        )
    if prog in _COPIERS:
        return _copier(c)
    if prog in _OPAQUE_WRITERS:
        return Classified(
            Category.MUTATING,
            prog,
            mutation=f"{prog} changes files",
            opaque=f"{prog} writes files its command line does not name",
        )
    return Classified(Category.MUTATING, prog, mutation=f"{prog} changes files")


def _runs_commands(c: _Call) -> Classified:
    return Classified(Category.UNKNOWN, c.prog, mutation=f"{c.prog} runs other commands")


def _register(*families: tuple[Iterable[str], _Handler]) -> dict[str, _Handler]:
    handlers: dict[str, _Handler] = {}
    for programs, handler in families:
        for prog in programs:
            handlers.setdefault(prog, handler)
    return handlers


_TEST_RUNNERS: Final = (
    (("pytest", "py.test"), _pytest),
    (("unittest",), _unittest),
    (("jest", "vitest", "mocha", "ava", "tap", "jasmine"), _js_runner),
    (("playwright", "cypress"), _browser_runner),
    (("rspec", "phpunit", "pest", "ctest", "behave", "nosetests", "nose2", "ward"), _plain_runner),
    (("node", "nodejs"), _node),
    (("tox", "nox"), _tox),
)
_TOOLCHAINS: Final = (
    (("go",), _go),
    (("cargo",), _cargo),
    (("mvn", "mvnw", "gradle", "gradlew"), _jvm),
    (("dotnet",), _dotnet),
    (("deno", "swift", "mix", "bazel", "bazelisk"), _toolchain),
)
_PACKAGE_MANAGERS: Final = (
    (PM, _pm),
    (("make", "gmake", "just", "task", "mage"), _make),
    (("hatch",), _hatch),
    (("uv",), _uv),
    (("build",), _builder),
    (("poetry", "pdm", "flit"), _package_build),
    (("pip", "pip3"), _pip),
)
_CHECKERS: Final = (
    (("ruff",), _ruff),
    (("black", "isort", "autopep8", "yapf", "autoflake"), _python_formatter),
    (("prettier", "biome", "dprint"), _web_formatter),
    (("gofmt", "goimports", "rustfmt", "clang-format", "shfmt", "terraform"), _native_formatter),
    (("pre-commit",), _pre_commit),
    (_LINTERS, _linter),
    (_TYPECHECKERS, _typechecker),
    (("tsc",), _tsc),
    (("vite", "next", "nuxt", "astro", "svelte-kit", "ng"), _package_build),
    (_BUILDERS, _builder),
    (("docker",), _docker),
    (("cmake",), _cmake),
)
_FILE_TOOLS: Final = (
    (("sed",), _sed),
    (("perl",), _perl),
    (("awk", "gawk"), _awk),
    (("find",), _find),
    (("curl",), _curl),
    ((*("rm", "unlink", "shred"), *_COPIERS, *_OPAQUE_WRITERS, *_MUTATING), _file_writer),
)

_HANDLERS: Final = _register(
    (SHELLS, _shell),
    *_TEST_RUNNERS,
    *_TOOLCHAINS,
    *_PACKAGE_MANAGERS,
    *_CHECKERS,
    (("git",), _git),
    *_FILE_TOOLS,
    (("xargs", "parallel", "watch", "eval", "source", "."), _runs_commands),
)
"""The handler for each program it classifies; the first family to name a program owns it."""


def _fallback(c: _Call) -> Classified:
    prog, args = c.prog, c.args
    if prog in READ_ONLY_PROGRAMS:
        return Classified(Category.READ_ONLY, prog)
    if "/" not in c.words[0] and len(args) == 1 and args[0] in _VERSION_FLAGS:
        return Classified(Category.READ_ONLY, prog)
    if PYTHON.match(prog) and args[:1] == ["-c"] and len(args) >= 2:
        if _PYTHON_SIDE_EFFECTS.search(args[1]) is None:
            return Classified(Category.READ_ONLY, prog)
        return Classified(Category.UNKNOWN, prog, mutation="python -c runs code that can write")
    return Classified(Category.UNKNOWN, prog, mutation=f"{prog} is not a known read-only program")


def _classify_program(
    words: list[str], env: dict[str, str], depth: int, cwd: str | None
) -> Classified:
    call = _Call(words, program_name(words[0]), words[1:], env, depth, cwd)
    handler = _HANDLERS.get(call.prog)
    result = handler(call) if handler is not None else None
    return result if result is not None else _fallback(call)


def _classify(simple: Simple, depth: int, cwd: str | None) -> Classified:
    wrapped = Wrapped()
    words = unwrap(list(simple.words), wrapped)
    if not words:
        result = Classified(Category.READ_ONLY, "")
    else:
        result = _classify_program(words, simple.env, depth, cwd)
    if result.runner is not None:
        result.filters.extend(wrapped.filters)
        result.relocations = [*wrapped.dirs, *result.relocations]
        if wrapped.filters or result.relocations:
            result.paths = None
    read_only = result.category is Category.READ_ONLY
    if simple.writes and result.mutation is None:
        result.mutation = f"writes {simple.writes[0]}"
        if result.category is Category.READ_ONLY:
            result.category = Category.MUTATING
    if simple.heredoc and result.category is Category.UNKNOWN:
        result.mutation = result.mutation or "feeds a heredoc to a program"
    if result.runner is not None:
        changed = [
            name
            for name, value in simple.env.items()
            if is_harness_env(name)
            and name != "PYTEST_ADDOPTS"
            and not harmless_env(name, value, cwd)
        ]
        result.filters.extend(f"runs with {name} changed" for name in changed)
        if changed:
            result.paths = None
        run_env = command_env(simple)
        result.run_env = [
            *result.run_env,
            *(
                name
                for name, value in run_env.items()
                if not run_env_allowed(name) and not harmless_env(name, value, cwd)
            ),
        ]
        regen = next(
            (n for n, v in run_env.items() if regenerates_env(n) and not off(f"={v}")), None
        )
        if regen is not None and result.mutation is None:
            result.mutation = result.opaque = f"{regen} set: the run rewrites expected outputs"
    named = result.touched
    if named is None:
        mutating = result.category is Category.MUTATING and not read_only
        named = [w for w in words[1:] if not w.startswith("-")] if mutating else []
    result.touched = [*named, *simple.writes]
    if result.opaque is None and result.mutation is not None:
        if result.category in (Category.UNKNOWN, Category.BUILD):
            result.opaque = result.mutation
        elif built := next((p for p in result.touched if unspelled(p)), None):
            result.opaque = f"writes {built}, a path the shell builds at run time"
    return result


def _empty(reason: str, command: str) -> CommandAnalysis:
    return CommandAnalysis(
        categories=[Category.UNKNOWN],
        mutation=f"the command could not be parsed ({reason})",
        exit_masked_by="the command could not be parsed",
        parse_error=reason,
        opaque_write=f"the command could not be parsed ({reason})",
        session_store=SESSION_STORE.search(command) is not None,
        guard_tamper=(
            f"the command could not be parsed and names {m.group(0)}"
            if (m := AGENT_CONFIG.search(command) or JUDGE_NAME.search(command))
            else None
        ),
    )


def _cd_step(simple: Simple) -> str:
    """Where a `cd`, `pushd` or `popd` goes: its target as written, or `?` when only the shell
    knows (`popd`, `cd -`, `cd` alone, a variable)."""
    words = simple.words
    args = [w for w in words[1:] if not (w.startswith("-") and w != "-")]
    if program_name(words[0]) == "popd" or len(args) != 1:
        return "?"
    target = args[0]
    return "?" if target == "-" or target.startswith("~") or unspelled(target) else target


@dataclass(slots=True)
class _Walk:
    """What `_analyse` gathers from a command line's simple commands, in order."""

    agent_text: bool
    categories: list[Category] = field(default_factory=list[Category])
    programs: list[str] = field(default_factory=list[str])
    runners: list[str] = field(default_factory=list[str])
    filters: list[str] = field(default_factory=list[str])
    deleted: list[str] = field(default_factory=list[str])
    touched: list[str] = field(default_factory=list[str])
    written: list[str] = field(default_factory=list[str])
    cds: list[str] = field(default_factory=list[str])
    run_cds: list[str] = field(default_factory=list[str])
    run_env: list[str] = field(default_factory=list[str])
    persisting: list[str] = field(default_factory=list[str])
    config_files: list[str] = field(default_factory=list[str])
    mutating_at: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    test_paths: list[str] | None = None
    collect_only: bool = False
    mutation: str | None = None
    opaque: str | None = None
    lasting: str | None = None
    nested_store: bool = False
    nested_guard: str | None = None
    nested_after: bool = False
    run_after: bool = False
    primary: tuple[int, int] | None = None
    masked: str | None = None
    output_filter: str | None = None


def _note(
    walk: _Walk, simple: Simple, classified: Classified, alone: bool, at: tuple[int, int]
) -> None:
    """Record what every simple command adds, whether or not it nests another command line."""
    walk.touched.extend(classified.touched or [])
    walk.opaque = walk.opaque or classified.opaque
    if classified.program in _DETACHERS:
        walk.lasting = (
            walk.lasting or f"{classified.program} starts a job that outlives the command"
        )
    if classified.mutation is not None or (
        classified.nested is not None and classified.nested.mutates
    ):
        walk.mutating_at.append(at)
    walk.categories.append(classified.category)
    if classified.program:
        walk.programs.append(classified.program)
    if classified.program in ("cd", "pushd", "popd"):
        step = _cd_step(simple)
        alone = alone and not simple.subshell
        walk.cds.append(step if alone else "?")
        if alone:
            walk.persisting.append(step)


def _take_nested(
    walk: _Walk,
    simple: Simple,
    classified: Classified,
    nested: CommandAnalysis,
    at: tuple[int, int],
    cwd: str | None,
) -> None:
    """Fold in a command line run by `bash -c`, `xargs` and the like."""
    walk.runners.extend(nested.runners)
    walk.filters.extend(nested.test_filters)
    walk.collect_only = walk.collect_only or nested.collect_only
    walk.deleted.extend(nested.deleted_paths)
    walk.agent_text = walk.agent_text or nested.agent_text
    walk.mutation = walk.mutation or nested.mutation
    walk.touched.extend(nested.touched_paths)
    walk.opaque = walk.opaque or nested.opaque_write
    walk.lasting = walk.lasting or nested.lasting_mutation
    walk.written.extend(nested.source_findings)
    walk.nested_store = walk.nested_store or nested.session_store
    walk.nested_guard = walk.nested_guard or nested.guard_tamper
    if nested.runners:
        walk.run_cds = [*walk.cds, *nested.run_cds]
        walk.test_paths = nested.test_paths
        outer = [
            name
            for name, value in command_env(simple).items()
            if not run_env_allowed(name) and not harmless_env(name, value, cwd)
        ]
        walk.run_env = [*nested.run_env, *outer]
        walk.config_files.extend(nested.config_files)
    if nested.runners or (classified.category in _CHECKS and not walk.runners):
        walk.nested_after = nested.mutation_after_check
        walk.primary = at
        walk.masked = nested.exit_masked_by
        walk.output_filter = nested.output_filter


def _take(walk: _Walk, classified: Classified, at: tuple[int, int]) -> None:
    """Fold in a simple command that runs a program itself."""
    walk.mutation = walk.mutation or classified.mutation
    walk.deleted.extend(classified.deleted)
    if classified.runner is not None:
        walk.runners.append(classified.runner)
        walk.filters.extend(classified.filters)
        walk.run_cds = [*walk.cds, *classified.relocations]
        walk.run_env = list(classified.run_env)
        walk.config_files.extend(classified.config_files)
        walk.run_after = classified.writes_after
        walk.test_paths = (
            classified.paths
            if classified.paths is not None
            else ([] if not classified.filters else None)
        )
        walk.collect_only = walk.collect_only or classified.collect_only
    elif classified.category not in _CHECKS or walk.runners:
        return
    walk.primary = at
    walk.masked = None
    walk.output_filter = None


def _first_filter(after_pipe: list[Simple]) -> str:
    """The program the check's output is piped into, skipping `tail`, which keeps the summary."""
    names = [program_name((unwrap(c.words) or ["?"])[0]) for c in after_pipe]
    return next((name for name in names if name != "tail"), names[0])


def _followed_by(pipelines: list[Pipeline], start: int) -> str | None:
    """Why the line's exit status is not the check's: a later pipeline joined by anything but
    `&&`."""
    for k in range(start, len(pipelines) - 1):
        op = pipelines[k].op_after
        if op != "&&":
            shown = " ".join(pipelines[k + 1].commands[0].words[:3])
            label = "a newline" if op == "\n" else repr(op)
            return f"it is followed by {label} and {shown!r}"
    return None


def _mask_exit(
    walk: _Walk, pipelines: list[Pipeline], pipefail: bool, depth: int, cwd: str | None
) -> None:
    """Why the check's exit status may not be the command line's, if it may not."""
    if walk.primary is None or walk.masked is not None:
        return
    p_index, c_index = walk.primary
    pipeline = pipelines[p_index]
    after_pipe = pipeline.commands[c_index + 1 :]
    if after_pipe:
        walk.output_filter = _first_filter(after_pipe)
        if not pipefail:
            walk.masked = f"its output is piped into {walk.output_filter}"
    if pipeline.commands[c_index].negated:
        walk.masked = "it is negated with !"
    if walk.masked is None and pipeline.op_after == "&":
        walk.masked = "it runs in the background"
    if walk.masked is None:
        walk.masked = _followed_by(pipelines, p_index)
    if walk.masked is None:
        trailing = [
            _classify(c, depth, cwd).category
            for later in pipelines[p_index + 1 :]
            for c in later.commands
        ]
        if any(cat is not Category.READ_ONLY for cat in trailing):
            walk.masked = "other commands run after it"


def _analyse(command: str, cwd: str | None, depth: int) -> CommandAnalysis:
    if len(command) > MAX_COMMAND_CHARS:
        return _empty(f"longer than {MAX_COMMAND_CHARS} characters", command)
    try:
        pipelines = parse(strip_comments(tokenize(command)))
    except ValueError as exc:
        return _empty(str(exc), command)

    walk = _Walk(agent_text=_AGENT_SUMMARY_TEXT.search(command) is not None)
    for p_index, pipeline in enumerate(pipelines):
        for c_index, simple in enumerate(pipeline.commands):
            classified = _classify(simple, depth, cwd)
            at = (p_index, c_index)
            _note(walk, simple, classified, len(pipeline.commands) == 1, at)
            if classified.nested is not None:
                _take_nested(walk, simple, classified, classified.nested, at, cwd)
            else:
                _take(walk, classified, at)

    if any(p.op_after in ("||", "&") for p in pipelines):
        walk.persisting = ["?" for _ in walk.persisting]
    background = any(p.op_after == "&" for p in pipelines)
    if walk.lasting is None and walk.mutation is not None and background:
        walk.lasting = f"{walk.mutation} in a background job"
    mutation_after = _mutation_after(walk)
    _mask_exit(walk, pipelines, re.search(r"\bpipefail\b", command) is not None, depth, cwd)
    return _analysis(walk, pipelines, command, cwd, mutation_after)


def _mutation_after(walk: _Walk) -> bool:
    """Whether the command line changes something after its check ran."""
    return walk.primary is not None and (
        any(position > walk.primary for position in walk.mutating_at)
        or (walk.nested_after and walk.primary in walk.mutating_at)
        or walk.run_after
    )


def _analysis(
    walk: _Walk, pipelines: list[Pipeline], command: str, cwd: str | None, mutation_after: bool
) -> CommandAnalysis:
    return CommandAnalysis(
        categories=walk.categories,
        programs=walk.programs,
        runners=walk.runners,
        test_filters=list(dict.fromkeys(walk.filters)),
        collect_only=walk.collect_only,
        exit_masked_by=walk.masked,
        output_filter=walk.output_filter,
        agent_text=walk.agent_text,
        mutation=walk.mutation,
        deleted_paths=walk.deleted,
        touched_paths=list(dict.fromkeys(walk.touched)),
        opaque_write=walk.opaque,
        lasting_mutation=walk.lasting,
        run_cds=walk.run_cds,
        run_env=walk.run_env,
        exports=exports(pipelines, cwd),
        config_files=list(dict.fromkeys(walk.config_files)),
        test_paths=walk.test_paths if len(walk.runners) == 1 else None,
        cd_targets=walk.persisting,
        mutation_after_check=mutation_after,
        session_store=walk.nested_store
        or writes_session_store(pipelines, command, walk.touched, walk.deleted, walk.opaque),
        harness_change=harness_change(pipelines, command, cwd),
        guard_tamper=walk.nested_guard
        or guard_tamper(pipelines, command, walk.touched, walk.deleted, walk.opaque),
        source_findings=list(dict.fromkeys([*walk.written, *written_code(pipelines, command)])),
    )


def analyse(command: str, cwd: str | None = None) -> CommandAnalysis:
    """Parse and classify a shell command line run in `cwd`."""
    return _analyse(command, cwd, 0)
