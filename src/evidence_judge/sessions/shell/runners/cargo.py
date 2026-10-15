"""`cargo test` options: which select a subset of the suite."""

from __future__ import annotations

from typing import Final

_CARGO_VALUE_FLAGS: Final = frozenset(
    {
        "-p",
        "--package",
        "--test",
        "--bin",
        "--example",
        "--bench",
        "--exclude",
        "--features",
        "-F",
        "--target",
        "-j",
        "--jobs",
        "--profile",
        "--manifest-path",
        "--target-dir",
        "--color",
        "-E",
        "--filterset",
        "--message-format",
        "--config",
        "-Z",
        "--test-threads",
        "--failure-output",
        "--success-output",
        "--status-level",
        "--final-status-level",
        "--cargo-profile",
        "--retries",
    }
)


_CARGO_FILTER_FLAGS: Final = frozenset(
    {"-p", "--package", "--test", "--bin", "--example", "--bench", "--exclude", "-E", "--filterset"}
)


_CARGO_FILTER_SWITCHES: Final = frozenset(
    {"--lib", "--bins", "--doc", "--examples", "--benches", "--tests"}
)


_TEST_BINARY_FILTERS: Final = frozenset({"--skip", "--exact", "--ignored"})


_CARGO_HARMLESS: Final = frozenset(
    {"--release", "-r", "--all", "--workspace", "--all-features", "--locked", "--frozen",
     "--offline", "-q", "--quiet", "-v", "-vv", "--verbose", "--no-fail-fast", "--all-targets",
     "--keep-going", "--timings", "--ignore-rust-version", "-j", "--jobs", "--target",
     "--target-dir", "--manifest-path", "--color", "--message-format", "--profile",
     "--cargo-profile", "--test-threads", "--no-capture", "--nocapture", "--failure-output",
     "--success-output", "--status-level", "--final-status-level", "--hide-progress-bar"}
)  # fmt: skip
"""`cargo test` and `cargo nextest run` options that change only output, profile, parallelism
or stopping early. Features can compile tests out; `--retries` can turn a failure into a pass."""


_TEST_BINARY_HARMLESS: Final = frozenset(
    {"--nocapture", "--show-output", "--test-threads", "-q", "--quiet", "--color", "--format",
     "--report-time", "--include-ignored"}
)  # fmt: skip


def cargo_filters(args: list[str]) -> tuple[list[str], bool]:
    filters: list[str] = []
    collect_only = False
    binary: list[str] = []
    if "--" in args:
        split = args.index("--")
        args, binary = args[:split], args[split + 1 :]
    if args[:1] == ["run"]:
        args = args[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, _ = arg.partition("=")
        if name in _CARGO_FILTER_FLAGS:
            filters.append(name)
            if not eq:
                i += 1
        elif name in _CARGO_FILTER_SWITCHES:
            filters.append(name)
        elif name in ("--no-run", "--list"):
            collect_only = True
        elif name in _CARGO_HARMLESS:
            if not eq and name in _CARGO_VALUE_FLAGS:
                i += 1
        elif arg.startswith("-"):
            filters.append(f"{name}, an option the judge cannot tell is harmless")
            if not eq and name in _CARGO_VALUE_FLAGS:
                i += 1
        else:
            filters.append(f"only tests matching {arg}")
        i += 1
    for arg in binary:
        name = arg.partition("=")[0]
        if arg in _TEST_BINARY_FILTERS:
            filters.append(arg)
        elif arg == "--list":
            collect_only = True
        elif arg.startswith("-") and name not in _TEST_BINARY_HARMLESS:
            filters.append(f"{name}, an option the judge cannot tell is harmless")
        elif not arg.startswith("-") and not arg.isdigit():
            filters.append(f"only tests matching {arg}")
    return filters, collect_only
