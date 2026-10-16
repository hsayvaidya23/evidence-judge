"""Options that keep a lint or type-check run from deciding the whole project."""

from __future__ import annotations

import posixpath
from typing import Final

_SUBCOMMANDS: Final = {"ruff": ("check",), "cargo": ("clippy", "check"), "golangci-lint": ("run",)}
_SOURCE_SUFFIX: Final = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue",
     ".svelte", ".go", ".rs", ".rb", ".java", ".kt", ".swift", ".sh", ".css", ".scss", ".md"}
)  # fmt: skip
_VALUE_OPTIONS: Final = frozenset(
    {"--config", "-c", "--rcfile", "--config-file", "-p", "--project", "--stdin-filename",
     "--output-file", "-o", "--output-format", "--format", "-f", "--cache-dir",
     "--cache-location", "--python-version", "--python-executable", "--target-version"}
)  # fmt: skip
_MASKS: Final = {
    "--exit-zero": "exits 0 whatever it finds",
    "--fail-under": "passes below a perfect score",
    "--issues-exit-code": "sets the exit status it reports findings with",
}
_NARROWS: Final = frozenset(
    {"--select", "--ignore", "--extend-ignore", "--exclude", "--extend-exclude",
     "--per-file-ignores", "--extend-per-file-ignores", "--config", "--isolated", "--rcfile",
     "--disable", "--errors-only", "--ignore-patterns", "--rule", "--no-eslintrc",
     "--no-config-lookup", "--ignore-pattern", "--config-file", "--disable-error-code",
     "--exclude-dirs", "--skip-dirs", "--skip-files", "--disable-all", "--enable-only", "--new",
     "--new-from-rev", "--new-from-patch", "--project", "--skipunannotated", "--stdin-filename",
     "--no-strict-optional", "--module", "--package", "--command", "--allow"}
)  # fmt: skip
"""Options that pick the rules, files or configuration a run checks: what it finds clean says
nothing about the project's own settings over all of its files."""
_SHORT_NARROWS: Final = {
    "pylint": frozenset({"-d", "-E"}),
    "golangci-lint": frozenset({"-D", "-c"}),
    "mypy": frozenset({"-m", "-p", "-c"}),
    "dmypy": frozenset({"-m", "-p", "-c"}),
    "eslint": frozenset({"-c"}),
    "pyright": frozenset({"-p"}),
    "basedpyright": frozenset({"-p"}),
    "cargo": frozenset({"-A", "-p"}),
}
_DEFAULT_CONFIGS: Final = frozenset(
    {".", "tsconfig.json", "pyproject.toml", "pyrightconfig.json", "setup.cfg", "mypy.ini",
     ".mypy.ini", "ruff.toml", ".ruff.toml", ".flake8", ".pylintrc", "pylintrc",
     ".golangci.yml", ".golangci.yaml", "eslint.config.js", "eslint.config.mjs"}
)  # fmt: skip
"""The configuration a tool finds on its own: naming it changes nothing."""
_RELAXED_IMPORTS: Final = frozenset({"skip", "silent"})


def _name(arg: str) -> str:
    return arg.partition("=")[0] if arg.startswith("--") else arg


def _option_value(args: list[str], index: int) -> str:
    arg = args[index]
    if "=" in arg and arg.startswith("--"):
        return arg.partition("=")[2]
    return args[index + 1] if index + 1 < len(args) else ""


def lint_scope(prog: str, args: list[str]) -> tuple[list[str], str | None]:
    """Why a lint or type-check run of `prog` with `args` does not decide the whole project
    (explicit files, a rule or file selection, another configuration), and why its exit status
    does not report what it found (`--exit-zero`), or ([], None)."""
    words = list(args)
    if words[:1] and words[0] in _SUBCOMMANDS.get(prog, ()):
        words = words[1:]
    if prog == "cargo" and "--" in words:
        words = [w for w in words if w != "--"]
    narrows: list[str] = []
    masked: str | None = None
    skip_next = False
    for index, arg in enumerate(words):
        if skip_next:
            skip_next = False
            continue
        name = _name(arg)
        if name in _MASKS and not (
            name == "--issues-exit-code" and _option_value(words, index) != "0"
        ):
            masked = masked or f"{prog} {name} {_MASKS[name]}"
        elif name == "--follow-imports":
            if _option_value(words, index) in _RELAXED_IMPORTS:
                narrows.append(f"{prog} {arg} leaves imported modules unchecked")
        elif (
            name in _NARROWS
            or name in _SHORT_NARROWS.get(prog, ())
            or name.startswith(("--allow-", "--disable-"))
        ):
            value = _option_value(words, index).removeprefix("./")
            if not (name in ("--config", "--project", "-p", "-c") and value in _DEFAULT_CONFIGS):
                narrows.append(f"{prog} {name} picks what it checks")
        if name in _VALUE_OPTIONS and "=" not in arg:
            skip_next = True
            continue
        if not arg.startswith("-") and posixpath.splitext(arg)[1] in _SOURCE_SUFFIX:
            narrows.append(f"{prog} checks only {arg}")
    return list(dict.fromkeys(narrows)), masked
