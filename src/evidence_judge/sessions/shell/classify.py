"""Classifying simple commands and analysing whole command lines."""

from __future__ import annotations

import re
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


def _classify_program(
    words: list[str], env: dict[str, str], depth: int, cwd: str | None
) -> Classified:
    prog = program_name(words[0])
    args = words[1:]
    if prog in SHELLS and depth < MAX_NESTING:
        script = next(
            (args[i + 1] for i, a in enumerate(args[:-1]) if re.fullmatch(r"-[a-z]*c[a-z]*", a)),
            None,
        )
        if script is not None:
            nested = _analyse(script, cwd, depth + 1)
            if nested.runners:
                category = Category.TEST
            elif nested.mutation is not None:
                category = Category.MUTATING
            else:
                category = nested.categories[-1] if nested.categories else Category.READ_ONLY
            return Classified(category, prog, nested=nested)
        return Classified(Category.UNKNOWN, prog, mutation=f"{prog} runs a script")
    if prog in ("pytest", "py.test"):
        filters, collect, paths = pytest_filters(args, env)
        regen = next((a for a in args if pytest_regenerates(a)), None)
        written, emptied, after = run_writes("pytest", args, cwd)
        why = f"pytest {regen} rewrites expected outputs" if regen else None
        if written or emptied:
            option, path = (written or emptied)[0]
            why = why or f"pytest {option} writes {path}"
        return Classified(
            Category.TEST,
            prog,
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
    if prog == "unittest":
        filters, collect = unittest_filters(args)
        return Classified(Category.TEST, prog, "unittest", filters, collect)
    if prog in ("jest", "vitest", "mocha", "ava", "tap", "jasmine"):
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
    if prog == "playwright" and args[:1] == ["test"]:
        filters, collect = js_filters("playwright", args[1:])
        return Classified(Category.TEST, prog, "playwright test", filters, collect)
    if prog == "cypress" and args[:1] == ["run"]:
        filters, collect = js_filters("cypress", args[1:])
        return Classified(Category.TEST, prog, "cypress run", filters, collect)
    if prog in ("rspec", "phpunit", "pest", "ctest", "behave", "nosetests", "nose2", "ward"):
        positional = [a for a in args if not a.startswith("-")]
        filters = [f"only {a}" for a in positional]
        filters.extend(
            a.partition("=")[0]
            for a in args
            if a.partition("=")[0]
            in (
                "-e",
                "--example",
                "-t",
                "--tag",
                "--filter",
                "--group",
                "--testsuite",
                "-R",
                "-L",
                "-k",
                "--only-failures",
            )
        )
        return Classified(Category.TEST, prog, prog, filters)
    if prog in ("node", "nodejs") and "--test" in args:
        filters, update = node_test_filters(args)
        why = "node --test --test-update-snapshots rewrites the expected snapshots"
        return Classified(
            Category.TEST,
            prog,
            "node --test",
            filters,
            mutation=why if update else None,
            opaque=why if update else None,
        )
    if prog in ("tox", "nox"):
        return Classified(Category.TEST, prog, prog, tox_filters(prog, args))
    if prog == "go":
        sub = args[0] if args else ""
        if sub == "test":
            filters, collect = go_filters(args[1:])
            regen = next((a for a in args[1:] if GO_REGEN.fullmatch(a) and not off(a)), None)
            why = f"go test {regen} rewrites expected outputs" if regen else None
            written, _, after = run_writes("go test", args[1:], cwd)
            wrote = f"go test {written[0][0]} writes {written[0][1]}" if written else None
            return Classified(
                Category.TEST,
                prog,
                "go test",
                filters,
                collect,
                mutation=why or wrote,
                opaque=why,
                touched=[path for _, path in written],
                writes_after=after,
            )
        if sub in ("build", "install"):
            return Classified(Category.BUILD, prog)
        if sub == "vet":
            return Classified(Category.LINT, prog)
        if sub in ("fmt", "generate", "get") or args[:2] == ["mod", "tidy"]:
            return Classified(Category.MUTATING, prog, mutation=f"go {sub} changes files")
        if why := tool_config_change(words):
            return Classified(Category.MUTATING, prog, mutation=why, touched=[])
        if sub in ("version", "env", "list", "doc", "help") or args[:2] == ["mod", "graph"]:
            return Classified(Category.READ_ONLY, prog)
        return Classified(Category.UNKNOWN, prog, mutation=f"go {sub} runs a program")
    if prog == "cargo":
        sub = args[0] if args else ""
        if sub in ("test", "t"):
            filters, collect = cargo_filters(args[1:])
            return Classified(Category.TEST, prog, "cargo test", filters, collect)
        if sub == "insta":
            action = args[1] if len(args) > 1 else ""
            if action in ("accept", "review", "reject") or (
                action == "test" and any(a in ("--accept", "--review") for a in args)
            ):
                why = f"cargo insta {action} rewrites expected outputs"
                return Classified(Category.MUTATING, prog, mutation=why, opaque=why)
            if action == "test":
                filters, collect = cargo_filters(args[2:])
                return Classified(Category.TEST, prog, "cargo insta test", filters, collect)
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
    if prog in PM:
        return pm_script(prog, words)
    if prog in ("make", "gmake", "just", "task", "mage"):
        return make_like(prog, words)
    if prog in ("mvn", "mvnw", "gradle", "gradlew"):
        goals = [a for a in args if not a.startswith("-")]
        filters = [
            a.partition("=")[0]
            for a in args
            if a.startswith(("-Dtest", "-pl", "--tests", "--projects"))
        ]
        if any(g in ("test", "verify", "check", "integration-test") for g in goals):
            return Classified(Category.TEST, prog, prog, filters)
        if any(g in ("build", "package", "compile", "install", "assemble") for g in goals):
            return Classified(Category.BUILD, prog)
        return Classified(Category.UNKNOWN, prog, mutation=f"{prog} runs a project task")
    if prog == "dotnet":
        sub = args[0] if args else ""
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
    if prog in ("deno", "swift", "mix", "bazel", "bazelisk"):
        sub = args[0] if args else ""
        if sub == "test":
            positional = [a for a in args[1:] if not a.startswith("-")]
            filters = [f"only {a}" for a in positional]
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
    if prog == "hatch" and args[:1] == ["test"]:
        return Classified(
            Category.TEST, prog, "hatch test", generic_filters("hatch test", args[1:])
        )
    if prog == "ruff":
        sub = args[0] if args else ""
        if sub == "format":
            check = any(a in ("--check", "--diff") for a in args)
            return Classified(
                Category.FORMAT, prog, mutation=None if check else "ruff format rewrites files"
            )
        fix = any(
            a.partition("=")[0] in ("--fix", "--unsafe-fixes", "--fix-only")
            and not a.endswith("=false")
            for a in args
        )
        return Classified(
            Category.LINT, prog, mutation="ruff --fix rewrites files" if fix else None
        )
    if prog in ("black", "isort", "autopep8", "yapf", "autoflake"):
        check = any(a in ("--check", "--diff", "--check-only", "-c") for a in args)
        if prog in ("autopep8", "yapf", "autoflake"):
            check = not any(a in ("-i", "--in-place") for a in args)
        return Classified(
            Category.FORMAT, prog, mutation=None if check else f"{prog} rewrites files"
        )
    if prog in ("prettier", "biome", "dprint"):
        writes = any(
            a in ("--write", "-w", "--apply", "--apply-unsafe", "--fix") for a in args
        ) or args[:1] == ["fmt"]
        category = (
            Category.LINT
            if prog == "biome" and args[:1] in (["lint"], ["check"], ["ci"])
            else Category.FORMAT
        )
        return Classified(category, prog, mutation=f"{prog} rewrites files" if writes else None)
    if prog in ("gofmt", "goimports", "rustfmt", "clang-format", "shfmt", "terraform"):
        writes = any(a in ("-w", "-i", "--write") for a in args) or (
            prog == "rustfmt" and "--check" not in args
        )
        if prog == "terraform":
            writes = args[:1] == ["fmt"] and "-check" not in args
        return Classified(
            Category.FORMAT, prog, mutation=f"{prog} rewrites files" if writes else None
        )
    if prog == "pre-commit":
        return Classified(Category.LINT, prog, mutation="pre-commit hooks may rewrite files")
    if prog in _LINTERS:
        fix = any(a.partition("=")[0] in FIX_FLAGS for a in args)
        return Classified(
            Category.LINT, prog, mutation=f"{prog} fixes rewrite files" if fix else None
        )
    if prog in _TYPECHECKERS:
        return Classified(Category.TYPECHECK, prog)
    if prog == "tsc":
        no_emit = "--noEmit" in args or "--noemit" in args
        return Classified(Category.TYPECHECK if no_emit else Category.BUILD, prog)
    if prog in ("vite", "next", "nuxt", "astro", "svelte-kit", "ng") and args[:1] == ["build"]:
        return Classified(Category.BUILD, prog)
    if (
        prog in _BUILDERS
        or (prog == "docker" and args[:1] == ["build"])
        or (prog == "cmake" and "--build" in args)
    ):
        return Classified(Category.BUILD, prog)
    if prog in ("build",) or (
        prog in ("uv", "poetry", "hatch", "pdm", "flit") and args[:1] == ["build"]
    ):
        return Classified(Category.BUILD, prog)
    if prog == "uv":
        sub = args[0] if args else ""
        if sub in ("sync", "add", "remove", "lock", "venv", "init", "self", "tool") or args[:2] in (
            ["pip", "install"],
            ["pip", "uninstall"],
            ["pip", "sync"],
        ):
            return Classified(Category.MUTATING, prog, mutation=f"uv {sub} changes the environment")
        return (
            Classified(Category.READ_ONLY, prog)
            if sub in ("pip", "tree", "version", "--version", "help", "python")
            else Classified(Category.UNKNOWN, prog, mutation=f"uv {sub} may change files")
        )
    if prog in ("pip", "pip3"):
        sub = args[0] if args else ""
        if sub in ("install", "uninstall", "download", "wheel"):
            return Classified(
                Category.MUTATING, prog, mutation=f"pip {sub} changes the environment"
            )
        return Classified(Category.READ_ONLY, prog)
    if prog == "git":
        return git(words)
    if prog == "sed":
        return sed(args)
    if prog == "perl":
        return perl(args)
    if prog in ("awk", "gawk"):
        program_text = " ".join(args)
        writes = (
            "system(" in program_text or re.search(r"print[^|;]*>\s*\"", program_text) is not None
        )
        return Classified(
            Category.MUTATING if writes else Category.READ_ONLY,
            prog,
            mutation="awk writes files or runs commands" if writes else None,
            touched=[],
            opaque="awk writes files or runs commands" if writes else None,
        )
    if prog == "find":
        acting = [
            a
            for a in args
            if a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")
        ]
        return Classified(
            Category.MUTATING if acting else Category.READ_ONLY,
            prog,
            mutation=f"find {acting[0]} acts on files" if acting else None,
            touched=[],
            opaque=f"find {acting[0]} acts on the files it finds" if acting else None,
        )
    if prog == "curl":
        named = [
            args[i + 1] if a in ("-o", "--output") else a.partition("=")[2]
            for i, a in enumerate(args)
            if (a in ("-o", "--output") and i + 1 < len(args)) or a.startswith("--output=")
        ]
        remote = any(a in ("-O", "--remote-name", "--remote-name-all", "-J") for a in args)
        writes = bool(named) or remote
        return Classified(
            Category.MUTATING if writes else Category.READ_ONLY,
            prog,
            mutation="curl writes a file" if writes else None,
            touched=named,
            opaque="curl -O names the file after the URL" if remote else None,
        )
    if prog in ("rm", "unlink", "shred"):
        deleted = [a for a in args if not a.startswith("-")]
        return Classified(
            Category.MUTATING, prog, mutation=f"{prog} deletes files", deleted=deleted
        )
    if prog in _COPIERS:
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
    if prog in _OPAQUE_WRITERS:
        return Classified(
            Category.MUTATING,
            prog,
            mutation=f"{prog} changes files",
            opaque=f"{prog} writes files its command line does not name",
        )
    if prog in _MUTATING:
        return Classified(Category.MUTATING, prog, mutation=f"{prog} changes files")
    if prog in ("xargs", "parallel", "watch", "eval", "source", "."):
        return Classified(Category.UNKNOWN, prog, mutation=f"{prog} runs other commands")
    if prog in READ_ONLY_PROGRAMS:
        return Classified(Category.READ_ONLY, prog)
    if "/" not in words[0] and len(args) == 1 and args[0] in _VERSION_FLAGS:
        return Classified(Category.READ_ONLY, prog)
    if PYTHON.match(prog) and args[:1] == ["-c"] and len(args) >= 2:
        if _PYTHON_SIDE_EFFECTS.search(args[1]) is None:
            return Classified(Category.READ_ONLY, prog)
        return Classified(Category.UNKNOWN, prog, mutation="python -c runs code that can write")
    return Classified(Category.UNKNOWN, prog, mutation=f"{prog} is not a known read-only program")


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


def _analyse(command: str, cwd: str | None, depth: int) -> CommandAnalysis:
    if len(command) > MAX_COMMAND_CHARS:
        return _empty(f"longer than {MAX_COMMAND_CHARS} characters", command)
    try:
        pipelines = parse(strip_comments(tokenize(command)))
    except ValueError as exc:
        return _empty(str(exc), command)

    categories: list[Category] = []
    programs: list[str] = []
    runners: list[str] = []
    filters: list[str] = []
    deleted: list[str] = []
    collect_only = False
    mutation: str | None = None
    agent_text = _AGENT_SUMMARY_TEXT.search(command) is not None
    pipefail = re.search(r"\bpipefail\b", command) is not None
    primary: tuple[int, int] | None = None
    masked: str | None = None
    output_filter: str | None = None

    touched: list[str] = []
    opaque: str | None = None
    lasting: str | None = None
    nested_store = False
    nested_guard: str | None = None
    written: list[str] = []
    cds: list[str] = []
    test_paths: list[str] | None = None
    run_cds: list[str] = []
    run_env: list[str] = []
    persisting: list[str] = []
    mutating_at: list[tuple[int, int]] = []
    nested_after = False
    run_after = False
    config_files: list[str] = []
    for p_index, pipeline in enumerate(pipelines):
        for c_index, simple in enumerate(pipeline.commands):
            classified = _classify(simple, depth, cwd)
            touched.extend(classified.touched or [])
            opaque = opaque or classified.opaque
            if classified.program in _DETACHERS:
                lasting = lasting or f"{classified.program} starts a job that outlives the command"
            if classified.mutation is not None or (
                classified.nested is not None and classified.nested.mutates
            ):
                mutating_at.append((p_index, c_index))
            categories.append(classified.category)
            if classified.program:
                programs.append(classified.program)
            if classified.program in ("cd", "pushd", "popd"):
                step = _cd_step(simple)
                alone = len(pipeline.commands) == 1 and not simple.subshell
                cds.append(step if alone else "?")
                if alone:
                    persisting.append(step)
            nested = classified.nested
            if nested is not None:
                runners.extend(nested.runners)
                filters.extend(nested.test_filters)
                collect_only = collect_only or nested.collect_only
                deleted.extend(nested.deleted_paths)
                agent_text = agent_text or nested.agent_text
                mutation = mutation or nested.mutation
                touched.extend(nested.touched_paths)
                opaque = opaque or nested.opaque_write
                lasting = lasting or nested.lasting_mutation
                written.extend(nested.source_findings)
                nested_store = nested_store or nested.session_store
                nested_guard = nested_guard or nested.guard_tamper
                if nested.runners:
                    run_cds = [*cds, *nested.run_cds]
                    test_paths = nested.test_paths
                    outer = [
                        name
                        for name, value in command_env(simple).items()
                        if not run_env_allowed(name) and not harmless_env(name, value, cwd)
                    ]
                    run_env = [*nested.run_env, *outer]
                    config_files.extend(nested.config_files)
                if nested.runners or (classified.category in _CHECKS and not runners):
                    nested_after = nested.mutation_after_check
                    primary = (p_index, c_index)
                    masked = nested.exit_masked_by
                    output_filter = nested.output_filter
                continue
            mutation = mutation or classified.mutation
            deleted.extend(classified.deleted)
            if classified.runner is not None:
                runners.append(classified.runner)
                filters.extend(classified.filters)
                run_cds = [*cds, *classified.relocations]
                run_env = list(classified.run_env)
                config_files.extend(classified.config_files)
                run_after = classified.writes_after
                test_paths = (
                    classified.paths
                    if classified.paths is not None
                    else ([] if not classified.filters else None)
                )
                collect_only = collect_only or classified.collect_only
                primary = (p_index, c_index)
                masked = None
                output_filter = None
            elif classified.category in _CHECKS and not runners:
                primary = (p_index, c_index)
                masked = None
                output_filter = None

    if any(p.op_after in ("||", "&") for p in pipelines):
        persisting = ["?" for _ in persisting]
    if lasting is None and mutation is not None and any(p.op_after == "&" for p in pipelines):
        lasting = f"{mutation} in a background job"
    mutation_after = primary is not None and (
        any(position > primary for position in mutating_at)
        or (nested_after and primary in mutating_at)
        or run_after
    )
    if primary is not None and masked is None:
        p_index, c_index = primary
        pipeline = pipelines[p_index]
        after_pipe = pipeline.commands[c_index + 1 :]
        if after_pipe:
            output_filter = (
                program_name(unwrap(after_pipe[0].words)[0]) if unwrap(after_pipe[0].words) else "?"
            )
            if any(program_name((unwrap(c.words) or ["?"])[0]) != "tail" for c in after_pipe):
                output_filter = next(
                    program_name((unwrap(c.words) or ["?"])[0])
                    for c in after_pipe
                    if program_name((unwrap(c.words) or ["?"])[0]) != "tail"
                )
            if not pipefail:
                masked = f"its output is piped into {output_filter}"
        if pipeline.commands[c_index].negated:
            masked = "it is negated with !"
        if masked is None and pipeline.op_after == "&":
            masked = "it runs in the background"
        if masked is None:
            for k in range(p_index, len(pipelines) - 1):
                op = pipelines[k].op_after
                if op != "&&":
                    following = pipelines[k + 1]
                    shown = " ".join(following.commands[0].words[:3])
                    label = "a newline" if op == "\n" else repr(op)
                    masked = f"it is followed by {label} and {shown!r}"
                    break
        if masked is None:
            trailing = [
                _classify(c, depth, cwd).category
                for later in pipelines[p_index + 1 :]
                for c in later.commands
            ]
            if any(cat is not Category.READ_ONLY for cat in trailing):
                masked = "other commands run after it"

    return CommandAnalysis(
        categories=categories,
        programs=programs,
        runners=runners,
        test_filters=list(dict.fromkeys(filters)),
        collect_only=collect_only,
        exit_masked_by=masked,
        output_filter=output_filter,
        agent_text=agent_text,
        mutation=mutation,
        deleted_paths=deleted,
        touched_paths=list(dict.fromkeys(touched)),
        opaque_write=opaque,
        lasting_mutation=lasting,
        run_cds=run_cds,
        run_env=run_env,
        exports=exports(pipelines, cwd),
        config_files=list(dict.fromkeys(config_files)),
        test_paths=test_paths if len(runners) == 1 else None,
        cd_targets=persisting,
        mutation_after_check=mutation_after,
        session_store=nested_store
        or writes_session_store(pipelines, command, touched, deleted, opaque),
        harness_change=harness_change(pipelines, command, cwd),
        guard_tamper=nested_guard or guard_tamper(pipelines, command, touched, deleted, opaque),
        source_findings=list(dict.fromkeys([*written, *written_code(pipelines, command)])),
    )


def analyse(command: str, cwd: str | None = None) -> CommandAnalysis:
    """Parse and classify a shell command line run in `cwd`."""
    return _analyse(command, cwd, 0)
