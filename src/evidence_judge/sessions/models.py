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

from evidence_judge.core.verdict import TamperDetection


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
    conflict: str | None = None
    """Why the output holds summary text the runner did not print, or None."""

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
    check_filters: list[str] = Field(
        default_factory=list[str],
        description="Why a lint or type-check run does not decide the whole project (explicit "
        "files, a rule selection, another configuration); empty when it does",
    )
    stop_early: str | None = Field(
        default=None,
        description="Why a failing test run may have stopped before running every test it "
        "selected (`pytest -x`, `jest --bail`, `go test -failfast`); None when it runs them all",
    )
    wrapper: str | None = Field(
        default=None,
        description="The project target or script the test, lint or type-check run goes "
        "through (`make test`, `npm run lint`, `tox -e py`), as written; None for a direct run",
    )
    wrapper_unseen: str | None = Field(
        default=None,
        description="Why the runner options the wrapper's definition passes are not known (a "
        "stop at the first failure, a filter); None for a direct run, or once the wrapper's "
        "own output or a definition the session showed names the command it runs",
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
    opaque_write: str | None = Field(
        default=None,
        description="Why the command may write files its command line does not name (a script, "
        "a git checkout of the whole tree, a glob); None when every target is named",
    )
    lasting_mutation: str | None = Field(
        default=None,
        description="Why a change the command may make can happen at any later time (a "
        "background job, a detached or scheduled process)",
    )
    run_cds: list[str] = Field(
        default_factory=list[str],
        description="Directories the command changes to before its test run (or its lint or "
        "type check), in order, as written (`?` for one only the shell knows)",
    )
    run_env: list[str] = Field(
        default_factory=list[str],
        description="Names of variables the test run's own command line sets (`CI=true "
        "pytest`), beyond ones that only change output formatting",
    )
    exports: list[str] = Field(
        default_factory=list[str],
        description="Names of variables the command exports to later commands, beyond ones that "
        "only change output formatting",
    )
    config_files: list[str] = Field(
        default_factory=list[str],
        description="Files the test run reads as its configuration although no name rule "
        "marks them (`make -f ci/rules`), as written",
    )
    test_paths: list[str] | None = Field(
        default=None,
        description="The files and directories a single test run selects when plain paths are "
        "its only filters ([] for none); None when it filters any other way",
    )
    cd_targets: list[str] = Field(
        default_factory=list[str],
        description="Directories the shell is left in for later commands (top-level `cd`, "
        "`pushd`, `popd`), in order, as written (`?` for one only the shell knows)",
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
    guard_tamper: str | None = Field(
        default=None,
        description="How the command can switch off the evidence hooks or the judge (agent "
        "settings, hook definitions, the evidence-judge install)",
    )
    source_findings: list[str] = Field(
        default_factory=list[str],
        description="Ways code the command writes to a Python file outside the tests weakens "
        "the suite (a heredoc that patches the runner), or that the judge cannot read that code",
    )
    suppressions: list[str] = Field(
        default_factory=list[str],
        description="Lint or type-check suppression comments the command writes into source "
        "files (`sed -i 's/$/  # noqa/' calc.py`, a heredoc), by label; when it writes a source "
        "file in a way the judge cannot read, every one its command line holds",
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
    check_errors: int | None = Field(
        default=None,
        description="Errors a lint or type-check run's own output counts (`Found 3 errors.`); "
        "None when it prints no count the judge can read",
    )

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
    adds_tests: bool = Field(
        default=False,
        description="The edit may add tests (a new test function, class or parametrize case), "
        "or its diff is unknown",
    )
    tests_added: int | None = Field(
        default=None,
        description="How many Python test functions the edit adds, when that can be counted",
    )
    suppressions: list[str] = Field(
        default_factory=list[str],
        description="Lint or type-check suppression comments the edit adds (`# noqa`, `# type: "
        "ignore`, `eslint-disable`)",
    )
    session_store: bool = False
    guard_tamper: str | None = Field(
        default=None,
        description="How the edit can switch off the evidence hooks or the judge (agent settings, "
        "hook definitions, the evidence-judge install)",
    )


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
    integrity: TamperDetection | None = Field(
        default=None,
        description="What an evidence log's own HMAC chain and head anchor show; None for "
        "other sources",
    )

    def by_id(self, event_id: str) -> SessionEvent | None:
        return next((e for e in self.events if e.id == event_id), None)
