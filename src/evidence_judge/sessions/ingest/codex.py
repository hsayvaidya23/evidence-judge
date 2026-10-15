"""Codex CLI: the `codex exec --json` event stream and `~/.codex/sessions` rollout files.

The exec stream is the harness's stdout, so a caller that saved it holds evidence the agent
never had (`EvidenceKind.HARNESS_STREAM`). Rollout files are written by the agent's process and
change between releases; this reader understands both generations of their `event_msg` records
(`exec_command_end`/`patch_apply_end`, and `item_completed` with `CommandExecution`/
`FileChange` items) and skips everything else.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Final

from evidence_judge.ingest._json import JsonObject, as_int, as_list, as_object, as_str
from evidence_judge.sessions import edits
from evidence_judge.sessions.ingest._lines import LineStats, iter_records, parse_ts
from evidence_judge.sessions.models import CallStatus, EvidenceKind, SessionRun, SessionSource
from evidence_judge.sessions.normalize import ToolCall, build_events
from evidence_judge.sessions.shell import tokenize, unwrap_shell

_KINDS: Final = {"add": "add", "create": "add", "delete": "delete", "update": "modify"}
_STREAM_TYPES: Final = frozenset(
    {"thread.started", "turn.started", "turn.completed", "turn.failed", "item.started",
     "item.updated", "item.completed", "error"}
)  # fmt: skip
_ROLLOUT_TYPES: Final = frozenset(
    {"session_meta", "response_item", "event_msg", "turn_context", "compacted"}
)


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        return _strip_wrapper(value)
    return unwrap_shell([str(v) for v in as_list(value)])


def _strip_wrapper(command: str) -> str:
    """`/bin/zsh -lc 'pytest -q'` (how Codex records a shell call) as `pytest -q`."""
    try:
        words = tokenize(command)
    except ValueError:
        return command
    if len(words) == 3 and not any(w in ("|", "&&", "||", ";", "\n") for w in words):
        return unwrap_shell(words)
    return command


def _status(raw: str | None, exit_code: int | None) -> CallStatus:
    if raw == "declined":
        return CallStatus.DENIED
    if raw in ("in_progress", None) and exit_code is None:
        return CallStatus.NO_RESULT
    if exit_code not in (None, 0) or raw == "failed":
        return CallStatus.ERROR
    return CallStatus.OK


def _changes_list(changes: Any) -> list[tuple[str, str, list[str] | None, list[str] | None]]:
    """Edits from a `file_change`/`FileChange`/`patch_apply_end` changes value: a list of
    {path, kind}, or a mapping of path to {type, unified_diff | content} or {add|update|delete:
    {...}}."""
    out: list[tuple[str, str, list[str] | None, list[str] | None]] = []
    listed = as_list(changes)
    if isinstance(changes, list):
        for item in map(as_object, listed):
            path = as_str(item.get("path"))
            kind = _KINDS.get(as_str(item.get("kind")) or "", "unknown")
            if path:
                added: list[str] | None = [] if kind == "add" else None
                out.append((path, kind, [] if kind == "add" else None, added))
        return out
    for path, value in as_object(changes).items():
        detail = as_object(value)
        kind_name = as_str(detail.get("type"))
        if kind_name is None and len(detail) == 1:
            kind_name, detail = next(iter(detail)), as_object(next(iter(detail.values())))
        kind = _KINDS.get(kind_name or "", "unknown")
        diff = as_str(detail.get("unified_diff"))
        content = as_str(detail.get("content"))
        if diff is not None:
            removed, added = edits.unified_diff_lines(diff)
            out.append((path, kind, removed, added))
        elif kind == "add":
            out.append((path, kind, [], (content or "").splitlines()))
        elif kind == "delete":
            out.append((path, kind, (content or "").splitlines() if content else None, []))
        else:
            out.append((path, kind, None, None))
        if move := as_str(detail.get("move_path")):
            out.append((path, "delete", None, []))
            out.append((move, "add", [], None))
    return out


def _command_call(
    item: JsonObject, ts: datetime | None, cwd: str | None, output_key: str
) -> ToolCall:
    exit_code = as_int(item.get("exit_code"))
    output = as_str(item.get(output_key))
    if output is None:
        stdout = as_str(item.get("stdout")) or ""
        stderr = as_str(item.get("stderr")) or ""
        output = stdout + ("\n" if stdout and stderr else "") + stderr
    return ToolCall(
        name="shell",
        input={"command": _command_text(item.get("command"))},
        status=_status(as_str(item.get("status")), exit_code),
        exit_code=exit_code,
        output=output,
        ts=ts,
        cwd=as_str(item.get("cwd")) or cwd,
    )


def _edit_call(changes: Any, ts: datetime | None, cwd: str | None, ok: bool) -> ToolCall:
    return ToolCall(
        name="apply_patch",
        input={},
        status=CallStatus.OK if ok else CallStatus.DENIED,
        ts=ts,
        cwd=cwd,
        edits=_changes_list(changes),
    )


def _other_call(name: str, ts: datetime | None, status: CallStatus = CallStatus.OK) -> ToolCall:
    return ToolCall(name=name, input={}, status=status, ts=ts)


def load_exec_stream(path: Path, *, cwd: str | None = None) -> SessionRun:
    """Read the stdout of `codex exec --json` (one event per line)."""
    stats = LineStats()
    calls: list[ToolCall] = []
    thread: str | None = None
    final: str | None = None
    failed: list[str] = []
    seen_items: set[str] = set()
    for _, record in iter_records(path, stats):
        kind = as_str(record.get("type")) or "<none>"
        if kind not in _STREAM_TYPES:
            stats.skip(kind)
            continue
        if kind == "thread.started":
            thread = as_str(record.get("thread_id"))
        elif kind in ("turn.failed", "error"):
            error = as_object(record.get("error"))
            failed.append(as_str(error.get("message")) or as_str(record.get("message")) or kind)
        elif kind == "item.completed":
            item = as_object(record.get("item"))
            item_id = as_str(item.get("id")) or ""
            if item_id in seen_items:
                continue
            seen_items.add(item_id)
            item_type = as_str(item.get("type"))
            if item_type == "agent_message":
                final = as_str(item.get("text")) or final
            elif item_type == "command_execution":
                calls.append(_command_call(item, None, cwd, "aggregated_output"))
            elif item_type == "file_change":
                ok = as_str(item.get("status")) != "failed"
                calls.append(_edit_call(item.get("changes"), None, cwd, ok))
            elif item_type == "mcp_tool_call":
                calls.append(_other_call(f"mcp:{as_str(item.get('tool')) or '?'}", None))
            elif item_type == "web_search":
                calls.append(_other_call("web_search", None))
    events, dropped = build_events(calls, cwd)
    notes = stats.notes()
    notes.extend(
        f"skipped {n} event(s) of unknown type {k!r}"
        for k, n in sorted(stats.skipped_types.items())
    )
    notes.extend(f"the turn failed: {message}" for message in failed)
    if dropped:
        notes.append(f"{dropped} event(s) over the event cap were dropped")
    return SessionRun(
        source=SessionSource.CODEX,
        evidence=EvidenceKind.HARNESS_STREAM,
        session_id=thread,
        cwd=cwd,
        final_answer=final,
        events=events,
        complete=not stats.damaged and not dropped,
        notes=notes,
    )


def load_rollout(path: Path) -> SessionRun:
    """Read a Codex rollout file (`rollout-*.jsonl`)."""
    stats = LineStats()
    calls: list[ToolCall] = []
    session_id: str | None = None
    version: str | None = None
    cwd: str | None = None
    task: str | None = None
    final: str | None = None
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    for _, record in iter_records(path, stats):
        kind = as_str(record.get("type")) or "<none>"
        if kind not in _ROLLOUT_TYPES:
            stats.skip(kind)
            continue
        ts = parse_ts(record.get("timestamp"))
        if ts is not None:
            first_ts = first_ts or ts
            last_ts = ts
        payload = as_object(record.get("payload"))
        if kind == "session_meta":
            if payload.get("parent_thread_id") is not None or as_object(payload.get("source")).get(
                "subagent"
            ):
                continue
            session_id = (
                session_id or as_str(payload.get("id")) or as_str(payload.get("session_id"))
            )
            version = version or as_str(payload.get("cli_version"))
            cwd = cwd or as_str(payload.get("cwd"))
            continue
        if kind == "turn_context":
            cwd = cwd or as_str(payload.get("cwd"))
            continue
        if kind != "event_msg":
            continue
        event = as_str(payload.get("type"))
        if event == "exec_command_end":
            calls.append(_command_call(payload, ts, cwd, "aggregated_output"))
        elif event == "patch_apply_end":
            calls.append(
                _edit_call(payload.get("changes"), ts, cwd, payload.get("success") is not False)
            )
        elif event == "item_completed":
            item = as_object(payload.get("item"))
            item_type = as_str(item.get("type"))
            if item_type == "CommandExecution":
                calls.append(_command_call(item, ts, cwd, "aggregated_output"))
            elif item_type == "FileChange":
                calls.append(
                    _edit_call(item.get("changes"), ts, cwd, as_str(item.get("status")) != "failed")
                )
            elif item_type in ("McpToolCall", "DynamicToolCall"):
                calls.append(_other_call(f"mcp:{as_str(item.get('tool')) or '?'}", ts))
            elif item_type in ("SubAgentActivity", "CollabAgentToolCall"):
                calls.append(
                    ToolCall(
                        name="Agent",
                        input={"description": "Codex subagent"},
                        status=CallStatus.OK,
                        ts=ts,
                    )
                )
        elif event == "user_message" and task is None:
            task = as_str(payload.get("message"))
        elif event == "task_complete":
            final = as_str(payload.get("last_agent_message")) or final
        elif event == "agent_message":
            final = as_str(payload.get("message")) or final
    events, dropped = build_events(calls, cwd)
    notes = stats.notes()
    notes.extend(
        f"skipped {n} record(s) of unknown type {k!r}"
        for k, n in sorted(stats.skipped_types.items())
    )
    if dropped:
        notes.append(f"{dropped} event(s) over the event cap were dropped")
    return SessionRun(
        source=SessionSource.CODEX,
        evidence=EvidenceKind.TRANSCRIPT,
        session_id=session_id,
        harness_version=version,
        cwd=cwd,
        task=task,
        final_answer=final,
        started_at=first_ts,
        ended_at=last_ts,
        events=events,
        complete=not stats.damaged and not dropped,
        notes=notes,
    )


def sniff(path: Path) -> str:
    """'stream' or 'rollout', from the first record's `type`."""
    stats = LineStats()
    for _, record in iter_records(path, stats):
        kind = as_str(record.get("type"))
        if kind in _ROLLOUT_TYPES:
            return "rollout"
        if kind in _STREAM_TYPES:
            return "stream"
    return "stream"
