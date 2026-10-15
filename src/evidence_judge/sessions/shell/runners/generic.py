"""Options of runners without their own table."""

from __future__ import annotations

from typing import Final

_COVERAGE_FLAGS: Final = frozenset({"--coverage", "--experimental-test-coverage", "--no-coverage"})
"""Coverage switches of jest and vitest (`--coverage`) and `node --test`
(`--experimental-test-coverage`): passed to a test script whose runner is unknown, each only
measures (or makes the runner reject the switch, which fails the run)."""


def generic_filters(label: str, args: list[str]) -> list[str]:
    """Arguments passed to a test script whose runner is unknown narrow it, conservatively;
    only coverage switches are known to change nothing."""
    meaningful = [
        a
        for a in args
        if a != "--"
        and a not in ("--silent", "-s", "--if-present")
        and a.partition("=")[0] not in _COVERAGE_FLAGS
    ]
    return [f"arguments passed to {label}: {' '.join(meaningful)}"] if meaningful else []
