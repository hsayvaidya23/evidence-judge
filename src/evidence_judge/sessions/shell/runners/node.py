"""`node --test` options: which select a subset of the suite."""

from __future__ import annotations

from typing import Final

_NODE_TEST_HARMLESS: Final = frozenset(
    {"--test", "--test-reporter", "--test-reporter-destination", "--test-concurrency",
     "--test-timeout", "--experimental-test-coverage", "--test-coverage-include",
     "--test-coverage-exclude", "--test-coverage-lines", "--test-coverage-branches",
     "--test-coverage-functions", "--test-force-exit", "--enable-source-maps", "--no-warnings",
     "--trace-warnings", "--trace-uncaught", "--stack-trace-limit", "--max-old-space-size"}
)  # fmt: skip
"""`node --test` options that change only output, coverage, workers, timeouts or warnings
printing. Anything else (`--require`, `--import`, `--env-file`, `--conditions`, a loader) can
change what the tests load."""


_NODE_TEST_FILTERS: Final = frozenset(
    {"--test-name-pattern", "--test-skip-pattern", "--test-only", "--test-shard"}
)


_NODE_VALUES: Final = frozenset(
    {"--test-reporter", "--test-reporter-destination", "--test-concurrency", "--test-timeout",
     "--test-coverage-include", "--test-coverage-exclude", "--test-coverage-lines",
     "--test-coverage-branches", "--test-coverage-functions", "--test-name-pattern",
     "--test-skip-pattern", "--test-shard", "--require", "-r", "--import", "--loader",
     "--experimental-loader", "--env-file", "--conditions", "-C", "--stack-trace-limit",
     "--max-old-space-size", "--input-type", "--eval", "-e", "--print", "-p"}
)  # fmt: skip


def node_test_filters(args: list[str]) -> tuple[list[str], bool]:
    """(filters, updates snapshots) of a `node --test` run, deny-by-default."""
    filters: list[str] = []
    update = False
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, _ = arg.partition("=")
        if name == "--test-update-snapshots":
            update = True
        elif name in _NODE_TEST_FILTERS:
            filters.append(name)
        elif arg.startswith("-") and name not in _NODE_TEST_HARMLESS:
            filters.append(f"{name}, an option the judge cannot tell is harmless")
        elif not arg.startswith("-"):
            filters.append(f"only {arg}")
        if not eq and name in _NODE_VALUES:
            i += 1
        i += 1
    return filters, update
