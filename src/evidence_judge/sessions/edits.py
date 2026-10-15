"""What an edit does to the test suite.

Adding tests is fine. Skipping a test, deleting or changing a line that holds a test or an
assertion in an existing test file, deleting a test file, or changing how the runner selects
tests all mean the suite that passed afterwards is not the suite that existed before: a passing
run can no longer prove "all tests pass". These checks run on the lines an edit removed and
added, which every source gives in some form (old/new strings, a structured patch, a unified
diff).
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from collections.abc import Iterable
from typing import Final

from evidence_judge.ingest._json import as_list, as_object

_TEST_PATH: Final = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?|testing|e2e|integration_tests)/|"
    r"(?:^|/)test_[^/]+\.py$|_tests?\.py$|(?:^|/)conftest\.py$|_test\.go$|"
    r"\.(?:test|spec)\.[cm]?[jt]sx?$|_spec\.rb$|Tests?\.(?:java|kt|cs|swift|php)$|"
    r"(?:^|/)tests?\.rs$",
    re.IGNORECASE,
)
_TEST_CONFIG: Final = re.compile(
    r"(?:^|/)(?:pytest\.ini|tox\.ini|noxfile\.py|\.mocharc\.[a-z]+|karma\.conf\.[jt]s|"
    r"phpunit\.xml(?:\.dist)?|\.rspec|(?:jest|vitest|playwright|cypress)\.config\.[cm]?[jt]s|"
    r"(?:jest|vitest)\.config\.json|\.nycrc|codecov\.ya?ml)$",
    re.IGNORECASE,
)
_SHARED_CONFIG: Final = re.compile(
    r"(?:^|/)(?:pyproject\.toml|setup\.cfg|package\.json|Cargo\.toml|Makefile|justfile|"
    r"\.github/workflows/[^/]+\.ya?ml)$",
    re.IGNORECASE,
)
_TEST_CONFIG_KEYS: Final = re.compile(
    r"addopts|testpaths|python_files|python_functions|python_classes|norecursedirs|"
    r"collect_ignore|deselect|--ignore|\s-k\s|\"test\"\s*:|testMatch|testRegex|"
    r"testPathIgnorePatterns|\bexclude\b|\binclude\b|\[tool\.pytest|\[tool:pytest\]|\[pytest\]|"
    r"\[\[test\]\]|harness\s*=|^test\s*:|\btest:",
    re.IGNORECASE,
)
_SKIP_MARKERS: Final = re.compile(
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail)\s*\(|"
    r"@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b|\.skipTest\s*\(|"
    r"\b(?:it|test|describe|context|suite)\.(?:skip|todo|failing)\s*\(|"
    r"\b(?:xit|xtest|xdescribe|xcontext|xspecify)\s*\(|\bt\.Skip(?:Now|f)?\s*\(|"
    r"#\[ignore\]|@(?:Disabled|Ignore)\b|markTestSkipped\s*\(|\bskip\s*\(\s*['\"]|"
    r"\bpending\s*\(|pytest_collection_modifyitems|collect_ignore",
)
_TEST_LINE: Final = re.compile(
    r"\bassert\w*\b|\bexpect\s*\(|\.(?:to|not)\.?\w*\(|\bself\.assert\w*\(|\bdef test\w*|"
    r"\b(?:it|test|describe|specify)\s*\(|\bfunc Test\w*|#\[test\]|#\[tokio::test\]|@Test\b|"
    r"\bshould\b|\bassert_eq!|\bassert_ne!|\bassert!|\brequire\.\w+\(|\bt\.(?:Error|Fatal)\w*\(|"
    r"\bpytest\.raises\b|\bpytest\.approx\b|@pytest\.mark\.parametrize|\bExpect\(|\bShould\(",
)
_WS: Final = re.compile(r"\s+")


def is_test_path(path: str) -> bool:
    return _TEST_PATH.search(path) is not None


def is_test_config(path: str) -> bool:
    return _TEST_CONFIG.search(path) is not None


def _norm(line: str) -> str:
    return _WS.sub("", line)


def _significant(lines: Iterable[str]) -> Counter[str]:
    return Counter(_norm(line) for line in lines if _TEST_LINE.search(line))


def line_diff(old: str, new: str) -> tuple[list[str], list[str]]:
    """Lines only in `old` and lines only in `new` (as multisets, order ignored)."""
    old_lines = Counter(old.splitlines())
    new_lines = Counter(new.splitlines())
    removed = list((old_lines - new_lines).elements())
    added = list((new_lines - old_lines).elements())
    return removed, added


def patch_lines(hunks: object) -> tuple[list[str], list[str]] | None:
    """Removed and added lines of a Claude Code `structuredPatch`."""
    listed = as_list(hunks)
    if not isinstance(hunks, list):
        return None
    removed: list[str] = []
    added: list[str] = []
    for hunk in listed:
        raw = as_object(hunk).get("lines")
        lines = as_list(raw)
        if not isinstance(raw, list):
            return None
        for line in lines:
            if not isinstance(line, str):
                return None
            if line.startswith("-"):
                removed.append(line[1:])
            elif line.startswith("+"):
                added.append(line[1:])
    return removed, added


def unified_diff_lines(diff: str) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    added: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return removed, added


def suite_findings(
    path: str, change: str, removed: list[str] | None, added: list[str] | None
) -> list[str]:
    """Ways an edit of `path` weakens the test suite. `change` is add, modify, delete or
    unknown; `removed`/`added` are None when the source has no diff."""
    name = posixpath.basename(path)
    findings: list[str] = []
    test_file = is_test_path(path)
    if change == "delete" and (test_file or is_test_config(path)):
        return [f"deleted test file {name}"]
    if added is not None and (test_file or is_test_config(path)):
        skips = [line.strip() for line in added if _SKIP_MARKERS.search(line)]
        if skips:
            findings.append(f"added a skip or expected-failure marker in {name}: {skips[0][:80]}")
    if test_file and change != "add":
        if removed is None:
            findings.append(f"rewrote test file {name} without a recorded diff")
        else:
            lost = _significant(removed) - _significant(added or [])
            if lost:
                count = sum(lost.values())
                findings.append(
                    f"removed or changed {count} line{'s' if count > 1 else ''} holding tests "
                    f"or assertions in {name}"
                )
    if is_test_config(path) and change != "add" and not test_file:
        findings.append(f"changed test configuration {name}")
    elif _SHARED_CONFIG.search(path) and change != "add":
        touched = [*(removed or []), *(added or [])]
        if removed is None and added is None:
            findings.append(f"rewrote {name}, which can hold test configuration, without a diff")
        elif any(_TEST_CONFIG_KEYS.search(line) for line in touched):
            findings.append(f"changed test configuration in {name}")
    return findings
