"""Splitting a command line into simple commands and unwrapping launchers."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final

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


SAFE_TARGETS: Final = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


ASSIGNMENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


PYTHON: Final = re.compile(r"^(?:python|pypy)[0-9.]*$|^py$")


SHELLS: Final = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})


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


_TIMEOUT_VALUE_FLAGS: Final = frozenset({"-s", "--signal", "-k", "--kill-after"})


@dataclass(slots=True)
class Simple:
    words: list[str] = field(default_factory=list[str])
    env: dict[str, str] = field(default_factory=dict[str, str])
    writes: list[str] = field(default_factory=list[str])
    heredoc: bool = False
    negated: bool = False
    subshell: bool = False


@dataclass(slots=True)
class Pipeline:
    commands: list[Simple] = field(default_factory=list[Simple])
    op_after: str | None = None


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


def strip_comments(tokens: list[str]) -> list[str]:
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


def parse(tokens: list[str]) -> list[Pipeline]:
    pipelines: list[Pipeline] = [Pipeline()]
    current = Simple()
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
            if tok not in ("<", "<<<") and not fd_dup and target not in SAFE_TARGETS | {"-"}:
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
            current = Simple()
            if tok in _SEPARATORS:
                pipelines[-1].op_after = tok
                pipelines.append(Pipeline())
            i += 1
            continue
        current.subshell = current.subshell or depth > 0
        if not current.words and tok == "!":
            current.negated = True
        elif not current.words and ASSIGNMENT.match(tok):
            name, _, value = tok.partition("=")
            current.env[name] = value
        else:
            current.words.append(tok)
        i += 1
    pipelines[-1].commands.append(current)
    cleaned: list[Pipeline] = []
    for pipeline in pipelines:
        commands = [c for c in pipeline.commands if c.words or c.writes or c.env]
        if commands:
            cleaned.append(Pipeline(commands, pipeline.op_after))
        elif cleaned and pipeline.op_after is not None:
            cleaned[-1].op_after = pipeline.op_after
    return cleaned


def program_name(word: str) -> str:
    return posixpath.basename(word)


def flags_then_rest(words: Sequence[str], value_flags: frozenset[str]) -> list[str]:
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


@dataclass(slots=True)
class Wrapped:
    """What the wrappers around a command do to the run inside them."""

    filters: list[str] = field(default_factory=list[str])
    dirs: list[str] = field(default_factory=list[str])


_UV_RUN_HARMLESS: Final = frozenset(
    {"--no-project", "--frozen", "--locked", "--offline", "-q", "--quiet", "-v", "--verbose",
     "--no-sync", "--isolated", "--python", "-p", "--all-extras", "--extra", "--group", "--dev",
     "--no-dev", "--only-dev", "--no-group", "--only-group", "--all-groups", "--color",
     "--native-tls", "--cache-dir", "--no-cache", "-n", "--refresh", "--refresh-package",
     "--reinstall", "--reinstall-package", "--upgrade", "-U", "--upgrade-package", "--index",
     "--default-index", "--index-url", "--extra-index-url", "--find-links", "-f", "--no-index",
     "--exclude-newer", "--resolution", "--prerelease", "--link-mode", "--active",
     "--managed-python", "--no-managed-python", "--no-python-downloads", "--no-env-file",
     "--no-editable", "--compile-bytecode", "--package", "--all-packages", "--with",
     "--with-editable", "--with-requirements", "--from", "--no-progress", "-m", "--module"}
)  # fmt: skip
"""`uv run` and `uvx` options that change only which interpreter, index, cache or lock state
the environment comes from (`--with` and `--from` are judged as packages the session adds).
`--directory` and `--project` move the run; `--env-file` sets variables; anything else may
change what runs."""


def _uv_run_options(args: list[str], wrapped: Wrapped) -> None:
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        name, eq, value = args[i].partition("=")
        takes = not eq and name in _UV_VALUE_FLAGS
        if takes:
            value = args[i + 1] if i + 1 < len(args) else ""
        if name in ("--directory", "--project"):
            wrapped.dirs.append(dir_word(value))
        elif name == "--env-file":
            wrapped.filters.append(f"variables loaded from {value} (--env-file)")
        elif name not in _UV_RUN_HARMLESS:
            wrapped.filters.append(f"uv run {name}, an option the judge cannot tell is harmless")
        i += 2 if takes else 1


def dir_word(target: str) -> str:
    """A directory as written, or `?` when only the shell knows it."""
    return "?" if target in ("", "-") or target.startswith("~") or unspelled(target) else target


def unwrap(words: list[str], wrapped: Wrapped | None = None) -> list[str]:
    """Strip wrapper programs that run another command (`timeout 60 pytest`, `uv run pytest`),
    noting in `wrapped` what they do to the run (`uv run --directory`, `dotenv`)."""
    wrapped = wrapped if wrapped is not None else Wrapped()
    for _ in range(MAX_NESTING * 2):
        if not words:
            return words
        prog = program_name(words[0])
        rest = words[1:]
        if prog in ("time", "nohup", "exec", "builtin", "caffeinate", "sudo", "doas"):
            words = flags_then_rest(rest, frozenset({"-u", "-g", "-C"}))
        elif prog == "command" and rest and rest[0] not in ("-v", "-V"):
            words = flags_then_rest(rest, frozenset())
        elif prog == "env" and rest:
            for i, arg in enumerate(rest):
                if arg in ("-C", "--chdir") and i + 1 < len(rest):
                    wrapped.dirs.append(dir_word(rest[i + 1]))
                elif arg.startswith("--chdir="):
                    wrapped.dirs.append(dir_word(arg.partition("=")[2]))
                elif not arg.startswith("-"):
                    break
            rest = flags_then_rest(rest, frozenset({"-u", "-C", "-S"}))
            while rest and ASSIGNMENT.match(rest[0]):
                rest = rest[1:]
            if not rest:
                return words
            words = rest
        elif prog == "timeout":
            rest = flags_then_rest(rest, _TIMEOUT_VALUE_FLAGS)
            words = rest[1:]
        elif prog == "nice":
            words = flags_then_rest(rest, frozenset({"-n", "--adjustment"}))
        elif prog in ("stdbuf", "ionice", "chrt", "taskset"):
            words = flags_then_rest(rest, frozenset({"-c", "-n", "-p"}))
        elif prog == "xvfb-run":
            words = flags_then_rest(rest, frozenset({"-n", "-s", "-e", "-f", "-p", "-w"}))
        elif prog in ("uv", "poetry", "pipenv", "pdm", "rye", "hatch") and rest[:1] == ["run"]:
            if prog == "uv":
                _uv_run_options(rest[1:], wrapped)
            words = flags_then_rest(rest[1:], _UV_VALUE_FLAGS)
        elif prog in ("uvx", "pipx") and (prog == "uvx" or rest[:1] == ["run"]):
            if prog == "uvx":
                _uv_run_options(rest, wrapped)
            words = flags_then_rest(rest if prog == "uvx" else rest[1:], _UV_VALUE_FLAGS)
        elif prog in ("npx", "bunx", "pnpx"):
            words = flags_then_rest(rest, frozenset({"-p", "--package", "-c", "--call"}))
        elif prog in ("npm", "pnpm", "yarn") and rest[:1] in (["exec"], ["dlx"], ["x"]):
            words = flags_then_rest(rest[1:], frozenset({"-p", "--package", "-c", "--call"}))
        elif prog == "bundle" and rest[:1] == ["exec"]:
            words = rest[1:]
        elif prog == "dotenv" and "--" in rest:
            wrapped.filters.append("variables loaded by dotenv")
            words = rest[rest.index("--") + 1 :]
        elif PYTHON.match(prog) and len(rest) >= 2 and rest[0] == "-m":
            words = [rest[1], *rest[2:]]
        elif PYTHON.match(prog) and rest[:1] and rest[0].startswith("-m") and len(rest[0]) > 2:
            words = [rest[0][2:], *rest[1:]]
        else:
            return words
    return words


def unspelled(path: str) -> bool:
    """A path the shell builds at run time (a variable, a command substitution, a glob)."""
    return re.search(r"[$`*?\[]", path) is not None


def unwrap_shell(argv: Sequence[str]) -> str:
    """A command recorded as an argument vector, as one shell line (`["bash", "-lc", "x"]` is
    `x`)."""
    if (
        len(argv) == 3
        and program_name(argv[0]) in SHELLS
        and re.fullmatch(r"-[a-z]*c[a-z]*", argv[1])
    ):
        return argv[2]
    return shlex.join(argv)
