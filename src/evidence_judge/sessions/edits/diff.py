"""Line diffs, patches and the configuration sections an edit touched."""

from __future__ import annotations

import difflib
import posixpath
import re
from collections import Counter
from collections.abc import Iterable
from typing import Final

from evidence_judge.ingest._json import as_list, as_object
from evidence_judge.sessions.edits.paths import (
    PACKAGE_JSON,
    SHARED_CONFIG,
    is_test_config,
    is_test_path,
)

_TEST_CONFIG_KEYS: Final = re.compile(
    r"addopts|testpaths|python_files|python_functions|python_classes|norecursedirs|"
    r"collect_ignore|deselect|--ignore|\s-k\s|\"test\"\s*:|testMatch|testRegex|"
    r"testPathIgnorePatterns|\bexclude\b|\binclude\b|\[tool\.pytest|\[tool:pytest\]|\[pytest\]|"
    r"\[\[test\]\]|harness\s*=|^test\s*:|\btest:|^\s*test\s*=|pythonpath|filterwarnings|"
    r"required_plugins|xfail_strict|usefixtures|confcutdir|rootdir|fail[_-]under|pytest|"
    r"conftest|\bjest\b|\bvitest\b|\bmocha\b|\bnose2?\b|\bunittest\b|skipTests|surefire|"
    r"failsafe|entry[-_]points",
    re.IGNORECASE,
)


_SECTION: Final = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:[#;].*)?$")


_TEST_SECTIONS: Final = re.compile(
    r"^(?:tool\.(?:pytest|coverage|tox|nox|hatch\.envs|pdm\.scripts|poe|taskipy|rye\.scripts|"
    r"uv)|tool:pytest|pytest|coverage:|testenv|tox|project\.entry-points\.pytest11|"
    r"options\.entry_points|project\.scripts|test|profile\.test|lib|bin)",
    re.IGNORECASE,
)


_JSON_STRUCTURE: Final = re.compile(r"^\s*[\[\]{}(),]*\s*$")


_JSON_VERSION_ENTRY: Final = re.compile(
    r'^\s*"(?P<key>[^"]*)"\s*:\s*"(?P<value>[~^<>=v\s]*\d[\w.+\-\s|<>=^~*]*|\*|latest|next|'
    r'(?:workspace|npm|file|link|portal|patch|git(?:\+\w+)?|github|https?):[^"]*)"\s*,?\s*$'
)


_TEST_LINE: Final = re.compile(
    r"\bassert\w*\b|\bexpect\s*\(|\.(?:to|not)\.?\w*\(|\bself\.assert\w*\(|\bdef test\w*|"
    r"\b(?:it|test|describe|specify)\s*\(|\bfunc Test\w*|#\[test\]|#\[tokio::test\]|@Test\b|"
    r"\bshould\b|\bassert_eq!|\bassert_ne!|\bassert!|\brequire\.\w+\(|\bt\.(?:Error|Fatal)\w*\(|"
    r"\bpytest\.raises\b|\bpytest\.approx\b|@pytest\.mark\.parametrize|\bExpect\(|\bShould\(",
)


_WS: Final = re.compile(r"\s+")


_TEST_DEFINITION: Final = re.compile(
    r"\bdef test|\bclass Test|parametrize|\bparams\s*=|pytest_generate_tests|load_tests|>>>|"
    r"\b(?:it|test|describe|specify|context)(?:\.each)?\s*[(`]|\bfunc (?:Test|Fuzz|Example)|"
    r"#\[(?:tokio::)?test|@(?:Test|ParameterizedTest|TestFactory|TestTemplate)\b"
)


def adds_tests(added: list[str] | None) -> bool:
    """The lines may define tests (a test function or class, a parametrize case, a doctest),
    or the diff is unknown."""
    return added is None or any(_TEST_DEFINITION.search(line) for line in added)


_TEST_FUNCTION: Final = re.compile(r"^\s*(?:async\s+)?def test\w*\s*\(")


_TEST_CLASS: Final = re.compile(r"^\s*class Test\w*\s*(?::|\(\s*(?:object)?\s*\)\s*:)")


_UNCOUNTED_TESTS: Final = re.compile(
    r"^\s*from\s+\.*(?:[\w.]*\.)?(?:tests?|test_\w+|\w+_tests?|conftest)(?:\.[\w.]*)?\s+import\b|"
    r"^\s*import\s+(?:[\w.]*\.)?(?:tests?|test_\w+|\w+_tests?)\b|"
    r"^\s*from\s+\S+\s+import\s+(?:.*\b[Tt]est\w*|\*)|^\s*class Test\w*\s*\(|"
    r"^\s*[Tt]est\w*\s*(?::[^=]*)?=(?!=)|\bglobals\(\)|\bsetattr\s*\(|\bexec\s*\(",
)
"""Lines in a test module that may bring in tests the count cannot see: importing from another
test module or importing names like tests, a test class inheriting another, binding a test
name by assignment, or creating names at run time."""


def tests_added(removed: list[str] | None, added: list[str] | None, path: str = "") -> int | None:
    """How many Python test functions the edit adds (`def test_*`, `async def test_*`), or None
    when that cannot be counted: an unknown diff, a parametrize case, a doctest, a test in another
    language, a test module line that may bring in tests some other way, or fewer test functions
    after the edit than before."""
    if added is None or removed is None:
        return None
    count = 0
    for line in added:
        if _TEST_FUNCTION.match(line):
            count += 1
        elif (is_test_path(path) and _UNCOUNTED_TESTS.search(line)) or (
            _TEST_DEFINITION.search(line) and not _TEST_CLASS.match(line)
        ):
            return None
    count -= sum(1 for line in removed if _TEST_FUNCTION.match(line))
    return count if count >= 0 else None


def _norm(line: str) -> str:
    return _WS.sub("", line)


def significant(lines: Iterable[str]) -> Counter[str]:
    return Counter(_norm(line) for line in lines if _TEST_LINE.search(line))


def content(lines: Iterable[str]) -> Counter[str]:
    """Non-blank lines, whitespace ignored, so re-indenting or reflowing is not a change."""
    return Counter(norm for line in lines if (norm := _norm(line)))


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


def _section_at(lines: list[str], index: int) -> str:
    for k in range(min(index, len(lines) - 1), -1, -1):
        if m := _SECTION.match(lines[k]):
            return m.group(1).strip("\"' ")
    return ""


def changed_sections(original: str, updated: str) -> set[str]:
    """The `[section]` names (TOML or INI) holding every line an edit removed or added."""
    old, new = original.splitlines(), updated.splitlines()
    sections: set[str] = set()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        sections.update(_section_at(old, k) for k in range(i1, i2))
        sections.update(_section_at(new, k) for k in range(j1, j2))
    return sections


def _package_json_change(lines: list[str]) -> bool:
    """True unless every changed line is JSON punctuation or a dependency's version."""
    for line in lines:
        if _JSON_STRUCTURE.match(line):
            continue
        entry = _JSON_VERSION_ENTRY.match(line)
        if entry is None or "test" in entry.group("key").casefold():
            return True
    return False


def config_findings(
    path: str,
    change: str,
    removed: list[str] | None,
    added: list[str] | None,
    texts: tuple[str, str] | None,
) -> list[str]:
    name = posixpath.basename(path)
    verb = "created" if change == "add" else "changed"
    if is_test_config(path):
        return [f"{verb} test configuration {name}"]
    touched = [*(removed or []), *(added or [])]
    if PACKAGE_JSON.search(path):
        if removed is None and added is None:
            return [f"rewrote {name}, which says how tests run, without a diff"]
        if _package_json_change(touched):
            return [f"{verb} {name} beyond dependency versions (scripts or test settings)"]
        return []
    if not SHARED_CONFIG.search(path):
        return []
    if removed is None and added is None and texts is None:
        return [f"rewrote {name}, which can hold test configuration, without a diff"]
    sections = changed_sections(*texts) if texts is not None else set[str]()
    test_sections = sorted(s for s in sections if _TEST_SECTIONS.match(s))
    if test_sections:
        return [f"{verb} test configuration in {name} ([{test_sections[0]}])"]
    if any(_TEST_CONFIG_KEYS.search(line) for line in touched):
        return [f"{verb} test configuration in {name}"]
    return []
