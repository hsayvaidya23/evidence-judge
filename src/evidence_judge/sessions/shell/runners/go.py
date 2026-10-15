"""`go test` options: which select a subset of the suite."""

from __future__ import annotations

from typing import Final

from evidence_judge.sessions.shell.env import GO_REGEN

_GO_VALUE_FLAGS: Final = frozenset(
    {
        "-run",
        "-skip",
        "-bench",
        "-count",
        "-timeout",
        "-tags",
        "-p",
        "-parallel",
        "-coverprofile",
        "-covermode",
        "-coverpkg",
        "-cpu",
        "-exec",
        "-o",
        "-vet",
        "-shuffle",
        "-fuzz",
        "-fuzztime",
        "-benchtime",
        "-ldflags",
        "-gcflags",
        "-mod",
        "-C",
    }
)


_GO_SWITCHES: Final = frozenset(
    {"-v", "-race", "-cover", "-failfast", "-json", "-benchmem", "-trimpath", "-a", "-x", "-work",
     "-msan", "-asan", "-fullpath"}
)  # fmt: skip


_GO_HARMLESS: Final = _GO_SWITCHES | frozenset(
    {"-count", "-timeout", "-p", "-parallel", "-cpu", "-mod", "-coverprofile", "-covermode",
     "-coverpkg", "-shuffle", "-vet", "-bench", "-benchtime", "-o", "-outputdir", "-cpuprofile",
     "-memprofile", "-blockprofile", "-mutexprofile", "-trace"}
)  # fmt: skip
"""`go test` options that change only output, parallelism, timing, coverage or the order tests
run in. Build tags, linker flags, `-exec` and `-C` can change which code and tests run."""


def go_filters(args: list[str]) -> tuple[list[str], bool]:
    filters: list[str] = []
    packages: list[str] = []
    collect_only = False
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, _ = arg.partition("=")
        flag = name.replace("--", "-", 1) if name.startswith("--") else name
        if flag in ("-run", "-skip"):
            filters.append(flag)
            if not eq:
                i += 1
        elif flag == "-short":
            filters.append("-short")
        elif flag == "-list":
            collect_only = True
            if not eq:
                i += 1
        elif flag == "-args":
            filters.append("-args, arguments the judge cannot read passed to the test binary")
            break
        elif flag in _GO_HARMLESS or GO_REGEN.fullmatch(arg):
            if not eq and flag in _GO_VALUE_FLAGS and flag not in _GO_SWITCHES:
                i += 1
        elif arg.startswith("-"):
            filters.append(f"{flag}, an option the judge cannot tell is harmless")
            if not eq and flag in _GO_VALUE_FLAGS:
                i += 1
        else:
            packages.append(arg)
        i += 1
    if not packages:
        filters.append("only the current package (no ./...)")
    filters.extend(f"only {p}" for p in packages if not p.endswith("..."))
    return filters, collect_only
