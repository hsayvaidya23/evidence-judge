"""Jest and Vitest options: which select a subset of the suite."""

from __future__ import annotations

from typing import Final

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


_JS_HARMLESS: Final = frozenset(
    {"--ci", "--runInBand", "-i", "--verbose", "--silent", "--coverage", "--no-coverage",
     "--watch", "--watchAll", "--no-watch", "--no-watchAll", "--run", "--colors", "--no-colors",
     "--color", "--no-color", "--passWithNoTests", "--detectOpenHandles", "--forceExit",
     "--json", "--logHeapUsage", "--useStderr", "--no-cache", "--cache", "--expand",
     "--errorOnDeprecated", "--noStackTrace", "--hideSkippedTests", "--no-file-parallelism",
     "-w", "--maxWorkers", "--reporters", "--reporter", "--outputFile", "--coverageReporters",
     "--coverageDirectory", "--testTimeout", "--pool", "--logLevel", "-u", "--updateSnapshot",
     "--update"}
)  # fmt: skip
"""jest and vitest options that change only output, workers, caching, timeouts or coverage."""


_JS_COLLECT: Final = frozenset({"--listTests", "--list", "--showConfig"})


_VITEST_SUBCOMMANDS: Final = frozenset({"run", "watch", "dev"})


def js_filters(runner: str, args: list[str]) -> tuple[list[str], bool]:
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
            if runner in ("jest", "vitest") and not _js_harmless(name):
                filters.append(f"{name}, an option the judge cannot tell is harmless")
        else:
            filters.append(f"only {arg}")
        i += 1
    return filters, collect_only


def _js_harmless(name: str) -> bool:
    return name in _JS_HARMLESS or name.startswith("--coverage.")
