"""Independent re-run: the judge runs the project's test suite itself and reads the runner's own
machine-readable report, instead of trusting the test output the session recorded.

An agent running as the user controls everything a transcript shows about a test run: the
environment, shell rc files, runner options, the directory the run happens in and the text it
prints. The re-run takes all of that out of its hands. The command comes from the spec, which the
user owns and the hooks snapshot when the turn starts; the working tree is copied into a
directory the judge owns; the command runs without a shell, in an environment built from
scratch; and the result is read from a report written outside the copy, never from stdout.

What the re-run cannot do is make weakened tests strong: a re-run of a suite whose assertions
were deleted still passes. Transcript analysis keeps deciding that, and runner configuration
that differs from the base revision is reported here as well.
"""

from __future__ import annotations

from evidence_judge.sessions.rerun.config import configuration_above, is_runner_config
from evidence_judge.sessions.rerun.reports import (
    ReportError,
    parse_cargo,
    parse_go,
    parse_jest,
    parse_junit,
)
from evidence_judge.sessions.rerun.run import execute, head_revision
from evidence_judge.sessions.rerun.spec import RerunResult, RerunSpec, RerunStatus, infer_runner

__all__ = [
    "ReportError",
    "RerunResult",
    "RerunSpec",
    "RerunStatus",
    "configuration_above",
    "execute",
    "head_revision",
    "infer_runner",
    "is_runner_config",
    "parse_cargo",
    "parse_go",
    "parse_jest",
    "parse_junit",
]
