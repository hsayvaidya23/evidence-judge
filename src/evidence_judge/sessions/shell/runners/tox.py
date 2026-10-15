"""tox and nox options: which select a subset of the environments."""

from __future__ import annotations

import re
from typing import Final

_TOX_HARMLESS: Final = frozenset(
    {"-q", "--quiet", "-v", "--verbose", "-vv", "-r", "--recreate", "--no-recreate-pkg",
     "-p", "--parallel", "--parallel-live", "-o", "--parallel-no-spinner", "--colored",
     "--no-provision", "--skip-pkg-install", "--develop", "--no-color", "--forcecolor",
     "--reuse-existing-virtualenvs", "-R", "--no-reuse-existing-virtualenvs", "--no-venv",
     "--stop-on-first-error", "-x", "--no-error-on-missing-interpreters"}
)  # fmt: skip
"""tox and nox options that change only output, parallelism or how environments are reused
(`nox -x` stops at the first failing session, which only shortens a failing run)."""


_TOX_ENVS: Final = frozenset({"-e", "--env", "-s", "--session", "--sessions"})


_TOX_VALUES: Final = frozenset(
    {"-e", "--env", "-s", "--session", "--sessions", "-k", "--keywords", "-t", "--tags", "-c",
     "--conf", "-f", "--noxfile", "--override", "-x", "--root", "--workdir", "--installpkg",
     "--python", "-P", "--force-python", "--db"}
)  # fmt: skip


_PYTHON_ENV: Final = re.compile(r"(?:py|pypy)(?:\d+(?:\.\d+)?)?")


def tox_filters(prog: str, args: list[str]) -> list[str]:
    """Filters of a tox or nox run, deny-by-default: environments other than a Python
    version's (`py`, `py312`), arguments passed through, and options outside the few that
    change only output or parallelism."""
    if args[:1] and args[0] in ("run", "r", "run-parallel", "p"):
        args = args[1:]
    filters: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            if args[i + 1 :]:
                filters.append(f"arguments passed through {prog}")
            break
        name, eq, value = arg.partition("=")
        if not arg.startswith("--") and arg[:2] in _TOX_ENVS and len(arg) > 2:
            name, eq, value = arg[:2], "=", arg[2:]
        following = args[i + 1] if i + 1 < len(args) else ""
        takes = name in _TOX_VALUES and not (prog == "nox" and name == "-x")
        if name in ("-p", "--parallel", "--python") and not eq:
            takes = prog == "nox" or following in ("auto", "all") or following.isdigit()
        if takes and not eq:
            value = following
            i += 1
        if name in _TOX_ENVS:
            envs = [e for part in value.split(",") for e in part.split() if e]
            if not envs or not all(_PYTHON_ENV.fullmatch(e) for e in envs):
                filters.append(name)
        elif name in ("-p", "--parallel", "--python"):
            pass
        elif arg.startswith("-") and not (name in _TOX_HARMLESS and not takes):
            filters.append(f"{name}, an option the judge cannot tell is harmless")
        elif not arg.startswith("-"):
            filters.append(f"only {arg}")
        i += 1
    return filters
