"""What an edit does to the test suite.

Adding tests is fine. Skipping a test, deleting or changing any existing line of a test file
(an assertion, but also the parametrize data, a fixture or a helper an assertion relies on),
deleting a test file, creating or changing test configuration, or changing the files that say
how the tests run (a Makefile recipe, a package.json script) all mean the suite that passed
afterwards is not the suite that existed before: a passing run can no longer prove "all tests
pass". These checks run on the lines an edit removed and added, which every source gives in
some form (old/new strings, a structured patch, a unified diff), and, for configuration files
holding several tools' settings, on the sections the edit touched when the whole file is known.
"""

from __future__ import annotations

from evidence_judge.sessions.edits.diff import (
    adds_tests,
    changed_sections,
    line_diff,
    patch_lines,
    tests_added,
    unified_diff_lines,
)
from evidence_judge.sessions.edits.paths import (
    data_reader,
    expected_module,
    git_metadata_reason,
    is_code_path,
    is_config_path,
    is_harness_path,
    is_test_config,
    is_test_path,
    session_config_reason,
)
from evidence_judge.sessions.edits.pyast import (
    changed_constants,
    exit_hooks,
    imported_names,
    opened_paths,
    rebinding_findings,
)
from evidence_judge.sessions.edits.suite import NO_DIFF, suite_findings

__all__ = [
    "NO_DIFF",
    "adds_tests",
    "changed_constants",
    "changed_sections",
    "data_reader",
    "exit_hooks",
    "expected_module",
    "git_metadata_reason",
    "imported_names",
    "is_code_path",
    "is_config_path",
    "is_harness_path",
    "is_test_config",
    "is_test_path",
    "line_diff",
    "opened_paths",
    "patch_lines",
    "rebinding_findings",
    "session_config_reason",
    "suite_findings",
    "tests_added",
    "unified_diff_lines",
]
