"""make and its relatives: targets, options and the recipes they run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.env import harmless_env, run_env_allowed
from evidence_judge.sessions.shell.lexer import ASSIGNMENT, dir_word, unspelled

_TEST_TARGETS: Final = frozenset({"test", "tests", "check"})


_MAKE_OPTIONS: Final[dict[str, tuple[dict[str, str], frozenset[str]]]] = {
    "make": (
        {"-C": "dir", "--directory": "dir", "-f": "file", "--file": "file", "--makefile": "file",
         "-I": "value", "--include-dir": "value", "-o": "value", "--old-file": "value",
         "--assume-old": "value", "-W": "value", "--what-if": "value", "--new-file": "value",
         "--assume-new": "value", "-j": "count", "--jobs": "count", "-l": "count",
         "--load-average": "count", "--max-load": "count", "-O": "count",
         "--output-sync": "count"},
        frozenset({"-s", "--silent", "--quiet", "-k", "--keep-going", "-w", "--print-directory",
                   "--no-print-directory", "-B", "--always-make", "-r", "--no-builtin-rules",
                   "-R", "--no-builtin-variables", "--warn-undefined-variables", "-j", "--jobs",
                   "-l", "-O"}),
    ),
    "gmake": ({}, frozenset()),
    "just": (
        {"-f": "file", "--justfile": "file", "-d": "dir", "--working-directory": "dir",
         "--set": "pair", "--shell": "value", "--dotenv-filename": "value",
         "--dotenv-path": "value", "--color": "value"},
        frozenset({"-q", "--quiet", "-v", "--verbose", "--color"}),
    ),
    "task": (
        {"-t": "file", "--taskfile": "file", "-d": "dir", "--dir": "dir", "-o": "value",
         "--output": "value", "-c": "value", "--color": "value", "-C": "count",
         "--concurrency": "count"},
        frozenset({"-s", "--silent", "-v", "--verbose", "-o", "--output", "-c", "--color", "-C",
                   "--concurrency", "-f", "--force"}),
    ),
    "mage": ({"-d": "dir", "-w": "dir", "-t": "value"}, frozenset({"-v", "-t"})),
}  # fmt: skip
"""For each make-like program, its options that take a value (what the value is) and the
options that change only output, parallelism or rebuilding. Any other option (`make -e`,
`make -i`, `just --dotenv-path`) may change what the recipe runs."""
_MAKE_OPTIONS["gmake"] = _MAKE_OPTIONS["make"]


@dataclass(slots=True)
class _MakeArgs:
    targets: list[str] = field(default_factory=list[str])
    assigns: dict[str, str] = field(default_factory=dict[str, str])
    dirs: list[str] = field(default_factory=list[str])
    files: list[str] = field(default_factory=list[str])
    unknown: list[str] = field(default_factory=list[str])
    dry_run: bool = False


def _make_args(prog: str, args: list[str]) -> _MakeArgs:
    """Targets, `VAR=value` arguments and options of a make-like command line, options
    anywhere (`make test -C dir`, `make -Cdir -j4 test`)."""
    takes, harmless = _MAKE_OPTIONS[prog]
    out = _MakeArgs()

    def option(name: str, value: str | None) -> bool:
        """Record `name`; True when it took `value`."""
        kind = takes.get(name)
        if name in ("-n", "--dry-run", "--just-print", "--recon", "--dry"):
            out.dry_run = True
        if kind == "dir":
            out.dirs.append(dir_word(value or ""))
        elif kind == "file":
            out.files.append(value or "")
            if not value or unspelled(value):
                out.unknown.append(f"{name} {value or ''}".strip())
        elif kind == "pair" and value is not None:
            out.assigns[value] = ""
        if kind == "count":
            return value is not None and value.isdigit()
        if name not in harmless and kind not in ("dir", "file") and not out.dry_run:
            out.unknown.append(name)
        return kind is not None

    i = 0
    while i < len(args):
        arg = args[i]
        following = args[i + 1] if i + 1 < len(args) else None
        if arg == "--":
            out.targets.extend(args[i + 1 :])
            break
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if eq:
                option(name, value)
            elif option(name, following):
                i += 1
        elif arg.startswith("-") and len(arg) > 1:
            for k, letter in enumerate(arg[1:], start=2):
                name, attached = f"-{letter}", arg[k:]
                if takes.get(name) is None:
                    option(name, None)
                    continue
                if attached and (takes[name] != "count" or attached.isdigit()):
                    option(name, attached)
                elif not attached and option(name, following):
                    i += 1
                break
        elif ASSIGNMENT.match(arg):
            name, _, value = arg.partition("=")
            out.assigns[name] = value
        else:
            out.targets.append(arg)
        i += 1
    return out


def make_like(prog: str, words: list[str]) -> Classified:
    parsed = _make_args(prog, words[1:])
    if parsed.dry_run:
        return Classified(Category.READ_ONLY, prog)
    targets = parsed.targets
    if not targets:
        return Classified(Category.BUILD, prog, mutation=f"{prog} runs the default target")
    target = targets[0]
    label = f"{prog} {target}"
    if target in _TEST_TARGETS or target.startswith(("test-", "test_", "tests-")):
        filters = [] if target in _TEST_TARGETS else [f"target {target}"]
        filters.extend(f"also runs {t}" for t in targets[1:])
        filters.extend(
            f"{name}, an option the judge cannot tell is harmless" for name in parsed.unknown
        )
        return Classified(
            Category.TEST,
            prog,
            label,
            filters,
            relocations=parsed.dirs,
            config_files=[f for f in parsed.files if f],
            run_env=[
                name
                for name, value in parsed.assigns.items()
                if not run_env_allowed(name) and not harmless_env(name, value)
            ],
        )
    if target in ("build", "all", "compile", "dist"):
        return Classified(Category.BUILD, prog)
    if target in ("lint", "check-lint", "ruff", "flake8"):
        return Classified(Category.LINT, prog)
    if target in ("typecheck", "type-check", "types", "mypy", "pyright", "tsc"):
        return Classified(Category.TYPECHECK, prog)
    if target in ("fmt", "format"):
        return Classified(Category.FORMAT, prog, mutation=f"{label} rewrites files")
    return Classified(Category.UNKNOWN, prog, mutation=f"{label} runs a project target")
