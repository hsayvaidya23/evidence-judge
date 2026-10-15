"""`python -m unittest` options: which select a subset of the suite."""

from __future__ import annotations

from typing import Final

_UNITTEST_HARMLESS: Final = frozenset(
    {"-v", "--verbose", "-q", "--quiet", "-b", "--buffer", "-f", "--failfast", "-c", "--catch",
     "--locals"}
)  # fmt: skip
"""`python -m unittest` switches that change only output, stopping early or Ctrl-C handling."""


_UNITTEST_VALUES: Final = frozenset(
    {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory", "-k",
     "--durations"}
)  # fmt: skip


_UNITTEST_DEFAULTS: Final = {
    "-s": ".", "--start-directory": ".", "-t": ".", "--top-level-directory": ".",
    "-p": "test*.py", "--pattern": "test*.py",
}  # fmt: skip


def unittest_filters(args: list[str]) -> tuple[list[str], bool]:
    """Filters of a `python -m unittest` run, deny-by-default: every option but the few that
    change only output, and every discover argument but the defaults (start `.`, pattern
    `test*.py`), narrows or moves the run."""
    if args[:1] in (["-h"], ["--help"]):
        return [], True
    discover = args[:1] == ["discover"]
    rest = args[1:] if discover else args
    label = "discover " if discover else ""
    filters: list[str] = []
    positional: list[str] = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        name, eq, value = arg.partition("=")
        if not arg.startswith("--") and arg[:2] in _UNITTEST_VALUES and len(arg) > 2:
            name, eq, value = arg[:2], "=", arg[2:]
        if arg in _UNITTEST_HARMLESS:
            pass
        elif name in _UNITTEST_VALUES:
            if not eq:
                value = rest[i + 1] if i + 1 < len(rest) else ""
                i += 1
            default = _UNITTEST_DEFAULTS.get(name)
            if name != "--durations" and not (discover and value.strip("/") == default):
                filters.append(f"{label}{name}")
        elif arg.startswith("-") and arg != "-":
            filters.append(f"{label}{name}, an option the judge cannot tell is harmless")
        else:
            positional.append(arg)
        i += 1
    if discover:
        defaults = (".", "test*.py", ".")
        filters.extend(
            f"discover {a}"
            for k, a in enumerate(positional)
            if k >= len(defaults) or a.rstrip("/") != defaults[k].rstrip("/") or a == ""
        )
    else:
        filters.extend(f"only {a}" for a in positional)
    return filters, False
