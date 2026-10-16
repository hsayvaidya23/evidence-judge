"""What the classifier knows about programs by name: which only read, which change files, and
which lint, type check or build."""

from __future__ import annotations

import re
from typing import Final

READ_ONLY_PROGRAMS: Final = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "wc",
        "echo",
        "printf",
        "pwd",
        "which",
        "type",
        "file",
        "stat",
        "diff",
        "cmp",
        "tree",
        "sort",
        "uniq",
        "cut",
        "tr",
        "jq",
        "yq",
        "du",
        "df",
        "date",
        "true",
        "false",
        "test",
        "[",
        "[[",
        "sleep",
        "cd",
        "pushd",
        "popd",
        "export",
        "set",
        "unset",
        "alias",
        ":",
        "exit",
        "return",
        "nl",
        "column",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "whoami",
        "id",
        "uname",
        "hostname",
        "ps",
        "xxd",
        "od",
        "hexdump",
        "md5",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "shasum",
        "env",
        "printenv",
        "fd",
        "locate",
        "comm",
        "paste",
        "fold",
        "fmt",
        "rev",
        "tac",
        "seq",
        "yes",
        "expr",
        "bc",
        "history",
        "lsof",
        "top",
        "free",
        "uptime",
        "nproc",
        "tput",
        "clear",
        "wait",
        "jobs",
        "read",
        "shopt",
        "ulimit",
        "umask",
        "trap",
        "command",
        "hash",
        "declare",
        "local",
        "typeset",
        "getconf",
        "sw_vers",
        "lscpu",
        "pgrep",
        "kill",
        "pkill",
        "open",
        "awk",
        "gawk",
        "sed",
        "find",
        "curl",
        "git",
        "pip",
        "pip3",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "cargo",
        "go",
        "uv",
    }
)
"""Programs that do not change files (the last group only for some subcommands, decided below)."""


MUTATING: Final = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "mkdir",
        "touch",
        "ln",
        "chmod",
        "chown",
        "chgrp",
        "truncate",
        "dd",
        "tee",
        "patch",
        "install",
        "unzip",
        "tar",
        "gunzip",
        "gzip",
        "bunzip2",
        "zip",
        "rsync",
        "scp",
        "wget",
        "shred",
        "unlink",
        "brew",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "apk",
        "conda",
        "mamba",
        "pipx",
        "gem",
        "composer",
        "sync",
        "mkfifo",
        "split",
    }
)


LINTERS: Final = frozenset(
    {
        "flake8",
        "pylint",
        "eslint",
        "golangci-lint",
        "rubocop",
        "shellcheck",
        "stylelint",
        "markdownlint",
        "markdownlint-cli2",
        "yamllint",
        "hadolint",
        "swiftlint",
        "ktlint",
        "standard",
        "xo",
        "oxlint",
        "pyflakes",
        "pycodestyle",
        "pydocstyle",
        "bandit",
        "vulture",
        "staticcheck",
        "revive",
        "tflint",
        "actionlint",
        "codespell",
        "cspell",
        "djlint",
    }
)


TYPECHECKERS: Final = frozenset(
    {"pyright", "basedpyright", "mypy", "pyre", "pytype", "vue-tsc", "flow", "ty"}
)


BUILDERS: Final = frozenset(
    {
        "webpack",
        "rollup",
        "esbuild",
        "parcel",
        "ninja",
        "msbuild",
        "javac",
        "gcc",
        "g++",
        "clang",
        "clang++",
        "rustc",
        "tsup",
        "swc",
        "turbo",
        "nx",
    }
)


OPAQUE_WRITERS: Final = frozenset(
    {"tar", "unzip", "patch", "dd", "rsync", "wget", "split", "bunzip2", "gunzip", "7z", "unrar"}
)
"""File-changing programs whose targets are not (all) the words on their command line."""


DETACHERS: Final = frozenset(
    {"setsid", "disown", "at", "batch", "screen", "tmux", "daemonize", "start-stop-daemon",
     "systemd-run", "launchctl", "crontab", "watchexec", "entr", "fswatch", "inotifywait"}
)  # fmt: skip
"""Programs that leave something running, or scheduled, after the command returns."""


COPIERS: Final = frozenset({"cp", "mv", "install", "ln", "scp"})


VERSION_FLAGS: Final = frozenset({"--version", "-V", "-VV", "--help", "-h"})


PYTHON_SIDE_EFFECTS: Final = re.compile(
    r"import|open|exec|eval|compile|__|getattr|setattr|delattr|globals|locals|vars|"
    r"breakpoint|help|input|memoryview"
)
"""Without these a `python -c` program reaches no module, file or builtin that can write."""
