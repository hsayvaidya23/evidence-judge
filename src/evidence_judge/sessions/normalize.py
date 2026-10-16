"""Turn the tool calls a source recorded into `SessionRun` events.

Every ingester reduces its format to `ToolCall`s (a tool name, its input, its result) and lets
this module decide what each call is: a command (parsed and classified), an edit (with its
test-suite findings), a read, a search, a subagent, or another tool.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from evidence_judge.sessions import edits, shell
from evidence_judge.sessions.models import (
    CallStatus,
    Category,
    CommandAnalysis,
    CommandEvent,
    FileEditEvent,
    FileReadEvent,
    OtherToolEvent,
    SearchEvent,
    SessionEvent,
    SubagentEvent,
)
from evidence_judge.sessions.shell import wrappers
from evidence_judge.sessions.summaries import check_errors, parse_summary

OUTPUT_HEAD: Final = 4_000
OUTPUT_TAIL: Final = 12_000
MAX_EVENTS: Final = 20_000
_NUMBERED: Final = re.compile(r"^ *\d+(?:\t|\u2192)", re.MULTILINE)

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
        self.opened: dict[str, set[str]] = {}
        """Path literals each test module the session shows reads (test data)."""
        self.imports: dict[str, dict[str, set[str] | None]] = {}
        """Modules each test module the session shows imports, and the names it takes."""
        self.constants: dict[str, set[str] | None] = {}
        """Module-level names each edit of a Python file outside the tests changed, by edit id."""
        self.definitions: dict[str, str | None] = {}
        """The text the session last showed of each wrapper definition (`Makefile`,
        `package.json`), by path; None once it changed in a way the judge cannot follow."""
        self.moved = False
        """A command left the shell in another directory, so a wrapper may read another
        definition than the one at its path."""

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

    def _test_source(self, path: str, source: str | None) -> None:
        if source and path.endswith(".py") and edits.is_test_path(path):
            opened = edits.opened_paths(source)
            if opened:
                self.opened.setdefault(path, set()).update(opened)
            for module, names in edits.imported_names(source).items():
                known = self.imports.setdefault(path, {}).get(module, set())
                merged = None if names is None or known is None else known | names
                self.imports[path][module] = merged

    def _sources(self, call: ToolCall, cwd: str | None) -> None:
        """Test module text the call shows: an edit's whole file or added lines, a read's
        result, or a command that names a test module and prints it."""
        if call.status is CallStatus.DENIED:
            return
        if call.edits is not None:
            for path, _, _, added in call.edits:
                self._test_source(relative(path, cwd), "\n".join(added or []))
            return
        rel = relative(_path(call), cwd)
        if call.name in EDIT_TOOLS:
            texts = _whole_texts(call)
            _, _, added = _claude_edit(call)
            self._test_source(rel, texts[1] if texts else "\n".join(added or []))
        elif call.name in READ_TOOLS:
            file = call.result.get("file")
            content = file.get("content") if isinstance(file, Mapping) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            text = content if isinstance(content, str) else _NUMBERED.sub("", call.output)
            self._test_source(rel, text)
        elif call.name in SHELL_TOOLS:
            command = _str(call.input.get("command")) or ""
            for word in re.findall(r"[\w./-]+\.py\b", command):
                self._test_source(relative(word, cwd), call.output)

    def _key(self, path: str, cwd: str | None) -> str:
        joined = posixpath.join(cwd, path) if cwd and not posixpath.isabs(path) else path
        return relative(joined, self.cwd)

    def _forget(self, paths: Iterable[str] | None, cwd: str | None) -> None:
        """Mark the definitions among `paths` changed; every one for None."""
        if paths is None:
            self.definitions = dict.fromkeys(self.definitions)
            return
        for path in paths:
            if posixpath.basename(path) in wrappers.DEFINITION_FILES:
                self.definitions[self._key(path, cwd)] = None

    def _shown(self, call: ToolCall, cwd: str | None) -> None:
        """Record the definition text an edit, a whole-file read or a patch shows, and forget a
        definition a call may have changed unseen."""
        if call.status is CallStatus.DENIED or call.name in SHELL_TOOLS:
            return
        if call.edits is not None:
            for path, change, _, added in call.edits:
                self._forget([path], cwd)
                if change == "add" and posixpath.basename(path) in wrappers.DEFINITION_FILES:
                    self.definitions[self._key(path, cwd)] = "\n".join(added or [])
            return
        path = _path(call)
        known = posixpath.basename(path) in wrappers.DEFINITION_FILES
        if call.name in EDIT_TOOLS and known:
            self.definitions[self._key(path, cwd)] = _edited_text(
                call, self.definitions.get(self._key(path, cwd))
            )
        elif call.name in READ_TOOLS and known and (text := _whole_read(call)) is not None:
            self.definitions[self._key(path, cwd)] = text
        elif call.name in SUBAGENT_TOOLS or (
            call.name not in (EDIT_TOOLS | READ_TOOLS | SEARCH_TOOLS | SAFE_TOOLS)
        ):
            self._forget(None, cwd)

    def _after_command(self, call: ToolCall, event: CommandEvent, cwd: str | None) -> None:
        if event.status is CallStatus.DENIED:
            return
        analysis = event.analysis
        if analysis.cd_targets:
            self.moved = True
        if analysis.opaque_write or analysis.lasting_mutation or analysis.parse_error:
            self._forget(None, cwd)
        self._forget([*analysis.touched_paths, *analysis.deleted_paths], cwd)
        shown = _CAT.fullmatch(event.command.strip())
        if (
            shown
            and posixpath.basename(shown[1]) in wrappers.DEFINITION_FILES
            and event.exit_code == 0
            and not call.output_truncated
        ):
            self.definitions[self._key(shown[1], cwd)] = call.output

    def _known_definitions(self, analysis: CommandAnalysis, cwd: str | None) -> dict[str, str]:
        if self.moved or analysis.run_cds:
            return {}
        return {
            name: text
            for name in wrappers.DEFINITION_FILES
            if isinstance(text := self.definitions.get(self._key(name, cwd)), str)
        }

    def finish(self) -> None:
        """Mark edits of files a test module reads as data: changing expected values weakens the
        suite like changing an assertion."""
        for i, event in enumerate(self.events):
            if not isinstance(event, FileEditEvent) or event.change == "add":
                continue
            if event.status is CallStatus.DENIED:
                continue
            name = posixpath.basename(event.path)
            reader = edits.data_reader(event.path, self.opened)
            expected = edits.expected_module(
                event.path, self.constants.get(event.id, set()), self.imports
            )
            if reader is not None:
                finding = (
                    f"changed {name}, which {posixpath.basename(reader)} reads as test data "
                    "(expected values decide what passes)"
                )
            elif expected is not None:
                finding = (
                    f"changed {', '.join(expected[1])} in {name}, which "
                    f"{posixpath.basename(expected[0])} imports as expected values (expected "
                    "values decide what passes)"
                )
            else:
                continue
            if finding not in event.test_findings:
                self.events[i] = event.model_copy(
                    update={"test_findings": [*event.test_findings, finding]}
                )

    def add(self, call: ToolCall) -> None:
        cwd = call.cwd or self.cwd
        self._sources(call, cwd)
        self._shown(call, cwd)
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
        if analysis.wrapper is not None and call.status is not CallStatus.DENIED:
            known = self._known_definitions(analysis, cwd)
            analysis = wrappers.resolve(analysis, call.output, known, cwd)
        output, _ = excerpt(call.output)
        summary = (
            parse_summary(analysis.runners, call.output)
            if analysis.is_test and call.status is not CallStatus.DENIED
            else None
        )
        checks = (Category.LINT, Category.TYPECHECK)
        errors = (
            check_errors(call.output)
            if any(c in checks for c in analysis.categories)
            and call.status is not CallStatus.DENIED
            else None
        )
        event = CommandEvent(
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
            check_errors=errors,
            **common,
        )
        self._append(event)
        self._after_command(call, event, cwd)

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
        event_id = self._id("edit")
        if rel.endswith(".py") and not edits.is_test_path(rel) and change == "modify":
            self.constants[event_id] = edits.changed_constants(removed, added, texts)
        findings = (
            edits.suite_findings(rel, change, removed, added, texts)
            if call.status is not CallStatus.DENIED
            else []
        )
        self._append(
            FileEditEvent(
                id=event_id,
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
                tests_added=edits.tests_added(removed, added, rel),
                suppressions=edits.added_suppressions(removed, added, texts)
                if call.status is not CallStatus.DENIED
                else [],
                session_store=shell.SESSION_STORE.search(path) is not None,
                guard_tamper=_guard_tamper(path, rel, added, call.status),
                **common,
            )
        )


_CAT: Final = re.compile(r"cat\s+([\w./-]+)")


def _whole_read(call: ToolCall) -> str | None:
    """The whole file a read shows, or None for part of it."""
    file = call.result.get("file")
    if isinstance(file, Mapping):
        content = file.get("content")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        start: object = file.get("startLine")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        lines = file.get("numLines")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        total = file.get("totalLines")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        partial = start not in (1, None) or (
            isinstance(lines, int) and isinstance(total, int) and lines < total
        )
        return content if isinstance(content, str) and not partial else None
    if any(key in call.input for key in ("offset", "limit", "start_line", "end_line")):
        return None
    return _NUMBERED.sub("", call.output) if call.output else None


def _edited_text(call: ToolCall, known: str | None) -> str | None:
    """A definition's text after an edit: the whole file when the source records it, or the
    known text with the replacement applied."""
    if (texts := _whole_texts(call)) is not None:
        return texts[1]
    if call.name in ("Write", "write_file"):
        return _str(call.input.get("content"))
    old, new = _str(call.input.get("old_string")), _str(call.input.get("new_string"))
    if known is None or old is None or new is None or not old or old not in known:
        return None
    every = call.input.get("replace_all") is True
    return known.replace(old, new) if every else known.replace(old, new, 1)


def _guard_tamper(path: str, rel: str, added: list[str] | None, status: CallStatus) -> str | None:
    why = shell.guard_path_reason(path, rel) if status is not CallStatus.DENIED else None
    return shell.hook_switch_reason(why, "\n".join(added or [])) if why else None


def build_events(calls: Iterable[ToolCall], cwd: str | None) -> tuple[list[SessionEvent], int]:
    builder = EventBuilder(cwd)
    for call in calls:
        builder.add(call)
    builder.finish()
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
    builder.finish()
    return builder.events
