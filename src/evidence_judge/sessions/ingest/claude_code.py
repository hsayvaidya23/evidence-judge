"""Claude Code session transcripts (`~/.claude/projects/<slug>/<session-id>.jsonl`).

The format is internal to Claude Code and changes between releases, so this reader is
defensive: it reads only `user` and `assistant` records, skips every other record type (the
`api-request*` copies would double-count tool calls; `attachment` records hold harness context),
accepts both `sessionId` and `session_id`, and never fails on a bad line.

The Bash tool's exit status is not a field. It is inferred from the `tool_result` block, as
documented in docs/design/coding-agent-sessions.md: `is_error` with `Exit code N` is N; a plain
success is 0; anything ambiguous is unknown.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from evidence_judge.ingest._json import JsonObject, as_list, as_object, as_str
from evidence_judge.sessions.ingest._lines import LineStats, iter_records, parse_ts
from evidence_judge.sessions.models import CallStatus, EvidenceKind, SessionRun, SessionSource
from evidence_judge.sessions.normalize import EDIT_TOOLS, SHELL_TOOLS, ToolCall, build_events

_EXIT_CODE: Final = re.compile(r"^(?:Error: )?Exit code (\d+)\s*\n?")
_DENIAL: Final = re.compile(
    r"<tool_use_error>|requires approval|permission|has been denied|doesn't want to proceed|"
    r"was rejected|not allowed|blocked by|hook",
    re.IGNORECASE,
)
_TRUNCATED: Final = re.compile(
    r"<persisted-output>|Output too large|output was truncated|\[\.\.\.\s*\d+ (?:lines|characters) truncated",
    re.IGNORECASE,
)
_BACKGROUND: Final = re.compile(r"^Command running in background", re.IGNORECASE)
_IGNORED_TYPES: Final = frozenset(
    {
        "system", "attachment", "api-request", "api-request-blob", "api-request-shape",
        "queue-operation", "last-prompt", "cost-state", "ai-title", "custom-title", "mode",
        "permission-mode", "file-history-snapshot", "file-history-delta", "pr-link",
        "agent-name", "atis-latch", "frame-link", "artifact-comment-monitor",
        "artifact-autoreact-ledger", "history-suppression", "bridge-session", "continued-in",
        "summary",
    }
)  # fmt: skip
_COMMAND_PROMPT: Final = re.compile(r"^\s*<(?:command-|local-command|bash-|system-reminder)")


@dataclass(slots=True)
class _Pending:
    name: str
    input: JsonObject
    ts: datetime | None
    cwd: str | None
    agent_id: str | None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = [
        as_str(as_object(block).get("text")) or ""
        for block in as_list(content)
        if as_object(block).get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


def _bash_result(
    block: JsonObject, tur: Any, pending: _Pending
) -> tuple[CallStatus, int | None, str, bool, bool]:
    """(status, exit code, output, truncated, background) of a Bash tool_result."""
    text = _text_of(block.get("content"))
    is_error = block.get("is_error") is True
    result = as_object(tur)
    stdout = as_str(result.get("stdout"))
    stderr = as_str(result.get("stderr")) or ""
    if stdout is not None:
        output = stdout + (("\n" if stdout and stderr else "") + stderr)
    else:
        output = _EXIT_CODE.sub("", text, count=1)
    truncated = (
        "persistedOutputPath" in result
        or _TRUNCATED.search(text) is not None
        or _TRUNCATED.search(output) is not None
    )
    background = bool(
        result.get("backgroundTaskId")
        or pending.input.get("run_in_background") is True
        or _BACKGROUND.match(text)
    )
    exit_match = _EXIT_CODE.match(text) or _EXIT_CODE.match(as_str(tur) or "")
    if is_error:
        if exit_match is not None:
            return CallStatus.ERROR, int(exit_match.group(1)), output, truncated, background
        if result.get("interrupted") is True or "timedOutAfterMs" in result:
            return CallStatus.INTERRUPTED, None, output, truncated, background
        if stdout is None and _DENIAL.search(text):
            return CallStatus.DENIED, None, "", False, False
        return CallStatus.ERROR, None, output, truncated, background
    if result.get("interrupted") is True or "timedOutAfterMs" in result:
        return CallStatus.INTERRUPTED, None, output, truncated, background
    if background:
        return CallStatus.OK, None, output, True, True
    if stdout is None or "returnCodeInterpretation" in result:
        return CallStatus.OK, None, output, truncated, background
    return CallStatus.OK, 0, output, truncated, background


def _tool_call(block: JsonObject, tur: Any, pending: _Pending, ts: datetime | None) -> ToolCall:
    if pending.name in SHELL_TOOLS:
        status, exit_code, output, truncated, background = _bash_result(block, tur, pending)
        return ToolCall(
            name=pending.name,
            input=pending.input,
            status=status,
            exit_code=exit_code,
            output=output,
            output_truncated=truncated,
            background=background,
            result=as_object(tur),
            ts=ts or pending.ts,
            cwd=pending.cwd,
            agent_id=pending.agent_id,
        )
    status = CallStatus.OK
    if block.get("is_error") is True:
        text = _text_of(block.get("content"))
        did_not_apply = pending.name in EDIT_TOOLS or _DENIAL.search(text) is not None
        status = CallStatus.DENIED if did_not_apply else CallStatus.ERROR
    return ToolCall(
        name=pending.name,
        input=pending.input,
        status=status,
        result=as_object(tur),
        ts=ts or pending.ts,
        cwd=pending.cwd,
        agent_id=pending.agent_id,
    )


@dataclass(slots=True)
class _Reading:
    """What `load_transcript` has gathered so far."""

    pending: dict[str, _Pending] = field(default_factory=dict[str, _Pending])
    calls: list[ToolCall] = field(default_factory=list[ToolCall])
    session_id: str | None = None
    version: str | None = None
    cwd: str | None = None
    branch: str | None = None
    task: str | None = None
    final_id: str | None = None
    final_parts: list[str] = field(default_factory=list[str])
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    unknown_blocks: dict[str, int] = field(default_factory=dict[str, int])


def _read_header(reading: _Reading, record: JsonObject, sidechain: bool) -> datetime | None:
    """Take the session's identity from a record; returns the record's timestamp."""
    reading.session_id = (
        reading.session_id or as_str(record.get("sessionId")) or as_str(record.get("session_id"))
    )
    reading.version = as_str(record.get("version")) or reading.version
    if not sidechain:
        reading.cwd = reading.cwd or as_str(record.get("cwd"))
        reading.branch = reading.branch or as_str(record.get("gitBranch")) or None
    ts = parse_ts(record.get("timestamp"))
    if ts is not None:
        reading.first_ts = reading.first_ts or ts
        reading.last_ts = ts
    return ts


def _read_assistant(
    reading: _Reading, record: JsonObject, sidechain: bool, ts: datetime | None
) -> None:
    """Queue the tool calls of an assistant record and keep its text as the final answer."""
    message = as_object(record.get("message"))
    message_id = as_str(message.get("id"))
    for block in map(as_object, as_list(message.get("content"))):
        block_type = as_str(block.get("type"))
        if block_type == "tool_use":
            use_id = as_str(block.get("id"))
            if use_id:
                reading.pending[use_id] = _Pending(
                    name=as_str(block.get("name")) or "?",
                    input=as_object(block.get("input")),
                    ts=ts,
                    cwd=as_str(record.get("cwd")),
                    agent_id="sidechain" if sidechain else None,
                )
        elif block_type == "text" and not sidechain:
            text = as_str(block.get("text")) or ""
            if message_id != reading.final_id or reading.final_id is None:
                reading.final_id, reading.final_parts = message_id, []
            if text.strip():
                reading.final_parts.append(text)
        elif block_type not in ("thinking", "redacted_thinking") and block_type:
            reading.unknown_blocks[block_type] = reading.unknown_blocks.get(block_type, 0) + 1


def _read_user(reading: _Reading, record: JsonObject, sidechain: bool, ts: datetime | None) -> None:
    """Match tool results to their calls, and take the first real prompt as the task."""
    content = as_object(record.get("message")).get("content")
    prompt = not sidechain and record.get("isMeta") is not True
    if isinstance(content, str):
        if reading.task is None and prompt and not _COMMAND_PROMPT.match(content):
            reading.task = content
        return
    blocks = [as_object(b) for b in as_list(content)]
    results = [b for b in blocks if b.get("type") == "tool_result"]
    tur = record.get("toolUseResult") if len(results) == 1 else None
    for block in results:
        use_id = as_str(block.get("tool_use_id"))
        match = reading.pending.pop(use_id, None) if use_id else None
        if match is not None:
            reading.calls.append(_tool_call(block, tur, match, ts))
    if not results and reading.task is None and prompt:
        text = _text_of(content)
        if text and not _COMMAND_PROMPT.match(text):
            reading.task = text


def _notes(stats: LineStats, reading: _Reading, dropped: int) -> list[str]:
    notes = stats.notes()
    for kind, count in sorted(stats.skipped_types.items()):
        notes.append(f"skipped {count} record(s) of unknown type {kind!r}")
    for kind, count in sorted(reading.unknown_blocks.items()):
        notes.append(f"skipped {count} content block(s) of unknown type {kind!r}")
    if reading.pending:
        notes.append(f"{len(reading.pending)} tool call(s) have no recorded result")
    if dropped:
        notes.append(f"{dropped} event(s) over the event cap were dropped")
    return notes


def load_transcript(path: Path) -> SessionRun:
    """Read one Claude Code transcript into a SessionRun."""
    stats = LineStats()
    reading = _Reading()
    for _, record in iter_records(path, stats):
        kind = as_str(record.get("type")) or "<none>"
        if kind not in ("user", "assistant"):
            if kind not in _IGNORED_TYPES:
                stats.skip(kind)
            continue
        sidechain = record.get("isSidechain") is True
        ts = _read_header(reading, record, sidechain)
        if kind == "assistant":
            _read_assistant(reading, record, sidechain, ts)
        else:
            _read_user(reading, record, sidechain, ts)

    calls = [
        *reading.calls,
        *(
            ToolCall(
                name=leftover.name,
                input=leftover.input,
                status=CallStatus.NO_RESULT,
                ts=leftover.ts,
                cwd=leftover.cwd,
                agent_id=leftover.agent_id,
            )
            for leftover in reading.pending.values()
        ),
    ]
    events, dropped = build_events(calls, reading.cwd)
    return SessionRun(
        source=SessionSource.CLAUDE_CODE,
        evidence=EvidenceKind.TRANSCRIPT,
        session_id=reading.session_id,
        harness_version=reading.version,
        cwd=reading.cwd,
        git_branch=reading.branch,
        task=reading.task,
        final_answer="\n\n".join(reading.final_parts) or None,
        started_at=reading.first_ts,
        ended_at=reading.last_ts,
        events=events,
        complete=not stats.damaged and not dropped,
        notes=_notes(stats, reading, dropped),
    )


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def project_slug(directory: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(directory))


_SESSION_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def locate(spec: str, *, project: Path | None = None, root: Path | None = None) -> Path:
    """Resolve `--claude-code`: a transcript path, a session id, or `latest` (the newest
    transcript of `project`, default the current directory). Raises FileNotFoundError."""
    candidate = Path(spec).expanduser()
    if candidate.suffix == ".jsonl" or os.sep in spec:
        if not candidate.is_file():
            raise FileNotFoundError(f"no transcript at {candidate}")
        return candidate
    base = root or projects_dir()
    if spec == "latest":
        directory = (project or Path.cwd()).resolve()
        slug = project_slug(directory)
        folders = [base / slug] if (base / slug).is_dir() else []
        if not folders and len(slug) > 200 and base.is_dir():
            folders = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(slug[:200])]
        transcripts = [f for folder in folders for f in folder.glob("*.jsonl") if f.is_file()]
        if not transcripts:
            raise FileNotFoundError(f"no Claude Code transcripts for {directory} under {base}")
        return max(transcripts, key=lambda f: f.stat().st_mtime)
    if not _SESSION_ID.match(spec):
        raise FileNotFoundError(f"{spec!r} is not a transcript path, a session id or 'latest'")
    matches = sorted(base.glob(f"*/{spec}.jsonl")) if base.is_dir() else []
    if not matches:
        raise FileNotFoundError(f"no transcript for session {spec} under {base}")
    return matches[0]
