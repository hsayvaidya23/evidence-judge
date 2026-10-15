"""`evidence-judge session inspect`: what a session record holds, without judging it."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from evidence_judge.sessions.checks import SessionIndex
from evidence_judge.sessions.models import (
    CommandEvent,
    FileEditEvent,
    OtherToolEvent,
    SessionEvent,
    SessionRun,
    SubagentEvent,
)

_COMMAND_CHARS = 120


def _detail(event: SessionEvent) -> str:
    if isinstance(event, CommandEvent):
        command = " ".join(event.command.split())
        if len(command) > _COMMAND_CHARS:
            command = command[: _COMMAND_CHARS - 1] + "…"
        return command
    if isinstance(event, FileEditEvent):
        return f"{event.change} {event.path}"
    if isinstance(event, SubagentEvent):
        return event.description
    if isinstance(event, OtherToolEvent):
        return event.name
    return str(getattr(event, "path", "") or getattr(event, "pattern", ""))


def _flags(event: SessionEvent) -> list[str]:
    flags: list[str] = []
    if isinstance(event, CommandEvent):
        analysis = event.analysis
        if analysis.runners:
            flags.append("test: " + ", ".join(analysis.runners))
        if event.summary is not None:
            flags.append(event.summary.describe())
        flags.extend(f"filter {f}" for f in analysis.test_filters)
        if analysis.exit_masked_by:
            flags.append(f"exit masked: {analysis.exit_masked_by}")
        if analysis.agent_text:
            flags.append("summary text in command")
        if analysis.mutation:
            flags.append(f"may change files: {analysis.mutation}")
        if event.output_truncated:
            flags.append("output truncated")
        if analysis.session_store:
            flags.append("touches the session store")
    elif isinstance(event, FileEditEvent):
        flags.extend(event.test_findings)
        if event.by_user:
            flags.append("made by the user")
    return flags


def _status(event: SessionEvent) -> str:
    if isinstance(event, CommandEvent):
        exit_text = "?" if event.exit_code is None else str(event.exit_code)
        return f"{event.status.value} {exit_text}"
    if isinstance(event, FileEditEvent):
        return event.status.value
    return ""


def summarize(run: SessionRun) -> dict[str, Any]:
    index = SessionIndex(run)
    last = index.last_change
    return {
        "source": run.source.value,
        "evidence": run.evidence.value,
        "session_id": run.session_id,
        "harness_version": run.harness_version,
        "cwd": run.cwd,
        "complete": run.complete,
        "task": run.task,
        "final_answer": run.final_answer,
        "last_change": None if last is None else {"event": last[1].id, "why": last[2]},
        "events": [
            {
                "id": e.id,
                "tool": e.tool,
                "status": _status(e),
                "detail": _detail(e),
                "flags": _flags(e),
            }
            for e in run.events
        ],
        "notes": run.notes,
    }


def print_session(run: SessionRun, console: Console) -> None:
    """Human-readable summary. Agent-controlled strings are rendered as plain text."""
    data = summarize(run)
    head = Text.assemble(
        (f" {run.source.value} ", "bold white on blue"),
        "  ",
        (run.evidence.value, "bold"),
        "  ",
        (run.session_id or "", "dim"),
    )
    console.print(head)
    if run.cwd:
        console.print(Text.assemble(("cwd ", "dim"), run.cwd))
    if run.task:
        console.print(Text.assemble(("task ", "dim"), run.task.strip()[:300]))
    table = Table(title="Events", title_justify="left", expand=True)
    table.add_column("id", no_wrap=True, style="cyan")
    table.add_column("tool", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", ratio=3)
    table.add_column("findings", ratio=3)
    for event in data["events"]:
        table.add_row(
            Text(event["id"]),
            Text(event["tool"]),
            Text(event["status"]),
            Text(event["detail"]),
            Text("; ".join(event["flags"]), style="dim"),
        )
    console.print(table)
    if data["last_change"]:
        console.print(Text.assemble(("last change ", "bold"), data["last_change"]["why"]))
    if run.final_answer:
        console.print(Text.assemble(("final answer ", "bold"), run.final_answer.strip()[:600]))
    if not run.complete:
        console.print(Text("the record could not be read completely", style="yellow"))
    for note in run.notes:
        console.print(Text.assemble(("note ", "yellow"), note))
