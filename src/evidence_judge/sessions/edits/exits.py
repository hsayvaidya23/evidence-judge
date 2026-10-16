"""Exit hooks outside Python: JavaScript and Go code that can end a test run with its own status.

`node --test` and `npm test` report what the process exit code says, so a module the tests
import can register an exit handler that prints a passing summary and sets the exit code to 0
after the real failures. `go test` reports a package `ok` when its test binary exits 0, so a
`TestMain` that ignores `m.Run()`'s result, or calls `os.Exit(0)`, turns failing tests into
`ok` (and `go test` hides the failures of a package it reports `ok`). These are read from the
text of the edit, not parsed.
"""

from __future__ import annotations

import re
from typing import Final

JS_SUFFIX: Final = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
_JS_RULES: Final = (
    (
        re.compile(
            r"\bprocess\s*\.\s*(?:on|once|addListener|prependListener|prependOnceListener)\s*"
            r"\(\s*['\"`](?:exit|beforeExit)['\"`]"
        ),
        "registers a process exit handler",
    ),
    (re.compile(r"\bprocess\s*\.\s*exitCode\s*=\s*0\b"), "sets process.exitCode to 0"),
    (re.compile(r"\bprocess\s*\.\s*(?:exit|reallyExit)\s*\(\s*0?\s*\)"), "calls process.exit(0)"),
)
_GO_RULES: Final = (
    (re.compile(r"\bos\s*\.\s*Exit\s*\(\s*0\s*\)"), "calls os.Exit(0)"),
    (
        re.compile(r"^\s*(?:_\s*=\s*)?\w+\s*\.\s*Run\s*\(\s*\)\s*;?\s*$", re.MULTILINE),
        "discards the result of the test runner's m.Run()",
    ),
)


def js_exit_hooks(source: str, name: str) -> list[str]:
    """Exit handlers and forced zero exit codes in JavaScript or TypeScript."""
    return _found(_JS_RULES, source, name)


def go_exit_hooks(source: str, name: str) -> list[str]:
    """A Go test file that exits 0, or runs the tests and drops their result."""
    return _found(_GO_RULES, source, name)


def _found(rules: tuple[tuple[re.Pattern[str], str], ...], source: str, name: str) -> list[str]:
    return [
        f"{what} in {name}, code that can end the test run with its own exit status"
        for pattern, what in rules
        if pattern.search(source)
    ]
