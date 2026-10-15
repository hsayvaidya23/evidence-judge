"""Turn the tool calls a source recorded into `SessionRun` events.

Every ingester reduces its format to `ToolCall`s (a tool name, its input, its result) and lets
this module decide what each call is: a command (parsed and classified), an edit (with its
test-suite findings), a read, a search, a subagent, or another tool.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from evidence_judge.sessions import edits, shell
from evidence_judge.sessions.models import (
    CallStatus,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    OtherToolEvent,
    SearchEvent,
    SessionEvent,
    SubagentEvent,
)
from evidence_judge.sessions.summaries import parse_summary

OUTPUT_HEAD: Final = 4_000
OUTPUT_TAIL: Final = 12_000
MAX_EVENTS: Final = 20_000

SHELL_TOOLS: Final = frozenset(
    {"Bash", "shell", "exec", "exec_command", "local_shell", "run_shell_command", "container.exec"}
)
EDIT_TOOLS: Final = frozenset(
    {"Edit", "MultiEdit", "Write", "NotebookEdit", "apply_patch", "replace", "write_file", "edit"}
)
READ_TOOLS: Final = frozenset({"Read", "read_file", "read_many_files", "NotebookRead", "view"})
SEARCH_TOOLS: Final = frozenset(
    {"Glob", "Grep", "LS", "glob", "grep", "search_file_content", "list_directory", "ls"}
)
SUBAGENT_TOOLS: Final = frozenset({"Task", "Agent"})
SAFE_TOOLS: Final = frozenset(
    {
        "TodoWrite", "TodoRead", "WebFetch", "WebSearch", "ToolSearch", "AskUserQuestion",
        "ExitPlanMode", "EnterPlanMode", "BashOutput", "TaskOutput", "KillShell", "KillBash",
        "TaskStop", "TaskList", "TaskGet", "ListMcpResourcesTool", "ReadMcpResourceTool",
        "Skill", "SlashCommand", "web_fetch", "google_web_search", "web_search", "save_memory",
        "update_plan", "todo_list", "Monitor", "SendMessage", "ListAgents",
    }
)  # fmt: skip
"""Tools that cannot change the workspace. Any other tool, MCP tools included, may."""


@dataclass(slots=True)
class ToolCall:
    name: str
    input: Mapping[str, Any]
    status: CallStatus
    exit_code: int | None = None
    output: str = ""
    output_truncated: bool = False
    background: bool = False
    result: Mapping[str, Any] = field(default_factory=dict[str, Any])
    """Structured result fields (Claude Code `toolUseResult`, a recorder's `result`)."""
    ts: datetime | None = None
    cwd: str | None = None
    agent_id: str | None = None
    by_user: bool = False
    edits: list[tuple[str, str, list[str] | None, list[str] | None]] | None = None
    """Pre-computed (path, change, removed, added) for sources that describe edits themselves."""


def excerpt(text: str) -> tuple[str, bool]:
    """Head and tail of `text`, and whether anything was cut."""
    if len(text) <= OUTPUT_HEAD + OUTPUT_TAIL:
        return text, False
    cut = len(text) - OUTPUT_HEAD - OUTPUT_TAIL
    return f"{text[:OUTPUT_HEAD]}\n[... {cut} characters omitted ...]\n{text[-OUTPUT_TAIL:]}", True


def relative(path: str, cwd: str | None) -> str:
    """`path` relative to `cwd` when it lies inside it, else normalised as given."""
    norm = posixpath.normpath(path) if path else path
    if cwd and posixpath.isabs(norm):
        root = posixpath.normpath(cwd)
        if norm == root:
            return "."
        if norm.startswith(root.rstrip("/") + "/"):
            return norm[len(root.rstrip("/")) + 1 :]
    return norm


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _path(call: ToolCall) -> str:
    for key in ("file_path", "notebook_path", "path", "absolute_path", "filePath"):
        if (value := _str(call.input.get(key))) or (value := _str(call.result.get(key))):
            return value
    return ""


def _whole_texts(call: ToolCall) -> tuple[str, str] | None:
    """The file before and after a Claude Code Edit, MultiEdit or Write, when the result
    records the original file."""
    original = call.result.get("originalFile")
    if not isinstance(original, str):
        return None
    if call.name in ("Write", "write_file"):
        content = _str(call.input.get("content"))
        return None if content is None else (original, content)
    raw = call.input.get("edits") if call.name == "MultiEdit" else [call.input]
    updated = original
    for item in raw if isinstance(raw, list) else []:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, Mapping):
            return None
        old = _str(item.get("old_string"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        new = _str(item.get("new_string"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if old is None or new is None or (old and old not in updated):
            return None
        every = item.get("replace_all") is True  # pyright: ignore[reportUnknownMemberType]
        updated = updated.replace(old, new) if every else updated.replace(old, new, 1)
    return original, updated


def _claude_edit(call: ToolCall) -> tuple[str, list[str] | None, list[str] | None]:
    """(change, removed, added) for Claude Code style edit tools."""
    patch = call.result.get("structuredPatch")
    patched = edits.patch_lines(patch) if patch else None
    kind = _str(call.result.get("type"))
    if call.name == "Write" or call.name == "write_file":
        content = _str(call.input.get("content")) or ""
        if kind == "create":
            return "add", [], content.splitlines()
        if patched is not None:
            return "modify", *patched
        original = call.result.get("originalFile")
        if isinstance(original, str):
            return "modify", *edits.line_diff(original, content)
        return "unknown", None, content.splitlines()
    if patched is not None:
        return "modify", *patched
    pairs: list[tuple[str, str]] = []
    if call.name == "MultiEdit" and isinstance(raw := call.input.get("edits"), list):
        for item in raw:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, Mapping):
                old = _str(item.get("old_string"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                new = _str(item.get("new_string"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                if old is not None and new is not None:
                    pairs.append((old, new))
    else:
        old = _str(call.input.get("old_string"))
        new = _str(call.input.get("new_string"))
        if old is not None and new is not None:
            pairs.append((old, new))
    if call.name == "NotebookEdit":
        source = _str(call.input.get("new_source")) or ""
        mode = _str(call.input.get("edit_mode"))
        if mode == "delete":
            return "modify", None, []
        return "modify", None if mode != "insert" else [], source.splitlines()
    if not pairs:
        return "modify", None, None
    removed: list[str] = []
    added: list[str] = []
    for old, new in pairs:
        r, a = edits.line_diff(old, new)
        removed.extend(r)
        added.extend(a)
    return "modify", removed, added


class EventBuilder:
    """Numbers events in execution order and assigns ids (`cmd#1`, `edit#1`, ...)."""

    def __init__(self, cwd: str | None) -> None:
        self.cwd = cwd
        self.events: list[SessionEvent] = []
        self.counters: dict[str, int] = {}
        self.dropped = 0

    def _id(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}#{self.counters[prefix]}"

    def _append(self, event: SessionEvent) -> None:
        if len(self.events) >= MAX_EVENTS:
            self.dropped += 1
            return
        self.events.append(event)

    @property
    def seq(self) -> int:
        return len(self.events)

    def add(self, call: ToolCall) -> None:
        cwd = call.cwd or self.cwd
        common: dict[str, Any] = {"ts": call.ts, "tool": call.name, "agent_id": call.agent_id}
        if call.edits is not None:
            for path, change, removed, added in call.edits:
                self._edit(call, path, change, removed, added, common)
            return
        if call.name in SHELL_TOOLS:
            self._command(call, cwd, common)
        elif call.name in EDIT_TOOLS:
            change, removed, added = _claude_edit(call)
            self._edit(call, _path(call), change, removed, added, common, _whole_texts(call))
        elif call.name in READ_TOOLS:
            self._append(
                FileReadEvent(
                    id=self._id("read"), seq=self.seq, path=relative(_path(call), cwd), **common
                )
            )
        elif call.name in SEARCH_TOOLS:
            pattern = _str(call.input.get("pattern")) or _str(call.input.get("path")) or ""
            self._append(
                SearchEvent(id=self._id("search"), seq=self.seq, pattern=pattern, **common)
            )
        elif call.name in SUBAGENT_TOOLS:
            self._append(
                SubagentEvent(
                    id=self._id("agent"),
                    seq=self.seq,
                    description=_str(call.input.get("description")) or "",
                    subagent_type=_str(call.input.get("subagent_type")),
                    **common,
                )
            )
        else:
            self._append(
                OtherToolEvent(
                    id=self._id("tool"),
                    seq=self.seq,
                    name=call.name,
                    may_mutate=call.name not in SAFE_TOOLS and call.status is not CallStatus.DENIED,
                    **common,
                )
            )

    def _command(self, call: ToolCall, cwd: str | None, common: dict[str, Any]) -> None:
        command = _str(call.input.get("command"))
        if command is None and isinstance(argv := call.input.get("command"), list):
            command = shell.unwrap_shell([str(a) for a in argv])  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        command = command or ""
        analysis = shell.analyse(command, cwd)
        output, _ = excerpt(call.output)
        summary = (
            parse_summary(analysis.runners, call.output)
            if analysis.is_test and call.status is not CallStatus.DENIED
            else None
        )
        self._append(
            CommandEvent(
                id=self._id("cmd"),
                seq=self.seq,
                command=command,
                cwd=cwd,
                status=call.status,
                exit_code=call.exit_code,
                output=output,
                output_chars=len(call.output),
                output_truncated=call.output_truncated,
                background=call.background,
                analysis=analysis,
                summary=summary,
                **common,
            )
        )

    def _edit(
        self,
        call: ToolCall,
        path: str,
        change: str,
        removed: list[str] | None,
        added: list[str] | None,
        common: dict[str, Any],
        texts: tuple[str, str] | None = None,
    ) -> None:
        rel = relative(path, call.cwd or self.cwd)
        findings = (
            edits.suite_findings(rel, change, removed, added, texts)
            if call.status is not CallStatus.DENIED
            else []
        )
        self._append(
            FileEditEvent(
                id=self._id("edit"),
                seq=self.seq,
                path=rel,
                change=change if change in ("add", "modify", "delete") else "unknown",  # pyright: ignore[reportArgumentType]
                status=call.status,
                lines_added=None if added is None else len(added),
                lines_removed=None if removed is None else len(removed),
                by_user=call.by_user or bool(call.result.get("userModified")),
                test_file=edits.is_test_path(rel),
                test_findings=findings,
                adds_tests=call.status is not CallStatus.DENIED and edits.adds_tests(added),
                session_store=shell.SESSION_STORE.search(path) is not None,
                **common,
            )
        )


def build_events(calls: Iterable[ToolCall], cwd: str | None) -> tuple[list[SessionEvent], int]:
    builder = EventBuilder(cwd)
    for call in calls:
        builder.add(call)
    return builder.events, builder.dropped


def extend_events(
    events: list[SessionEvent], calls: Iterable[ToolCall], cwd: str | None
) -> list[SessionEvent]:
    """`events` followed by `calls`, numbered on from the existing ids."""
    builder = EventBuilder(cwd)
    builder.events = list(events)
    for event in events:
        prefix, _, number = event.id.partition("#")
        if number.isdigit():
            builder.counters[prefix] = max(builder.counters.get(prefix, 0), int(number))
    for call in calls:
        builder.add(call)
    return builder.events
