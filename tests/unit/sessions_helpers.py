"""Builders for synthetic coding-agent sessions used by the session tests."""

from __future__ import annotations

from typing import Any

from evidence_judge.sessions.models import CallStatus, EvidenceKind, SessionRun, SessionSource
from evidence_judge.sessions.normalize import ToolCall, build_events

CWD = "/work/toy"
GREEN = "...\n3 passed in 0.01s"
RED = "FF.\n== 2 failed, 1 passed in 0.02s =="


def bash(
    command: str,
    output: str = "",
    exit_code: int | None = 0,
    *,
    status: CallStatus | None = None,
    **extra: Any,
) -> ToolCall:
    if status is None:
        status = CallStatus.OK if exit_code in (0, None) else CallStatus.ERROR
    return ToolCall(
        name="Bash",
        input={"command": command},
        status=status,
        exit_code=exit_code,
        output=output,
        **extra,
    )


def edit(path: str, old: str = "return a - b", new: str = "return a + b") -> ToolCall:
    return ToolCall(
        name="Edit",
        input={"file_path": f"{CWD}/{path}", "old_string": old, "new_string": new},
        status=CallStatus.OK,
    )


def write(path: str, content: str, kind: str = "create") -> ToolCall:
    return ToolCall(
        name="Write",
        input={"file_path": f"{CWD}/{path}", "content": content},
        status=CallStatus.OK,
        result={"type": kind},
    )


def tool(name: str, **tool_input: Any) -> ToolCall:
    return ToolCall(name=name, input=tool_input, status=CallStatus.OK)


def session(
    *calls: ToolCall,
    answer: str | None = None,
    complete: bool = True,
    evidence: EvidenceKind = EvidenceKind.TRANSCRIPT,
) -> SessionRun:
    events, _ = build_events(calls, CWD)
    return SessionRun(
        source=SessionSource.CLAUDE_CODE,
        evidence=evidence,
        session_id="synthetic",
        cwd=CWD,
        final_answer=answer,
        events=events,
        complete=complete,
    )
