"""The spec's `rerun:` block and what one re-run showed."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

Runner = Literal["pytest", "go", "jest", "vitest", "cargo", "node", "unittest"]

_ENV_NAME: Final = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class RerunSpec(BaseModel):
    """The spec's `rerun:` block: how the judge runs the suite itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: list[str] = Field(
        min_length=1,
        description="The test command as an argv list, run without a shell (no pipes, globs or "
        "variables); the judge appends its report options at the end",
    )
    runner: Runner | None = Field(
        default=None,
        description="How the judge gets a machine-readable report; inferred from the command "
        "when omitted",
    )
    timeout: float = Field(
        default=600.0, gt=0, le=3600, description="Seconds before the run is cut"
    )
    env: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="Variables to set on top of the judge's minimal environment",
    )
    min_tests: int | None = Field(
        default=None, ge=1, description="A pass needs at least this many tests in the report"
    )
    base: str | None = Field(
        default=None,
        description="Git revision the runner configuration is compared with (default: HEAD, or "
        "the revision the hooks saw when the turn started)",
    )
    include: list[str] = Field(
        default_factory=list[str],
        description="Paths or glob patterns, relative to the project, copied even when "
        "`.gitignore` ignores them (a generated `_version.py`)",
    )
    sandbox: list[str] | None = Field(
        default=None,
        min_length=1,
        description="An argv prefix the command runs under (bwrap, sandbox-exec, docker run "
        "...); {tree}, {report_dir}, {home} and {tmp} stand for the copy, the report "
        "directory, and the run's HOME and TMPDIR",
    )

    @field_validator("command", "sandbox")
    @classmethod
    def _argv(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not part or "\0" in part for part in value):
            raise ValueError("command arguments must be non-empty and contain no NUL")
        return value

    @field_validator("include")
    @classmethod
    def _relative(cls, value: list[str]) -> list[str]:
        for pattern in value:
            pure = PurePosixPath(pattern)
            if not pattern or "\0" in pattern or pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"include pattern {pattern!r} must stay inside the project")
        return value

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        for name, text in value.items():
            if not _ENV_NAME.match(name) or "\0" in text:
                raise ValueError(f"invalid environment variable {name!r}")
        return value


class RerunStatus(StrEnum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    UNKNOWN = "unknown"


class RerunResult(BaseModel):
    """What one re-run showed. `status` is the suite's result as far as the report shows it;
    `problems` and `config_changes` are reasons the run cannot pass whatever its status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: RerunStatus
    reason: str
    runner: str | None = None
    command: list[str] = Field(default_factory=list[str])
    workdir: str = ""
    base: str | None = Field(
        default=None, description="The revision the configuration was compared with"
    )
    exit_code: int | None = None
    duration_s: float = 0.0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failing: list[str] = Field(default_factory=list[str], description="Failing tests, by name")
    passing: list[str] = Field(default_factory=list[str], exclude=True)
    report_read: bool = Field(
        default=False, description="The runner's report was read and consistent"
    )
    config_changes: list[str] = Field(default_factory=list[str])
    problems: list[str] = Field(default_factory=list[str])
    notes: list[str] = Field(default_factory=list[str])

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    def describe(self) -> str:
        parts = [f"{self.passed} passed"]
        for label, count in (
            ("failed", self.failed),
            ("errors", self.errors),
            ("skipped", self.skipped),
        ):
            if count:
                parts.append(f"{count} {label}")
        return ", ".join(parts)

    def at_least(self, minimum: int | None, source: str) -> RerunResult:
        """The result, no longer a pass when the report counted fewer than `minimum` tests."""
        if self.status is not RerunStatus.PASS or minimum is None or self.total >= minimum:
            return self
        return self.model_copy(
            update={
                "status": RerunStatus.UNKNOWN,
                "reason": f"the re-run reported {self.total} tests, fewer than the {minimum} of "
                f"{source}",
            }
        )


def infer_runner(argv: list[str]) -> Runner | None:
    names = [PurePosixPath(a).name for a in argv]
    for i, name in enumerate(names):
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if (
            name in ("pytest", "py.test")
            or (name == "-m" and nxt == "pytest")
            or name == "-mpytest"
        ):
            return "pytest"
        if (name == "-m" and nxt == "unittest") or name == "-munittest":
            return "unittest"
        if name == "go" and nxt == "test":
            return "go"
        if name == "cargo" and nxt == "test":
            return "cargo"
        if name in ("jest", "vitest"):
            return cast(Runner, name)
    if names and names[0] in ("node", "nodejs") and "--test" in argv:
        return "node"
    return None
