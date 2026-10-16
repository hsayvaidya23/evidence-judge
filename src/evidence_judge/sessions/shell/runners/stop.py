"""Options that make a test run stop before it has run every test it selected."""

from __future__ import annotations

from typing import Final

from evidence_judge.sessions.shell.runners.pytest import short_cluster

_STOP_OPTIONS: Final = {
    "pytest": frozenset({"-x", "--exitfirst", "--maxfail", "--sw", "--stepwise"}),
    "unittest": frozenset({"-f", "--failfast"}),
    "jest": frozenset({"-b", "--bail"}),
    "vitest": frozenset({"--bail"}),
    "mocha": frozenset({"-b", "--bail"}),
    "ava": frozenset({"--fail-fast"}),
    "tap": frozenset({"-b", "--bail"}),
    "jasmine": frozenset({"--stop-on-failure"}),
    "playwright test": frozenset({"-x", "--max-failures"}),
    "rspec": frozenset({"--fail-fast"}),
    "phpunit": frozenset({"--stop-on-failure", "--stop-on-error", "--stop-on-defect"}),
    "pest": frozenset({"--stop-on-failure", "--stop-on-error", "--stop-on-defect", "--bail"}),
    "nosetests": frozenset({"-x", "--stop"}),
    "nose2": frozenset({"-F", "--fail-fast"}),
    "behave": frozenset({"--stop"}),
    "ctest": frozenset({"--stop-on-failure"}),
    "go test": frozenset({"-failfast", "--failfast"}),
    "nox": frozenset({"-x", "--stop-on-first-error"}),
    "tox": frozenset({"--stop-on-first-error"}),
}
"""Per runner, the options that end a failing run early (`pytest -x`, `jest --bail`)."""


_KEEP_GOING: Final = {
    "cargo test": frozenset({"--no-fail-fast"}),
    "cargo nextest": frozenset({"--no-fail-fast"}),
    "cargo insta test": frozenset({"--no-fail-fast"}),
    "mvn": frozenset({"-fae", "--fail-at-end", "-fn", "--fail-never"}),
    "mvnw": frozenset({"-fae", "--fail-at-end", "-fn", "--fail-never"}),
    "gradle": frozenset({"--continue"}),
    "gradlew": frozenset({"--continue"}),
}
"""Runners that stop at their first failing test binary, module or task unless told to keep
going."""


def stop_early(runner: str, args: list[str]) -> str | None:
    """Why a run of `runner` with `args` may stop at its first failures, or None."""
    words = [part for arg in args for part in short_cluster(arg)] if runner == "pytest" else args
    names = [word.partition("=")[0] for word in words]
    if (keep := _KEEP_GOING.get(runner)) is not None:
        if any(name in keep for name in names):
            return None
        return f"{runner} stops at its first failing target unless told to keep going"
    stops = _STOP_OPTIONS.get(runner, frozenset[str]())
    found = next((name for name in names if name in stops), None)
    return f"{runner} {found} stops at the first failures" if found else None
