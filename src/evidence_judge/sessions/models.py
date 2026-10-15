"""The normalised record of a coding-agent session: what the harness says the agent ran and
changed, in execution order.

Every source (a Claude Code transcript, a Codex stream or rollout, an evidence-recorder log) is
read into one `SessionRun`. Analysis that needs the raw command or output (shell parsing, test
summaries, test-file findings) is done once at ingest and stored as typed fields, so redacting
the strings afterwards never changes what the checkers decide.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionSource(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    GEMINI = "gemini"


class EvidenceKind(StrEnum):
    """Who wrote the record the run was read from."""

    TRANSCRIPT = "transcript"
    """The agent's own process (a Claude Code transcript, a Codex rollout)."""
    HARNESS_STREAM = "harness_stream"
    """Stdout of the harness, saved by whoever ran it (`codex exec --json`)."""
    EVIDENCE_LOG = "evidence_log"
    """A recorder hook the harness runs after every tool call."""


class Category(StrEnum):
    TEST = "test"
    BUILD = "build"
    LINT = "lint"
    TYPECHECK = "typecheck"
    FORMAT = "format"
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    UNKNOWN = "unknown"


class CallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    """The call ran and failed (a non-zero exit, a tool error)."""
    DENIED = "denied"
    """The call never ran: refused by a permission rule or rejected as malformed."""
    INTERRUPTED = "interrupted"
    NO_RESULT = "no_result"
    """The log has the call but not its result; it may or may not have run."""


class TestSummary(BaseModel):
    """Counts parsed from a test runner's own output."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    __test__ = False

    framework: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    deselected: int = 0
    """Tests the run left out on purpose (pytest deselected, cargo filtered out)."""
    no_tests: bool = False

    @property
    def green(self) -> bool:
        return self.passed > 0 and self.failed == 0 and self.errors == 0 and not self.no_tests

    @property
    def red(self) -> bool:
        return self.failed > 0 or self.errors > 0

    def describe(self) -> str:
        if self.no_tests:
            return f"{self.framework}: no tests ran"
        parts = [f"{self.passed} passed"]
        for label, count in (
            ("failed", self.failed),
            ("errors", self.errors),
            ("skipped", self.skipped),
            ("deselected", self.deselected),
        ):
            if count:
                parts.append(f"{count} {label}")
        return f"{self.framework}: " + ", ".join(parts)


class CommandAnalysis(BaseModel):
    """What a shell command does, from parsing it (never from running it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    categories: list[Category] = Field(default_factory=list[Category])
    programs: list[str] = Field(default_factory=list[str])
    runners: list[str] = Field(
        default_factory=list[str], description="Test runners the command invokes"
    )
    test_filters: list[str] = Field(
        default_factory=list[str],
        description="Why a test run is a subset of the suite (empty for a whole-suite run)",
    )
    collect_only: bool = False
    exit_masked_by: str | None = Field(
        default=None,
        description="Why the command's exit status is not the runner's (|| true, ; exit 0, a pipe)",
    )
    output_filter: str | None = Field(
        default=None, description="The program the runner's output is piped into"
    )
    agent_text: bool = Field(
        default=False,
        description="The command line itself holds test-summary text (echo, printf, heredoc)",
    )
    mutation: str | None = Field(
        default=None, description="Why the command may change the workspace; None if it cannot"
    )
    deleted_paths: list[str] = Field(default_factory=list[str])
    touched_paths: list[str] = Field(
        default_factory=list[str], description="Paths a known file-changing program names"
    )
    mutation_after_check: bool = Field(
        default=False,
        description="A command that may change files runs after the test, build or lint step",
    )
    session_store: bool = Field(
        default=False, description="The command touches a harness session store"
    )
    harness_change: str | None = Field(
        default=None,
        description="How the command changes what later commands run (an alias, a function, PATH)",
    )
    parse_error: str | None = None

    @property
    def is_test(self) -> bool:
        return bool(self.runners) and not self.collect_only

    @property
    def mutates(self) -> bool:
        return self.mutation is not None


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    seq: int = Field(ge=0, description="Position in execution order")
    ts: datetime | None = None
    tool: str
    agent_id: str | None = Field(default=None, description="The subagent that made the call")


class CommandEvent(_Event):
    type: Literal["command"] = "command"
    command: str
    cwd: str | None = None
    status: CallStatus
    exit_code: int | None
    output: str = Field(default="", description="Head and tail of the combined output")
    output_chars: int = 0
    output_truncated: bool = Field(
        default=False, description="The recorded output is not the whole output"
    )
    background: bool = False
    analysis: CommandAnalysis
    summary: TestSummary | None = None

    @property
    def ran(self) -> bool:
        return self.status not in (CallStatus.DENIED,)


class FileEditEvent(_Event):
    type: Literal["edit"] = "edit"
    path: str
    change: Literal["add", "modify", "delete", "unknown"]
    status: CallStatus = CallStatus.OK
    lines_added: int | None = None
    lines_removed: int | None = None
    by_user: bool = False
    test_file: bool = False
    test_findings: list[str] = Field(
        default_factory=list[str],
        description="Ways the edit weakens the test suite (skip markers, removed assertions, ...)",
    )
    session_store: bool = False


class FileReadEvent(_Event):
    type: Literal["read"] = "read"
    path: str


class SearchEvent(_Event):
    type: Literal["search"] = "search"
    pattern: str


class SubagentEvent(_Event):
    type: Literal["subagent"] = "subagent"
    description: str = ""
    subagent_type: str | None = None
    transcript_loaded: bool = False


class OtherToolEvent(_Event):
    type: Literal["tool"] = "tool"
    name: str
    may_mutate: bool


SessionEvent = Annotated[
    CommandEvent | FileEditEvent | FileReadEvent | SearchEvent | SubagentEvent | OtherToolEvent,
    Field(discriminator="type"),
]


class SessionRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SessionSource
    evidence: EvidenceKind
    session_id: str | None = None
    harness_version: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    task: str | None = None
    final_answer: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    events: list[SessionEvent] = Field(default_factory=list[SessionEvent])
    complete: bool = Field(
        default=True,
        description="False when the record could not be read completely (bad lines, size caps, "
        "a broken hash chain)",
    )
    notes: list[str] = Field(default_factory=list[str], description="What ingest skipped and why")

    def by_id(self, event_id: str) -> SessionEvent | None:
        return next((e for e in self.events if e.id == event_id), None)
