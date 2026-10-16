"""pytest options: which select a subset of the suite."""

from __future__ import annotations

from typing import Final

from evidence_judge.sessions.shell.env import pytest_regenerates
from evidence_judge.sessions.shell.lexer import unspelled

_PYTEST_HARMLESS_VALUES: Final = frozenset(
    {"-r", "-n", "--numprocesses", "--maxprocesses", "--tb", "--color", "--code-highlight",
     "--capture", "--durations", "--durations-min", "--maxfail", "--junitxml", "--junit-xml",
     "--junit-prefix", "--cov", "--cov-report", "--cov-config", "--cov-fail-under",
     "--cov-context", "--dist", "--log-level", "--log-cli-level", "--log-file",
     "--log-file-level", "--log-format", "--log-date-format", "--log-cli-format",
     "--log-cli-date-format", "--show-capture", "--basetemp", "--randomly-seed",
     "--random-order-seed", "--random-order-bucket", "--timeout", "--timeout-method", "--html",
     "--benchmark-json", "--count", "--workers", "--tests-per-worker", "--verbosity", "--assert",
     "--hypothesis-seed"}
)  # fmt: skip
"""pytest options taking a value that change only how a run reports, orders, spreads, times or
measures the tests it would run anyway."""


_PYTEST_HARMLESS_SWITCHES: Final = frozenset(
    {"-q", "--quiet", "-v", "--verbose", "-x", "--exitfirst", "-s", "-l", "--showlocals",
     "--no-showlocals", "--no-header", "--no-summary", "--strict-markers", "--strict-config",
     "--strict", "--full-trace", "--disable-warnings", "--disable-pytest-warnings",
     "--cache-clear", "--ff", "--failed-first", "--nf", "--new-first", "--setup-show",
     "--no-cov", "--no-cov-on-fail", "--cov-branch", "--cov-append", "--instafail",
     "--runxfail", "--doctest-modules", "--doctest-continue-on-failure",
     "--randomly-dont-reorganize", "--random-order", "--benchmark-disable",
     "--hypothesis-show-statistics", "--pdb", "--trace", "--force-sugar"}
)  # fmt: skip
"""pytest switches that change only output, order, stopping early or debugging. Any other
option may decide what a pass means (`--record-mode=rewrite`, `--regtest-reset`, `--reruns`)."""


_PYTEST_SHORT_VALUES: Final = frozenset("kmpcoWrn")


_PYTEST_COLLECT: Final = frozenset(
    {"--co", "--collect-only", "--fixtures", "--markers", "--version", "-V", "-h", "--help",
     "--setup-plan"}
)  # fmt: skip


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
    "-W": "a warnings filter (-W)",
    "--pythonwarnings": "a warnings filter (--pythonwarnings)",
}


_HARMLESS_PLUGIN_SWITCHES: Final = (
    ["no:cacheprovider"],
    ["no:randomly"],
    ["no:random_order"],
    ["no:cov"],
    ["no:sugar"],
    ["no:xdist"],
)
"""`-p` values that change no test's outcome or selection: the cache plugin only stores --lf
state, randomly and random_order only shuffle, cov only measures, sugar only prints and xdist
only spreads the same tests over workers. `no:warnings` is not one: it drops `filterwarnings =
error`, which can turn failing tests into passing ones."""


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


def short_cluster(arg: str) -> list[str]:
    """`-qx` as `-q -x`, and `-qopython_functions=x` as `-q -o python_functions=x`: a letter
    that takes a value takes the rest of the word."""
    if arg.startswith("--") or len(arg) <= 2 or not arg.startswith("-"):
        return [arg]
    out: list[str] = []
    for k, letter in enumerate(arg[1:], start=2):
        out.append(f"-{letter}")
        if letter in _PYTEST_SHORT_VALUES:
            if arg[k:]:
                out.append(arg[k:])
            break
    return out


def pytest_filters(
    args: list[str], env: dict[str, str]
) -> tuple[list[str], bool, list[str] | None]:
    """(filters, collect only, paths): `paths` are the files and directories the run selects
    when its only filters are plain paths, else None. An option not known to be harmless counts
    as a filter."""
    args = [part for arg in args for part in short_cluster(arg)]
    filters: list[str] = []
    paths: list[str] = []
    plain = True
    collect_only = False
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, _ = arg.partition("=")
        if arg in _PYTEST_COLLECT:
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
        elif name in _PYTEST_HARMLESS_VALUES:
            i += 0 if eq else 1
        elif name == "--import-mode":
            mode = arg.partition("=")[2] if eq else (args[i + 1] if i + 1 < len(args) else "")
            i += 0 if eq else 1
            if mode not in ("prepend", "append", "importlib"):
                filters.append("--import-mode, an option the judge cannot tell is harmless")
                plain = False
        elif arg in _PYTEST_HARMLESS_SWITCHES or pytest_regenerates(arg):
            pass
        elif arg.startswith("-"):
            filters.append(f"{name}, an option the judge cannot tell is harmless")
            plain = False
        else:
            paths.append(arg)
            plain = plain and "::" not in arg and not unspelled(arg)
            if arg not in (".", "./"):
                filters.append(f"only {arg}")
        i += 1
    if "PYTEST_ADDOPTS" in env:
        filters.append("PYTEST_ADDOPTS")
        plain = False
    return filters, collect_only, paths if plain else None
