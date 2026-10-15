"""A small, conservative shell parser for the commands coding agents run.

It never executes anything. It splits a command line into simple commands joined by operators,
strips environment assignments, redirections and wrappers (`timeout`, `uv run`, `npx`,
`bash -c`, ...), and classifies each simple command: a test run (and which runner, and whether it
runs a subset), a build, a lint, a type check, a formatter, a read-only command, or one that may
change the workspace. Anything it cannot classify may change the workspace: being wrong in that
direction costs a NEEDS_REVIEW, never a false PASS.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final

from evidence_judge.sessions.models import Category, CommandAnalysis

MAX_COMMAND_CHARS: Final = 100_000
MAX_NESTING: Final = 4

_OPERATORS: Final = (
    "&&",
    "||",
    ";;",
    "|&",
    "&>>",
    "&>",
    ">>",
    ">&",
    "<<<",
    "<<-",
    "<<",
    "<>",
    ">|",
    "|",
    ";",
    "&",
    "<",
    ">",
    "(",
    ")",
    "\n",
)
_SEPARATORS: Final = frozenset({"&&", "||", ";", ";;", "&", "\n"})
_PIPES: Final = frozenset({"|", "|&"})
_REDIRECTS: Final = frozenset({">", ">>", ">|", "&>", "&>>", ">&", "<", "<>", "<<<"})
_HEREDOCS: Final = frozenset({"<<", "<<-"})
_SAFE_TARGETS: Final = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
_CHECKS: Final = frozenset({Category.BUILD, Category.LINT, Category.TYPECHECK, Category.FORMAT})
_ASSIGNMENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON: Final = re.compile(r"^(?:python|pypy)[0-9.]*$|^py$")
_SHELLS: Final = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
SESSION_STORE: Final = re.compile(
    r"\.claude/projects|\.claude/plugins|\.codex/sessions|\.codex/archived_sessions|"
    r"\.gemini/tmp|session-evidence|evidence-judge/evidence|\.evidence-judge(?:/|$)|"
    r"EVJ_HOOK_LOG_DIR",
    re.IGNORECASE,
)
"""Where harnesses and the evidence recorder keep session records (and the project's
`.evidence-judge/` spec): a command that writes there can rewrite the evidence."""
AGENT_CONFIG: Final = re.compile(
    r"(?:^|/)\.claude/(?:[^/]*settings[^/]*\.json|(?:[^/]+/)*[^/]*hooks[^/]*)$|"
    r"(?:^|/)\.claude/(?:hooks|plugins)(?:/|$)|(?:^|/)\.claude-plugin(?:/|$)|"
    r"(?:^|/)\.codex(?:/|$)|(?:^|/)\.gemini(?:/|$)|(?:^|/)hooks/hooks\.json$|"
    r"(?:^|/)\.mcp\.json$|(?:^|/)managed-settings\.json$|"
    r"(?:^|/)(?:ClaudeCode|claude-code|codex|gemini-cli)/(?:[^/]+/)*[^/]*settings[^/]*$",
    re.IGNORECASE,
)
"""Coding-agent configuration: settings that can turn hooks off (`disableAllHooks`), hook and
plugin definitions, MCP server lists, and the Codex and Gemini CLI homes. An agent that writes
one can switch off the hooks that record and judge it."""
JUDGE_INSTALL: Final = re.compile(
    r"(?:^|/)(?:bin|Scripts)/evidence-judge(?:\.exe|\.cmd)?$|"
    r"(?:^|/)(?:uv/tools|pipx/venvs|tools)/evidence-judge(?:/|$)",
    re.IGNORECASE,
)
"""The installed `evidence-judge` executable or tool environment the hooks run."""
_JUDGE_NAME: Final = re.compile(r"evidence[-_]judge", re.IGNORECASE)
_PACKAGE_VERBS: Final = frozenset({"install", "uninstall", "remove", "rm", "upgrade", "reinstall"})


def guard_path_reason(path: str, shown: str | None = None) -> str | None:
    """Why writing `path` (named `shown` in the reason) can disable the evidence hooks or the
    judge, or None."""
    shown = shown or path
    if JUDGE_INSTALL.search(path):
        return f"replaced or removed the evidence-judge install at {shown}"
    if AGENT_CONFIG.search(path):
        return f"changed agent configuration {shown}, which can disable the evidence hooks"
    return None


_HOOK_SWITCH: Final = re.compile(
    r"disableAllHooks|allowManagedHooksOnly|\"?hooks\"?\s*[:=]\s*(?:\{\s*\}|\[\s*\]|null|false)",
    re.IGNORECASE,
)


def hook_switch_reason(why: str, text: str) -> str:
    """`why`, saying so when `text` (a command line or written content) switches hooks off."""
    found = _HOOK_SWITCH.search(text)
    if found is None:
        return why
    return f"{why}; it sets {found.group(0)}, which turns the hooks (the evidence recorder and judge) off"


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
_GIT_MUTATING: Final = frozenset(
    {
        "apply",
        "checkout",
        "restore",
        "reset",
        "stash",
        "switch",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "pull",
        "mv",
        "rm",
        "clean",
        "am",
        "worktree",
        "submodule",
        "clone",
        "init",
        "filter-branch",
        "filter-repo",
        "update-index",
        "read-tree",
        "checkout-index",
        "sparse-checkout",
        "lfs",
        "bisect",
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
_PM: Final = frozenset({"npm", "pnpm", "yarn", "bun"})
_PM_INSTALL: Final = frozenset(
    {
        "install",
        "i",
        "ci",
        "add",
        "remove",
        "rm",
        "uninstall",
        "un",
        "update",
        "up",
        "upgrade",
        "link",
        "unlink",
        "rebuild",
        "dedupe",
        "prune",
        "patch",
        "import",
        "create",
        "init",
    }
)
_PM_READ_ONLY: Final = frozenset(
    {
        "ls",
        "list",
        "view",
        "info",
        "outdated",
        "audit",
        "why",
        "explain",
        "config",
        "help",
        "version",
        "--version",
        "-v",
        "root",
        "bin",
        "prefix",
        "whoami",
        "search",
        "doctor",
    }
)
_TYPECHECK_SCRIPTS: Final = re.compile(r"^(?:type-?check|types|tsc|check-types|typecheck:.*)$")
_LINT_SCRIPTS: Final = re.compile(r"^lint(?::.*)?$")
_FORMAT_SCRIPTS: Final = re.compile(r"^(?:format|fmt|prettier)(?::.*)?$")
_BUILD_SCRIPTS: Final = re.compile(r"^(?:build|compile)(?::.*)?$")
_TEST_TARGETS: Final = frozenset({"test", "tests", "check"})

_PYTEST_VALUE_FLAGS: Final = frozenset(
    {
        "-k",
        "-m",
        "-p",
        "-c",
        "-o",
        "-n",
        "-W",
        "-r",
        "--tb",
        "--cov",
        "--cov-report",
        "--cov-config",
        "--cov-fail-under",
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--rootdir",
        "--confcutdir",
        "--durations",
        "--durations-min",
        "--maxfail",
        "--timeout",
        "--junitxml",
        "--junit-xml",
        "--basetemp",
        "--import-mode",
        "--log-level",
        "--log-cli-level",
        "--log-file",
        "--color",
        "--capture",
        "--dist",
        "--override-ini",
        "--config-file",
        "--randomly-seed",
        "--reruns",
        "--html",
        "--benchmark-json",
        "--count",
        "--workers",
        "--tests-per-worker",
        "--log-format",
        "--log-date-format",
    }
)
_PYTEST_FILTER_FLAGS: Final = {
    "-k": "-k",
    "-m": "-m",
    "--deselect": "--deselect",
    "--ignore": "--ignore",
    "--ignore-glob": "--ignore-glob",
    "-c": "a different config (-c)",
    "--config-file": "a different config (--config-file)",
    "-o": "a config override (-o)",
    "--override-ini": "a config override (--override-ini)",
    "-p": "a plugin switch (-p)",
    "--rootdir": "a different root (--rootdir)",
    "--confcutdir": "--confcutdir",
}
_HARMLESS_PLUGIN_SWITCHES: Final = (["no:cacheprovider"],)
"""`-p` values that change no test's outcome: the cache plugin only stores --lf state."""
_PYTEST_FILTER_SWITCHES: Final = {
    "--lf": "--lf",
    "--last-failed": "--last-failed",
    "--sw": "--stepwise",
    "--stepwise": "--stepwise",
    "--sw-skip": "--stepwise-skip",
    "--noconftest": "--noconftest",
    "--pyargs": "--pyargs",
    "--doctest-only": "--doctest-only",
}
_JS_VALUE_FLAGS: Final = frozenset(
    {
        "-t",
        "--testNamePattern",
        "--testPathPattern",
        "--testPathPatterns",
        "--testPathIgnorePatterns",
        "--shard",
        "-c",
        "--config",
        "--reporters",
        "--reporter",
        "-w",
        "--maxWorkers",
        "--selectProjects",
        "--project",
        "--environment",
        "--dir",
        "--root",
        "--mode",
        "--changedSince",
        "--outputFile",
        "--coverageReporters",
        "--testTimeout",
        "--pool",
        "--poolOptions",
        "-r",
        "--grep",
        "-g",
        "--fgrep",
        "-f",
        "--spec",
        "--exclude",
        "--browser",
        "--workers",
        "-j",
        "--retries",
        "--timeout",
        "--seed",
        "--require",
        "--ui",
        "--slow",
        "--filter",
    }
)
_JS_FILTER_FLAGS: Final = frozenset(
    {
        "-t",
        "--testNamePattern",
        "--testPathPattern",
        "--testPathPatterns",
        "--testPathIgnorePatterns",
        "--shard",
        "-c",
        "--config",
        "--selectProjects",
        "--project",
        "--changedSince",
        "--dir",
        "--root",
        "--grep",
        "-g",
        "--fgrep",
        "-f",
        "--spec",
        "--exclude",
        "--filter",
    }
)
_JS_FILTER_SWITCHES: Final = frozenset(
    {
        "-o",
        "--onlyChanged",
        "--findRelatedTests",
        "--related",
        "--lastCommit",
        "--changed",
        "--onlyFailures",
        "--bail",
        "--invert",
        "--only",
        "--last-failed",
    }
)
_JS_COLLECT: Final = frozenset({"--listTests", "--list", "--showConfig"})
_VITEST_SUBCOMMANDS: Final = frozenset({"run", "watch", "dev"})
_GO_VALUE_FLAGS: Final = frozenset(
    {
        "-run",
        "-skip",
        "-bench",
        "-count",
        "-timeout",
        "-tags",
        "-p",
        "-parallel",
        "-coverprofile",
        "-covermode",
        "-coverpkg",
        "-cpu",
        "-exec",
        "-o",
        "-vet",
        "-shuffle",
        "-fuzz",
        "-fuzztime",
        "-benchtime",
        "-ldflags",
        "-gcflags",
        "-mod",
        "-C",
    }
)
_CARGO_VALUE_FLAGS: Final = frozenset(
    {
        "-p",
        "--package",
        "--test",
        "--bin",
        "--example",
        "--bench",
        "--exclude",
        "--features",
        "-F",
        "--target",
        "-j",
        "--jobs",
        "--profile",
        "--manifest-path",
        "--target-dir",
        "--color",
        "-E",
        "--filterset",
        "--message-format",
        "--config",
        "-Z",
    }
)
_CARGO_FILTER_FLAGS: Final = frozenset(
    {"-p", "--package", "--test", "--bin", "--example", "--bench", "--exclude", "-E", "--filterset"}
)
_CARGO_FILTER_SWITCHES: Final = frozenset(
    {"--lib", "--bins", "--doc", "--examples", "--benches", "--tests"}
)
_TEST_BINARY_FILTERS: Final = frozenset({"--skip", "--exact", "--ignored"})
_UV_VALUE_FLAGS: Final = frozenset(
    {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--project",
        "--directory",
        "--group",
        "--extra",
        "--package",
        "--env-file",
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "-f",
        "--no-group",
        "--only-group",
        "--config-file",
        "--cache-dir",
        "--refresh-package",
        "--reinstall-package",
        "--upgrade-package",
        "--exclude-newer",
        "--prerelease",
        "--resolution",
        "--link-mode",
        "--color",
        "--native-tls",
        "--from",
    }
)
_MAKE_VALUE_FLAGS: Final = frozenset({"-C", "-f", "--file", "--directory", "-j", "--jobs", "-l"})
_TIMEOUT_VALUE_FLAGS: Final = frozenset({"-s", "--signal", "-k", "--kill-after"})
_FIX_FLAGS: Final = frozenset(
    {
        "--fix",
        "--fix-only",
        "--unsafe-fixes",
        "-a",
        "-A",
        "--autocorrect",
        "--autocorrect-all",
        "--write",
        "--apply",
        "--apply-unsafe",
        "-u",
        "--updateSnapshot",
        "--update-snapshots",
        "--snapshot-update",
        "--update",
    }
)


@dataclass(slots=True)
class _Simple:
    words: list[str] = field(default_factory=list[str])
    env: dict[str, str] = field(default_factory=dict[str, str])
    writes: list[str] = field(default_factory=list[str])
    heredoc: bool = False
    negated: bool = False
    subshell: bool = False


@dataclass(slots=True)
class _Pipeline:
    commands: list[_Simple] = field(default_factory=list[_Simple])
    op_after: str | None = None


@dataclass(slots=True)
class _Classified:
    category: Category
    program: str
    runner: str | None = None
    filters: list[str] = field(default_factory=list[str])
    collect_only: bool = False
    mutation: str | None = None
    deleted: list[str] = field(default_factory=list[str])
    touched: list[str] | None = None
    """Files the program writes, when its command line names them all; None to take them from
    its arguments."""
    opaque: str | None = None
    """Why the program may write files its command line does not name."""
    paths: list[str] | None = None
    """The paths a test run selects when plain paths are its only filters."""
    nested: CommandAnalysis | None = None


def _split_operators(token: str) -> Iterator[str]:
    rest = token
    while rest:
        op = next((o for o in _OPERATORS if rest.startswith(o)), rest[0])
        yield op
        rest = rest[len(op) :]


def tokenize(command: str) -> list[str]:
    """Shell words and operators; raises ValueError for unbalanced quotes."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    tokens: list[str] = []
    for token in lexer:
        if token and set(token) <= set("();<>|&\n"):
            tokens.extend(_split_operators(token))
        else:
            tokens.append(token)
    return tokens


def _strip_comments(tokens: list[str]) -> list[str]:
    out: list[str] = []
    skipping = False
    for tok in tokens:
        if tok == "\n":
            skipping = False
        elif tok.startswith("#") and (not out or out[-1] in _SEPARATORS | _PIPES):
            skipping = True
        if not skipping:
            out.append(tok)
    return out


def _parse(tokens: list[str]) -> list[_Pipeline]:
    pipelines: list[_Pipeline] = [_Pipeline()]
    current = _Simple()
    heredoc_end: str | None = None
    pending_heredoc: str | None = None
    depth = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if heredoc_end is not None:
            if tok == heredoc_end and (i + 1 == len(tokens) or tokens[i + 1] == "\n"):
                heredoc_end = None
            i += 1
            continue
        if tok == "\n" and pending_heredoc is not None:
            heredoc_end, pending_heredoc = pending_heredoc, None
            i += 1
            continue
        if tok in _HEREDOCS:
            current.heredoc = True
            pending_heredoc = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
            continue
        if tok in _REDIRECTS:
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if current.words and current.words[-1].isdigit():
                current.words.pop()
            fd_dup = tok == ">&" and target.isdigit()
            if tok not in ("<", "<<<") and not fd_dup and target not in _SAFE_TARGETS | {"-"}:
                current.writes.append(target)
            i += 2
            continue
        if tok in ("(", ")"):
            depth = depth + 1 if tok == "(" else max(depth - 1, 0)
            current.subshell = current.subshell or depth > 0
            i += 1
            continue
        if tok in _PIPES or tok in _SEPARATORS:
            pipelines[-1].commands.append(current)
            current = _Simple()
            if tok in _SEPARATORS:
                pipelines[-1].op_after = tok
                pipelines.append(_Pipeline())
            i += 1
            continue
        current.subshell = current.subshell or depth > 0
        if not current.words and tok == "!":
            current.negated = True
        elif not current.words and _ASSIGNMENT.match(tok):
            name, _, value = tok.partition("=")
            current.env[name] = value
        else:
            current.words.append(tok)
        i += 1
    pipelines[-1].commands.append(current)
    cleaned: list[_Pipeline] = []
    for pipeline in pipelines:
        commands = [c for c in pipeline.commands if c.words or c.writes or c.env]
        if commands:
            cleaned.append(_Pipeline(commands, pipeline.op_after))
        elif cleaned and pipeline.op_after is not None:
            cleaned[-1].op_after = pipeline.op_after
    return cleaned


def program_name(word: str) -> str:
    return posixpath.basename(word)


def _flags_then_rest(words: Sequence[str], value_flags: frozenset[str]) -> list[str]:
    """Drop leading options (and their values) and return the remaining words."""
    i = 0
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        flag = words[i]
        i += 1
        if "=" not in flag and flag in value_flags:
            i += 1
    if i < len(words) and words[i] == "--":
        i += 1
    return list(words[i:])


def _unwrap(words: list[str]) -> list[str]:
    """Strip wrapper programs that run another command (`timeout 60 pytest`, `uv run pytest`)."""
    for _ in range(MAX_NESTING * 2):
        if not words:
            return words
        prog = program_name(words[0])
        rest = words[1:]
        if prog in ("time", "nohup", "exec", "builtin", "caffeinate", "sudo", "doas"):
            words = _flags_then_rest(rest, frozenset({"-u", "-g", "-C"}))
        elif prog == "command" and rest and rest[0] not in ("-v", "-V"):
            words = _flags_then_rest(rest, frozenset())
        elif prog == "env" and rest:
            rest = _flags_then_rest(rest, frozenset({"-u", "-C", "-S"}))
            while rest and _ASSIGNMENT.match(rest[0]):
                rest = rest[1:]
            if not rest:
                return words
            words = rest
        elif prog == "timeout":
            rest = _flags_then_rest(rest, _TIMEOUT_VALUE_FLAGS)
            words = rest[1:]
        elif prog == "nice":
            words = _flags_then_rest(rest, frozenset({"-n", "--adjustment"}))
        elif prog in ("stdbuf", "ionice", "chrt", "taskset"):
            words = _flags_then_rest(rest, frozenset({"-c", "-n", "-p"}))
        elif prog == "xvfb-run":
            words = _flags_then_rest(rest, frozenset({"-n", "-s", "-e", "-f", "-p", "-w"}))
        elif prog in ("uv", "poetry", "pipenv", "pdm", "rye", "hatch") and rest[:1] == ["run"]:
            words = _flags_then_rest(rest[1:], _UV_VALUE_FLAGS)
        elif prog in ("uvx", "pipx") and (prog == "uvx" or rest[:1] == ["run"]):
            words = _flags_then_rest(rest if prog == "uvx" else rest[1:], _UV_VALUE_FLAGS)
        elif prog in ("npx", "bunx", "pnpx"):
            words = _flags_then_rest(rest, frozenset({"-p", "--package", "-c", "--call"}))
        elif prog in ("npm", "pnpm", "yarn") and rest[:1] in (["exec"], ["dlx"], ["x"]):
            words = _flags_then_rest(rest[1:], frozenset({"-p", "--package", "-c", "--call"}))
        elif prog == "bundle" and rest[:1] == ["exec"]:
            words = rest[1:]
        elif prog == "dotenv" and "--" in rest:
            words = rest[rest.index("--") + 1 :]
        elif _PYTHON.match(prog) and len(rest) >= 2 and rest[0] == "-m":
            words = [rest[1], *rest[2:]]
        elif _PYTHON.match(prog) and rest[:1] and rest[0].startswith("-m") and len(rest[0]) > 2:
            words = [rest[0][2:], *rest[1:]]
        else:
            return words
    return words


def _pytest_filters(
    args: list[str], env: dict[str, str]
) -> tuple[list[str], bool, list[str] | None]:
    """(filters, collect only, paths): `paths` are the files and directories the run selects
    when its only filters are plain paths, else None."""
    filters: list[str] = []
    paths: list[str] = []
    plain = True
    collect_only = False
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, _ = arg.partition("=")
        if arg in (
            "--co",
            "--collect-only",
            "--fixtures",
            "--markers",
            "--version",
            "-h",
            "--help",
            "--setup-plan",
        ):
            collect_only = True
        elif arg == "-p" and args[i + 1 : i + 2] in _HARMLESS_PLUGIN_SWITCHES:
            i += 1
        elif arg.startswith("-") and not eq and arg in _PYTEST_FILTER_FLAGS:
            filters.append(_PYTEST_FILTER_FLAGS[arg])
            plain = False
            i += 1
        elif eq and name in _PYTEST_FILTER_FLAGS:
            filters.append(_PYTEST_FILTER_FLAGS[name])
            plain = False
        elif arg in _PYTEST_FILTER_SWITCHES:
            filters.append(_PYTEST_FILTER_SWITCHES[arg])
            plain = False
        elif [arg[2:]] in _HARMLESS_PLUGIN_SWITCHES and arg.startswith("-p"):
            pass
        elif re.fullmatch(r"-k.+|-m.+|-p.+|-c.+|-o.+", arg):
            filters.append(_PYTEST_FILTER_FLAGS[arg[:2]])
            plain = False
        elif arg.startswith("-"):
            if not eq and arg in _PYTEST_VALUE_FLAGS:
                i += 1
        else:
            paths.append(arg)
            plain = plain and "::" not in arg and not _unspelled(arg)
            if arg not in (".", "./"):
                filters.append(f"only {arg}")
        i += 1
    if "PYTEST_ADDOPTS" in env:
        filters.append("PYTEST_ADDOPTS")
        plain = False
    return filters, collect_only, paths if plain else None


def _unittest_filters(args: list[str]) -> tuple[list[str], bool]:
    if not args or args[0] == "discover":
        rest = args[1:] if args else []
        filters = [f"discover {a}" for a in rest if a in ("-p", "--pattern", "-k")]
        return filters, False
    if args[0] in ("-h", "--help"):
        return [], True
    positional = [a for a in args if not a.startswith("-")]
    filters = [f"only {a}" for a in positional]
    if "-k" in args:
        filters.append("-k")
    return filters, False


def _js_filters(runner: str, args: list[str]) -> tuple[list[str], bool]:
    filters: list[str] = []
    collect_only = False
    if runner == "vitest" and args[:1] == ["related"]:
        filters.append("vitest related")
    if args[:1] and args[0] in _VITEST_SUBCOMMANDS | {"related", "test"}:
        args = args[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, _ = arg.partition("=")
        if name in _JS_COLLECT:
            collect_only = True
        elif name in _JS_FILTER_FLAGS:
            filters.append(name)
            if not eq:
                i += 1
        elif name in _JS_FILTER_SWITCHES:
            filters.append(name)
        elif arg.startswith("-"):
            if not eq and arg in _JS_VALUE_FLAGS:
                i += 1
        else:
            filters.append(f"only {arg}")
        i += 1
    return filters, collect_only


def _go_filters(args: list[str]) -> tuple[list[str], bool]:
    filters: list[str] = []
    packages: list[str] = []
    collect_only = False
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, _ = arg.partition("=")
        flag = name.replace("--", "-", 1) if name.startswith("--") else name
        if flag in ("-run", "-skip"):
            filters.append(flag)
            if not eq:
                i += 1
        elif flag == "-short":
            filters.append("-short")
        elif flag == "-list":
            collect_only = True
            if not eq:
                i += 1
        elif arg.startswith("-"):
            if not eq and flag in _GO_VALUE_FLAGS:
                i += 1
        else:
            packages.append(arg)
        i += 1
    if not packages:
        filters.append("only the current package (no ./...)")
    filters.extend(f"only {p}" for p in packages if not p.endswith("..."))
    return filters, collect_only


def _cargo_filters(args: list[str]) -> tuple[list[str], bool]:
    filters: list[str] = []
    collect_only = False
    binary: list[str] = []
    if "--" in args:
        split = args.index("--")
        args, binary = args[:split], args[split + 1 :]
    if args[:1] == ["run"]:
        args = args[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, _ = arg.partition("=")
        if name in _CARGO_FILTER_FLAGS:
            filters.append(name)
            if not eq:
                i += 1
        elif name in _CARGO_FILTER_SWITCHES:
            filters.append(name)
        elif name in ("--no-run", "--list"):
            collect_only = True
        elif arg.startswith("-"):
            if not eq and name in _CARGO_VALUE_FLAGS:
                i += 1
        else:
            filters.append(f"only tests matching {arg}")
        i += 1
    for arg in binary:
        if arg in _TEST_BINARY_FILTERS:
            filters.append(arg)
        elif arg == "--list":
            collect_only = True
        elif not arg.startswith("-") and not arg.isdigit():
            filters.append(f"only tests matching {arg}")
    return filters, collect_only


def _generic_filters(label: str, args: list[str]) -> list[str]:
    """Arguments passed to a test script whose runner is unknown narrow it, conservatively."""
    meaningful = [a for a in args if a != "--" and a not in ("--silent", "-s", "--if-present")]
    return [f"arguments passed to {label}: {' '.join(meaningful)}"] if meaningful else []


def _pm_script(pm: str, words: list[str]) -> _Classified:
    rest = [
        w for w in words[1:] if w not in ("--silent", "-s", "--if-present", "-w", "--workspaces")
    ]
    if not rest:
        return _Classified(Category.MUTATING, pm, mutation=f"{pm} with no arguments installs")
    sub = rest[0]
    args = rest[1:]
    if sub in ("run", "run-script", "rum", "urn"):
        if not args:
            return _Classified(Category.READ_ONLY, pm)
        sub, args = args[0], args[1:]
    elif sub in ("t", "tst"):
        sub = "test"
    elif sub in _PM_INSTALL:
        return _Classified(Category.MUTATING, pm, mutation=f"{pm} {sub} changes dependencies")
    elif sub in _PM_READ_ONLY:
        return _Classified(Category.READ_ONLY, pm)
    elif pm == "bun" and sub == "test":
        filters, collect = _js_filters("bun", args)
        return _Classified(Category.TEST, pm, "bun test", filters, collect)
    label = f"{pm} {sub}" if sub == "test" else f"{pm} run {sub}"
    if sub == "test" or sub.startswith("test:"):
        filters = _generic_filters(label, args)
        if sub != "test":
            filters.insert(0, f"script {sub} (not the project's test script)")
        return _Classified(Category.TEST, pm, f"{pm} test", filters)
    if _BUILD_SCRIPTS.match(sub):
        return _Classified(Category.BUILD, pm)
    if _LINT_SCRIPTS.match(sub):
        fix = any(a in _FIX_FLAGS for a in args) or "fix" in sub
        return _Classified(
            Category.LINT, pm, mutation=f"{label} --fix rewrites files" if fix else None
        )
    if _TYPECHECK_SCRIPTS.match(sub):
        return _Classified(Category.TYPECHECK, pm)
    if _FORMAT_SCRIPTS.match(sub):
        checking = "check" in sub or any("check" in a for a in args)
        return _Classified(
            Category.FORMAT, pm, mutation=None if checking else f"{label} rewrites files"
        )
    return _Classified(Category.UNKNOWN, pm, mutation=f"{label} runs a project script")


def _make_like(prog: str, words: list[str]) -> _Classified:
    targets = [w for w in _flags_then_rest(words[1:], _MAKE_VALUE_FLAGS) if "=" not in w]
    if any(w in ("-n", "--dry-run", "--just-print") for w in words[1:]):
        return _Classified(Category.READ_ONLY, prog)
    if not targets:
        return _Classified(Category.BUILD, prog, mutation=f"{prog} runs the default target")
    target = targets[0]
    label = f"{prog} {target}"
    if target in _TEST_TARGETS or target.startswith(("test-", "test_", "tests-")):
        filters = [] if target in _TEST_TARGETS else [f"target {target}"]
        filters.extend(f"also runs {t}" for t in targets[1:])
        return _Classified(Category.TEST, prog, label, filters)
    if target in ("build", "all", "compile", "dist"):
        return _Classified(Category.BUILD, prog)
    if target in ("lint", "check-lint", "ruff", "flake8"):
        return _Classified(Category.LINT, prog)
    if target in ("typecheck", "type-check", "types", "mypy", "pyright", "tsc"):
        return _Classified(Category.TYPECHECK, prog)
    if target in ("fmt", "format"):
        return _Classified(Category.FORMAT, prog, mutation=f"{label} rewrites files")
    return _Classified(Category.UNKNOWN, prog, mutation=f"{label} runs a project target")


def _maybe_directory(path: str) -> bool:
    """A path that may name a directory (and so files inside it the command line does not
    name): a trailing slash, `.`, a glob, or a last component without an extension."""
    name = posixpath.basename(path.rstrip("/"))
    return path.endswith("/") or name in ("", ".", "..", "*") or "." not in name.lstrip(".")


def _git_paths(sub: str, rest: list[str]) -> list[str] | None:
    """The files `git checkout`/`restore` rewrite when its command line names them, else None
    (a branch switch, the whole tree, or a directory)."""
    if "--" in rest:
        paths = rest[rest.index("--") + 1 :]
    elif sub == "restore":
        paths = _positional(rest, frozenset({"-s", "--source"}))
    else:
        return None
    if not paths or any(_maybe_directory(p) or _unspelled(p) for p in paths):
        return None
    return paths


def _positional(args: list[str], value_flags: frozenset[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and arg != "-":
            if "=" not in arg and arg in value_flags:
                i += 1
        else:
            out.append(arg)
        i += 1
    return out


def _git(words: list[str]) -> _Classified:
    args = _flags_then_rest(words[1:], frozenset({"-C", "-c", "--git-dir", "--work-tree"}))
    sub = args[0] if args else ""
    rest = args[1:]
    if sub not in _GIT_MUTATING:
        return _Classified(Category.READ_ONLY, "git")
    if sub == "stash" and rest[:1] in (["list"], ["show"]):
        return _Classified(Category.READ_ONLY, "git")
    if sub == "worktree" and rest[:1] == ["list"]:
        return _Classified(Category.READ_ONLY, "git")
    mutation = f"git {sub} changes the working tree"
    named = _positional(rest, frozenset({"-m", "-s", "--source"}))
    if sub == "rm":
        return _Classified(Category.MUTATING, "git", mutation=mutation, deleted=named, touched=[])
    if sub == "mv":
        return _Classified(
            Category.MUTATING, "git", mutation=mutation, deleted=named[:-1], touched=named[-1:]
        )
    if sub in ("init", "update-index") or (
        sub == "reset" and not {"--hard", "--merge", "--keep"} & set(rest)
    ):
        return _Classified(Category.MUTATING, "git", mutation=mutation, touched=[])
    new_branch = {"checkout": ("-b", "-B"), "switch": ("-c", "-C")}.get(sub, ())
    if rest[:1] and rest[0] in new_branch and len(named) == 1:
        return _Classified(Category.MUTATING, "git", mutation=mutation, touched=[])
    if sub in ("checkout", "restore") and (paths := _git_paths(sub, rest)) is not None:
        return _Classified(Category.MUTATING, "git", mutation=mutation, touched=paths)
    return _Classified(
        Category.MUTATING,
        "git",
        mutation=mutation,
        touched=[],
        opaque=f"git {sub} can rewrite any file in the working tree",
    )


def _unspelled(path: str) -> bool:
    """A path the shell builds at run time (a variable, a command substitution, a glob)."""
    return re.search(r"[$`*?\[]", path) is not None


def _sed(args: list[str]) -> _Classified:
    in_place = False
    script_given = False
    files: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "-f", "--expression", "--file", "-l", "--line-length"):
            script_given = script_given or arg not in ("-l", "--line-length")
            i += 2
            continue
        if arg.startswith(("--expression=", "--file=")):
            script_given = True
        elif arg.startswith("--in-place"):
            in_place = True
        elif arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            short = arg[1:]
            if "i" in short:
                in_place = True
                if short.endswith("i") and i + 1 < len(args) and args[i + 1] == "":
                    i += 1
            if short[-1:] in ("e", "f") and "i" not in short:
                script_given = True
                i += 1
        elif not script_given:
            script_given = True
        else:
            files.append(arg)
        i += 1
    if not in_place:
        return _Classified(Category.READ_ONLY, "sed")
    return _Classified(Category.MUTATING, "sed", mutation="sed -i edits files", touched=files)


def _perl(args: list[str]) -> _Classified:
    in_place = any(a.startswith("-") and not a.startswith("--") and "i" in a[1:] for a in args)
    scripts = [i for i, a in enumerate(args) if re.fullmatch(r"-[a-zA-Z0-9]*[eE]", a)]
    if not in_place or not scripts:
        return _Classified(
            Category.MUTATING if in_place else Category.UNKNOWN,
            "perl",
            mutation="perl -i edits files" if in_place else "perl runs a program",
        )
    skip = {i + 1 for i in scripts}
    files = [a for i, a in enumerate(args) if i not in skip and not a.startswith("-")]
    return _Classified(Category.MUTATING, "perl", mutation="perl -i edits files", touched=files)


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
_SCRATCH: Final = ("/tmp/", "/private/tmp/", "/var/folders/", "/dev/")  # noqa: S108
_VERSION_FLAGS: Final = frozenset({"--version", "-V", "-VV", "--help", "-h"})
_PYTHON_SIDE_EFFECTS: Final = re.compile(
    r"import|open|exec|eval|compile|__|getattr|setattr|delattr|globals|locals|vars|"
    r"breakpoint|help|input|memoryview"
)
"""Without these a `python -c` program reaches no module, file or builtin that can write."""


def _classify_program(
    words: list[str], env: dict[str, str], depth: int, cwd: str | None
) -> _Classified:
    prog = program_name(words[0])
    args = words[1:]
    if prog in _SHELLS and depth < MAX_NESTING:
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
            return _Classified(category, prog, nested=nested)
        return _Classified(Category.UNKNOWN, prog, mutation=f"{prog} runs a script")
    if prog in ("pytest", "py.test"):
        filters, collect, paths = _pytest_filters(args, env)
        return _Classified(Category.TEST, prog, "pytest", filters, collect, paths=paths)
    if prog == "unittest":
        filters, collect = _unittest_filters(args)
        return _Classified(Category.TEST, prog, "unittest", filters, collect)
    if prog in ("jest", "vitest", "mocha", "ava", "tap", "jasmine"):
        filters, collect = _js_filters(prog, args)
        if prog == "vitest" and args[:1] in (["bench"], ["list"]):
            collect = True
        mutation = (
            "updates snapshots"
            if any(a in ("-u", "--updateSnapshot", "--update") for a in args)
            else None
        )
        return _Classified(
            Category.TEST,
            prog,
            prog,
            filters,
            collect,
            mutation=mutation,
            opaque=f"{prog} -u rewrites the expected snapshots" if mutation else None,
        )
    if prog == "playwright" and args[:1] == ["test"]:
        filters, collect = _js_filters("playwright", args[1:])
        return _Classified(Category.TEST, prog, "playwright test", filters, collect)
    if prog == "cypress" and args[:1] == ["run"]:
        filters, collect = _js_filters("cypress", args[1:])
        return _Classified(Category.TEST, prog, "cypress run", filters, collect)
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
        return _Classified(Category.TEST, prog, prog, filters)
    if prog in ("tox", "nox"):
        filters = [
            a.partition("=")[0]
            for a in args
            if a.partition("=")[0] in ("-e", "-s", "--session", "--sessions", "-k", "-t", "--tags")
        ]
        if "--" in args and args[args.index("--") + 1 :]:
            filters.append(f"arguments passed through {prog}")
        return _Classified(Category.TEST, prog, prog, filters)
    if prog == "go":
        sub = args[0] if args else ""
        if sub == "test":
            filters, collect = _go_filters(args[1:])
            return _Classified(Category.TEST, prog, "go test", filters, collect)
        if sub in ("build", "install"):
            return _Classified(Category.BUILD, prog)
        if sub == "vet":
            return _Classified(Category.LINT, prog)
        if sub in ("fmt", "generate", "get") or args[:2] == ["mod", "tidy"]:
            return _Classified(Category.MUTATING, prog, mutation=f"go {sub} changes files")
        if sub in ("version", "env", "list", "doc", "help") or args[:2] == ["mod", "graph"]:
            return _Classified(Category.READ_ONLY, prog)
        return _Classified(Category.UNKNOWN, prog, mutation=f"go {sub} runs a program")
    if prog == "cargo":
        sub = args[0] if args else ""
        if sub in ("test", "t"):
            filters, collect = _cargo_filters(args[1:])
            return _Classified(Category.TEST, prog, "cargo test", filters, collect)
        if sub == "nextest":
            filters, collect = _cargo_filters(args[1:])
            return _Classified(Category.TEST, prog, "cargo nextest", filters, collect)
        if sub in ("build", "b"):
            return _Classified(Category.BUILD, prog)
        if sub in ("check", "c"):
            return _Classified(Category.TYPECHECK, prog)
        if sub == "clippy":
            fix = "--fix" in args
            return _Classified(
                Category.LINT, prog, mutation="clippy --fix rewrites files" if fix else None
            )
        if sub == "fmt":
            check = "--check" in args
            return _Classified(
                Category.FORMAT, prog, mutation=None if check else "cargo fmt rewrites files"
            )
        if sub in ("tree", "metadata", "search", "version", "--version", "help", "doc"):
            return _Classified(Category.READ_ONLY, prog)
        return _Classified(Category.UNKNOWN, prog, mutation=f"cargo {sub} may change files")
    if prog in _PM:
        return _pm_script(prog, words)
    if prog in ("make", "gmake", "just", "task", "mage"):
        return _make_like(prog, words)
    if prog in ("mvn", "mvnw", "gradle", "gradlew"):
        goals = [a for a in args if not a.startswith("-")]
        filters = [
            a.partition("=")[0]
            for a in args
            if a.startswith(("-Dtest", "-pl", "--tests", "--projects"))
        ]
        if any(g in ("test", "verify", "check", "integration-test") for g in goals):
            return _Classified(Category.TEST, prog, prog, filters)
        if any(g in ("build", "package", "compile", "install", "assemble") for g in goals):
            return _Classified(Category.BUILD, prog)
        return _Classified(Category.UNKNOWN, prog, mutation=f"{prog} runs a project task")
    if prog == "dotnet":
        sub = args[0] if args else ""
        if sub == "test":
            filters = [a for a in args[1:] if a in ("--filter",)] + [
                f"only {a}" for a in args[1:2] if not a.startswith("-")
            ]
            return _Classified(Category.TEST, prog, "dotnet test", filters)
        if sub == "build":
            return _Classified(Category.BUILD, prog)
        if sub == "format":
            return _Classified(
                Category.FORMAT,
                prog,
                mutation=None if "--verify-no-changes" in args else "dotnet format rewrites files",
            )
        return _Classified(Category.UNKNOWN, prog, mutation=f"dotnet {sub} may change files")
    if prog in ("deno", "swift", "mix", "bazel", "bazelisk"):
        sub = args[0] if args else ""
        if sub == "test":
            positional = [a for a in args[1:] if not a.startswith("-")]
            filters = [f"only {a}" for a in positional]
            filters.extend(
                a for a in args[1:] if a.partition("=")[0] in ("--filter", "--only", "--exclude")
            )
            return _Classified(Category.TEST, prog, f"{prog} test", filters)
        if sub in ("build", "compile"):
            return _Classified(Category.BUILD, prog)
        if sub == "lint":
            return _Classified(Category.LINT, prog)
        if sub == "check":
            return _Classified(Category.TYPECHECK, prog)
        if sub in ("fmt", "format"):
            return _Classified(
                Category.FORMAT,
                prog,
                mutation=None if "--check" in args else f"{prog} {sub} rewrites files",
            )
        return _Classified(Category.UNKNOWN, prog, mutation=f"{prog} {sub} may change files")
    if prog == "hatch" and args[:1] == ["test"]:
        return _Classified(
            Category.TEST, prog, "hatch test", _generic_filters("hatch test", args[1:])
        )
    if prog == "ruff":
        sub = args[0] if args else ""
        if sub == "format":
            check = any(a in ("--check", "--diff") for a in args)
            return _Classified(
                Category.FORMAT, prog, mutation=None if check else "ruff format rewrites files"
            )
        fix = any(
            a.partition("=")[0] in ("--fix", "--unsafe-fixes", "--fix-only")
            and not a.endswith("=false")
            for a in args
        )
        return _Classified(
            Category.LINT, prog, mutation="ruff --fix rewrites files" if fix else None
        )
    if prog in ("black", "isort", "autopep8", "yapf", "autoflake"):
        check = any(a in ("--check", "--diff", "--check-only", "-c") for a in args)
        if prog in ("autopep8", "yapf", "autoflake"):
            check = not any(a in ("-i", "--in-place") for a in args)
        return _Classified(
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
        return _Classified(category, prog, mutation=f"{prog} rewrites files" if writes else None)
    if prog in ("gofmt", "goimports", "rustfmt", "clang-format", "shfmt", "terraform"):
        writes = any(a in ("-w", "-i", "--write") for a in args) or (
            prog == "rustfmt" and "--check" not in args
        )
        if prog == "terraform":
            writes = args[:1] == ["fmt"] and "-check" not in args
        return _Classified(
            Category.FORMAT, prog, mutation=f"{prog} rewrites files" if writes else None
        )
    if prog == "pre-commit":
        return _Classified(Category.LINT, prog, mutation="pre-commit hooks may rewrite files")
    if prog in _LINTERS:
        fix = any(a.partition("=")[0] in _FIX_FLAGS for a in args)
        return _Classified(
            Category.LINT, prog, mutation=f"{prog} fixes rewrite files" if fix else None
        )
    if prog in _TYPECHECKERS:
        return _Classified(Category.TYPECHECK, prog)
    if prog == "tsc":
        no_emit = "--noEmit" in args or "--noemit" in args
        return _Classified(Category.TYPECHECK if no_emit else Category.BUILD, prog)
    if prog in ("vite", "next", "nuxt", "astro", "svelte-kit", "ng") and args[:1] == ["build"]:
        return _Classified(Category.BUILD, prog)
    if (
        prog in _BUILDERS
        or (prog == "docker" and args[:1] == ["build"])
        or (prog == "cmake" and "--build" in args)
    ):
        return _Classified(Category.BUILD, prog)
    if prog in ("build",) or (
        prog in ("uv", "poetry", "hatch", "pdm", "flit") and args[:1] == ["build"]
    ):
        return _Classified(Category.BUILD, prog)
    if prog == "uv":
        sub = args[0] if args else ""
        if sub in ("sync", "add", "remove", "lock", "venv", "init", "self", "tool") or args[:2] in (
            ["pip", "install"],
            ["pip", "uninstall"],
            ["pip", "sync"],
        ):
            return _Classified(
                Category.MUTATING, prog, mutation=f"uv {sub} changes the environment"
            )
        return (
            _Classified(Category.READ_ONLY, prog)
            if sub in ("pip", "tree", "version", "--version", "help", "python")
            else _Classified(Category.UNKNOWN, prog, mutation=f"uv {sub} may change files")
        )
    if prog in ("pip", "pip3"):
        sub = args[0] if args else ""
        if sub in ("install", "uninstall", "download", "wheel"):
            return _Classified(
                Category.MUTATING, prog, mutation=f"pip {sub} changes the environment"
            )
        return _Classified(Category.READ_ONLY, prog)
    if prog == "git":
        return _git(words)
    if prog == "sed":
        return _sed(args)
    if prog == "perl":
        return _perl(args)
    if prog in ("awk", "gawk"):
        program_text = " ".join(args)
        writes = (
            "system(" in program_text or re.search(r"print[^|;]*>\s*\"", program_text) is not None
        )
        return _Classified(
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
        return _Classified(
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
        return _Classified(
            Category.MUTATING if writes else Category.READ_ONLY,
            prog,
            mutation="curl writes a file" if writes else None,
            touched=named,
            opaque="curl -O names the file after the URL" if remote else None,
        )
    if prog in ("rm", "unlink", "shred"):
        deleted = [a for a in args if not a.startswith("-")]
        return _Classified(
            Category.MUTATING, prog, mutation=f"{prog} deletes files", deleted=deleted
        )
    if prog in _COPIERS:
        named = [a for a in args if not a.startswith("-")]
        target = named[-1:] or [""]
        into_directory = (
            _maybe_directory(target[0]) and not target[0].startswith(_SCRATCH)
        ) or any(a in ("-t", "--target-directory") or a.startswith("-t") for a in args)
        return _Classified(
            Category.MUTATING,
            prog,
            mutation="mv moves files" if prog == "mv" else f"{prog} changes files",
            deleted=named[:-1] if prog == "mv" else [],
            touched=named if prog == "mv" else target,
            opaque=f"{prog} writes into a directory" if into_directory else None,
        )
    if prog in _OPAQUE_WRITERS:
        return _Classified(
            Category.MUTATING,
            prog,
            mutation=f"{prog} changes files",
            opaque=f"{prog} writes files its command line does not name",
        )
    if prog in _MUTATING:
        return _Classified(Category.MUTATING, prog, mutation=f"{prog} changes files")
    if prog in ("xargs", "parallel", "watch", "eval", "source", "."):
        return _Classified(Category.UNKNOWN, prog, mutation=f"{prog} runs other commands")
    if prog in READ_ONLY_PROGRAMS:
        return _Classified(Category.READ_ONLY, prog)
    if "/" not in words[0] and len(args) == 1 and args[0] in _VERSION_FLAGS:
        return _Classified(Category.READ_ONLY, prog)
    if _PYTHON.match(prog) and args[:1] == ["-c"] and len(args) >= 2:
        if _PYTHON_SIDE_EFFECTS.search(args[1]) is None:
            return _Classified(Category.READ_ONLY, prog)
        return _Classified(Category.UNKNOWN, prog, mutation="python -c runs code that can write")
    return _Classified(Category.UNKNOWN, prog, mutation=f"{prog} is not a known read-only program")


def _classify(simple: _Simple, depth: int, cwd: str | None) -> _Classified:
    words = _unwrap(list(simple.words))
    if not words:
        result = _Classified(Category.READ_ONLY, "")
    else:
        result = _classify_program(words, simple.env, depth, cwd)
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
            if is_harness_env(name) and name != "PYTEST_ADDOPTS" and not _harmless_env(name, value)
        ]
        result.filters.extend(f"runs with {name} changed" for name in changed)
        if changed:
            result.paths = None
    named = result.touched
    if named is None:
        mutating = result.category is Category.MUTATING and not read_only
        named = [w for w in words[1:] if not w.startswith("-")] if mutating else []
    result.touched = [*named, *simple.writes]
    if result.opaque is None and result.mutation is not None:
        if result.category in (Category.UNKNOWN, Category.BUILD):
            result.opaque = result.mutation
        elif built := next((p for p in result.touched if _unspelled(p)), None):
            result.opaque = f"writes {built}, a path the shell builds at run time"
    return result


HARNESS_ENV: Final = frozenset(
    {"PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
     "NODE_OPTIONS", "NODE_PATH", "GOFLAGS", "RUSTFLAGS", "RUSTC_WRAPPER", "LD_PRELOAD",
     "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "VIRTUAL_ENV", "BASH_ENV",
     "ENV", "JEST_CONFIG", "VITEST_CONFIG", "PYTHONOPTIMIZE", "PYTHONWARNINGS", "PYTHONUSERBASE",
     "PYTHONNOUSERSITE", "PYTHONSAFEPATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
     "MAVEN_OPTS", "GRADLE_OPTS", "RUSTC", "CARGO_BUILD_RUSTC_WRAPPER", "GOTOOLCHAIN"}
)  # fmt: skip
"""Variables that change which program runs, or how a test runner behaves."""
_HARNESS_ENV_PREFIX: Final = re.compile(
    r"^(?:PYTEST_|_PYTEST|JEST_|VITEST_|MOCHA_|NODE_TEST|CARGO_TARGET_\w+_RUNNER$|NEXTEST_)"
)


def is_harness_env(name: str) -> bool:
    return name in HARNESS_ENV or _HARNESS_ENV_PREFIX.match(name) is not None


_PROJECT_DIR: Final = re.compile(
    r"^(?:\.|\./|\$PWD|\$\{PWD\}|\$\(pwd\)|`pwd`)(?::\$\{?PYTHONPATH\}?)?$"
)


def _harmless_env(name: str, value: str) -> bool:
    """`PYTHONPATH=.` (or `$PWD`) adds the directory `python -m pytest` already imports from."""
    return name == "PYTHONPATH" and _PROJECT_DIR.match(value) is not None


def _sources(simple: _Simple) -> str | None:
    """Why a simple command sets variables the command line does not name, or None."""
    words = simple.words
    prog = program_name(words[0]) if words else ""
    if prog in ("source", ".") and words[1:]:
        return f"{prog} {words[1]} sets whatever variables the file holds"
    if prog == "eval":
        return "eval runs text built at run time"
    if prog == "set" and (
        "allexport" in words or any(re.fullmatch(r"-[a-z]*a[a-z]*", w) for w in words[1:])
    ):
        return "set -a exports every variable set afterwards"
    if prog in ("export", "declare", "typeset", "readonly") and any(
        _unspelled(w.partition("=")[0]) for w in words[1:] if not w.startswith("-")
    ):
        return f"{prog} sets variables named at run time"
    return None


_FUNCTION_DEF: Final = re.compile(
    r"(?:^|[;&|\n(]\s*)(?:function\s+[\w.:-]+|[\w.:-]+\s*\(\s*\))\s*\{"
)


_RUNNER_PACKAGES: Final = frozenset(
    {"pytest", "jest", "vitest", "mocha", "nose2", "tox", "nox", "pytest-asyncio", "pytest-cov"}
)
"""Packages an honest session installs to run the tests at all; anything else it installs can
register a test-runner plugin (a `pytest11` entry point, a jest preset)."""
_LAUNCH_WRAPPERS: Final = frozenset(
    {"time", "timeout", "nice", "env", "sudo", "doas", "command", "exec", "nohup", "caffeinate"}
)
_PACKAGE_SOURCE: Final = re.compile(
    r"^(?:\.{1,2}(?:/|$)|/|~|file:|git\+|(?:git|hg|svn|bzr)[+:]|https?://|github:|link:|"
    r"workspace:|portal:)|\.(?:whl|tar\.gz|tgz|zip)$|\s@\s",
    re.IGNORECASE,
)
_PROJECT_ITSELF: Final = re.compile(r"^\.(?:/)?(?:\[[\w,.-]+\])?$")
_INSTALL_VALUE_FLAGS: Final = frozenset(
    {"-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url",
     "-f", "--find-links", "--target", "-t", "--prefix", "--root", "--python", "-p",
     "--group", "--optional", "--extra", "--tag", "--registry", "--save-prefix", "-w",
     "--workspace", "--filter", "--dev-dep"}
)  # fmt: skip


def _foreign_source(value: str) -> bool:
    """A package spec naming a path, an archive, a URL or a VCS repository, not a registry name
    (`@scope/name` is a registry name)."""
    return _PACKAGE_SOURCE.search(value) is not None or ("/" in value and not value.startswith("@"))


def _package_word(value: str, why: str) -> str | None:
    """Why installing `value` changes the test harness, or None for the project itself and a
    test runner from the registry."""
    if _PROJECT_ITSELF.match(value):
        return None
    if _foreign_source(value):
        return f"{why} {value}, a local or VCS package that can hook into the test runner"
    name = re.split(r"[\[<>=!~;@ ]", value, maxsplit=1)[0].casefold()
    if name in _RUNNER_PACKAGES:
        return None
    return f"{why} {value}, a package the session adds, which can register a test-runner plugin"


def _install_targets(args: list[str]) -> list[tuple[str, bool]]:
    """The packages an install names, each with whether it is editable (`-e path`)."""
    out: list[tuple[str, bool]] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "--editable") and i + 1 < len(args):
            out.append((args[i + 1], True))
            i += 2
            continue
        if arg.startswith("--editable="):
            out.append((arg.partition("=")[2], True))
        elif arg in _INSTALL_VALUE_FLAGS:
            i += 1
        elif not arg.startswith("-"):
            out.append((arg, False))
        i += 1
    return out


def _package_change(words: list[str]) -> str | None:
    """Why a command adds code the test runner may load: `uv run --with ./plugin`, `uvx --from
    git+...`, or installing a package other than the project itself and a test runner."""
    start = 0
    while start < len(words) and (
        program_name(words[start]) in _LAUNCH_WRAPPERS
        or words[start].startswith("-")
        or _ASSIGNMENT.match(words[start])
        or words[start].replace(".", "").isdigit()
    ):
        start += 1
    words = words[start:]
    if not words:
        return None
    prog, args = program_name(words[0]), words[1:]
    if _PYTHON.match(prog) and args[:2] == ["-m", "pip"]:
        prog, args = "pip", args[2:]
    if prog in ("uv", "uvx", "pipx"):
        for i, arg in enumerate(args):
            flag, eq, inline = arg.partition("=")
            if flag not in ("--with", "--with-editable", "--with-requirements", "--from"):
                continue
            value = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
            if flag in ("--with-editable", "--with-requirements"):
                return f"{prog} {flag} {value} adds packages that can hook into the test runner"
            if _foreign_source(value):
                return f"{prog} {flag} {value}, a local or VCS package that can hook into the test runner"
    if prog == "uv" and args[:2] == ["pip", "install"]:
        prog, args = "pip", args[1:]
    adds = {
        "pip": ("install",),
        "pip3": ("install",),
        "uv": ("add",),
        "poetry": ("add",),
        "pdm": ("add",),
        "rye": ("add",),
        "npm": ("install", "i", "add", "link", "ln"),
        "pnpm": ("add", "install", "i", "link"),
        "yarn": ("add", "link"),
        "bun": ("add", "install", "i", "link"),
    }.get(prog)
    if adds is None or not args or args[0] not in adds:
        return None
    verb = f"{prog} {args[0]}"
    targets = _install_targets(args[1:])
    if prog in _PM and args[0] in ("link", "ln"):
        return f"{verb} links {targets[0][0] if targets else 'a local package'} into node_modules"
    for value, editable in targets:
        if editable and not _PROJECT_ITSELF.match(value):
            return f"{verb} -e {value}, a local package that can hook into the test runner"
        if why := _package_word(value, f"{verb} installs"):
            return why
    return None


def _harness_change(pipelines: list[_Pipeline], command: str) -> str | None:
    """Why a command changes how later commands run (aliases, functions, PATH, packages the
    runner may load), or None."""
    if _FUNCTION_DEF.search(command):
        return "defines a shell function"
    for pipeline in pipelines:
        for simple in pipeline.commands:
            words = simple.words
            if why := _package_change(list(words)):
                return why
            prog = program_name(words[0]) if words else ""
            if prog in ("alias", "unalias") and words[1:]:
                return f"{prog} {words[1].partition('=')[0]}"
            if prog == "hash" and "-p" in words:
                return "hash -p remaps a command"
            if prog in ("export", "declare", "typeset", "readonly", "set"):
                assignments = (w.partition("=") for w in words[1:] if "=" in w)
                names = [n for n, _, v in assignments if not _harmless_env(n, v)]
                if changed := [n for n in names if is_harness_env(n)]:
                    return f"{prog} {changed[0]}"
            if not words and (
                changed := [
                    n
                    for n, v in simple.env.items()
                    if is_harness_env(n) and not _harmless_env(n, v)
                ]
            ):
                return f"sets {changed[0]}"
            if why := _sources(simple):
                return why
    return None


def _writes_session_store(
    pipelines: list[_Pipeline],
    command: str,
    touched: list[str],
    deleted: list[str],
    opaque: str | None,
) -> bool:
    """The command writes a session store: it names one as a target, or it names one at all
    (quotes removed, so `~/.cl""aude` counts) and may write files it does not name. Reading
    one (`cat ~/.claude/projects/x.jsonl`) does not count."""
    if any(SESSION_STORE.search(path) for path in [*touched, *deleted]):
        return True
    words = [w for p in pipelines for c in p.commands for w in [*c.words, *c.writes]]
    named = SESSION_STORE.search(command) is not None or any(SESSION_STORE.search(w) for w in words)
    return named and opaque is not None


def _guard_tamper(
    pipelines: list[_Pipeline],
    command: str,
    touched: list[str],
    deleted: list[str],
    opaque: str | None,
) -> str | None:
    """Why the command can switch off the evidence hooks or the judge they run: it writes or
    deletes agent configuration or the judge's install, (un)installs the judge, or names one
    of those places and may write files it does not name."""
    for path in [*touched, *deleted]:
        if why := guard_path_reason(path):
            return hook_switch_reason(why, command)
        if _JUDGE_NAME.search(path) and _unspelled(path):
            return f"writes {path}, which can resolve to the evidence-judge executable"
    for pipeline in pipelines:
        for simple in pipeline.commands:
            words = _unwrap(list(simple.words))
            if (
                words
                and any(w in _PACKAGE_VERBS for w in words[1:])
                and any(_JUDGE_NAME.search(w) for w in words[1:])
            ):
                return f"{program_name(words[0])} changes the evidence-judge install the hooks run"
    if opaque is not None:
        words = [w for p in pipelines for c in p.commands for w in [*c.words, *c.writes]]
        if any(AGENT_CONFIG.search(w) or JUDGE_INSTALL.search(w) for w in words):
            return f"names agent configuration or the judge's install and {opaque}"
    return None


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
            if (m := AGENT_CONFIG.search(command) or _JUDGE_NAME.search(command))
            else None
        ),
    )


def _cd_step(simple: _Simple) -> str:
    """Where a `cd`, `pushd` or `popd` goes: its target as written, or `?` when only the shell
    knows (`popd`, `cd -`, `cd` alone, a variable)."""
    words = simple.words
    args = [w for w in words[1:] if not (w.startswith("-") and w != "-")]
    if program_name(words[0]) == "popd" or len(args) != 1:
        return "?"
    target = args[0]
    return "?" if target == "-" or target.startswith("~") or _unspelled(target) else target


def _analyse(command: str, cwd: str | None, depth: int) -> CommandAnalysis:
    if len(command) > MAX_COMMAND_CHARS:
        return _empty(f"longer than {MAX_COMMAND_CHARS} characters", command)
    try:
        pipelines = _parse(_strip_comments(tokenize(command)))
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
    cds: list[str] = []
    test_paths: list[str] | None = None
    run_cds: list[str] = []
    persisting: list[str] = []
    mutating_at: list[tuple[int, int]] = []
    nested_after = False
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
                nested_store = nested_store or nested.session_store
                nested_guard = nested_guard or nested.guard_tamper
                if nested.runners:
                    run_cds = [*cds, *nested.run_cds]
                    test_paths = nested.test_paths
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
                run_cds = list(cds)
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
    )
    if primary is not None and masked is None:
        p_index, c_index = primary
        pipeline = pipelines[p_index]
        after_pipe = pipeline.commands[c_index + 1 :]
        if after_pipe:
            output_filter = (
                program_name(_unwrap(after_pipe[0].words)[0])
                if _unwrap(after_pipe[0].words)
                else "?"
            )
            if any(program_name((_unwrap(c.words) or ["?"])[0]) != "tail" for c in after_pipe):
                output_filter = next(
                    program_name((_unwrap(c.words) or ["?"])[0])
                    for c in after_pipe
                    if program_name((_unwrap(c.words) or ["?"])[0]) != "tail"
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
        test_paths=test_paths if len(runners) == 1 else None,
        cd_targets=persisting,
        mutation_after_check=mutation_after,
        session_store=nested_store
        or _writes_session_store(pipelines, command, touched, deleted, opaque),
        harness_change=_harness_change(pipelines, command),
        guard_tamper=nested_guard or _guard_tamper(pipelines, command, touched, deleted, opaque),
    )


def analyse(command: str, cwd: str | None = None) -> CommandAnalysis:
    """Parse and classify a shell command line run in `cwd`."""
    return _analyse(command, cwd, 0)


def unwrap_shell(argv: Sequence[str]) -> str:
    """A command recorded as an argument vector, as one shell line (`["bash", "-lc", "x"]` is
    `x`)."""
    if (
        len(argv) == 3
        and program_name(argv[0]) in _SHELLS
        and re.fullmatch(r"-[a-z]*c[a-z]*", argv[1])
    ):
        return argv[2]
    return shlex.join(argv)
