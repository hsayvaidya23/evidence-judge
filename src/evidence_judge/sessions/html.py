"""The HTML report for a session verdict: the browser report's page, with the session's tool
calls as its timeline."""

from __future__ import annotations

from pathlib import Path

from evidence_judge.core.verdict import RunVerdict
from evidence_judge.report.html_report import (
    MAX_TIMELINE_ROWS,
    EvidenceView,
    TimelineRow,
    anchor_for,
    cited_ids,
    render_report,
)
from evidence_judge.sessions.models import (
    CallStatus,
    CommandEvent,
    FileEditEvent,
    SessionEvent,
    SessionRun,
)
from evidence_judge.sessions.report import describe_event

_LABEL_CHARS = 160


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _LABEL_CHARS else text[: _LABEL_CHARS - 1] + "…"


def _command_status(event: CommandEvent) -> tuple[str, str]:
    if event.status is CallStatus.OK and event.exit_code == 0:
        return "exit 0", "ok"
    if event.exit_code is not None:
        return f"exit {event.exit_code}", "bad"
    tone = "bad" if event.status is CallStatus.ERROR else "muted"
    return event.status.value.replace("_", " "), tone


def _status(event: SessionEvent) -> tuple[str, str]:
    if isinstance(event, CommandEvent):
        return _command_status(event)
    if isinstance(event, FileEditEvent):
        if event.test_findings:
            return event.status.value, "bad"
        return event.status.value, "ok" if event.status is CallStatus.OK else "muted"
    return event.tool, "muted"


def _row_detail(event: SessionEvent) -> str | None:
    if isinstance(event, CommandEvent):
        return event.summary.describe() if event.summary is not None else None
    if isinstance(event, FileEditEvent):
        return "; ".join(event.test_findings) or None
    return None


def _mutation(event: SessionEvent) -> bool:
    if isinstance(event, CommandEvent):
        return event.analysis.mutation is not None
    return isinstance(event, FileEditEvent)


def _row(event: SessionEvent, position: int, total: int, cited: set[str]) -> TimelineRow:
    status, tone = _status(event)
    kind = event.type if isinstance(event, CommandEvent | FileEditEvent) else "tool"
    return TimelineRow(
        anchor=anchor_for(event.id),
        evidence_id=event.id,
        time=f"#{event.seq}",
        kind=kind,
        label=_short(describe_event(event)),
        detail=_row_detail(event),
        status=status,
        tone=tone,
        cited=event.id in cited,
        mutation=_mutation(event),
        offset=round(position / max(total - 1, 1) * 100, 2),
    )


def _describe(run: SessionRun) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for event in run.events:
        status, tone = _status(event)
        out[event.id] = (f"{_short(describe_event(event))[:60]} · {status}", tone)
    return out


def session_evidence(verdict: RunVerdict, run: SessionRun) -> EvidenceView:
    cited = cited_ids(verdict)
    total = len(run.events)
    rows = [_row(e, i, total, cited) for i, e in enumerate(run.events[:MAX_TIMELINE_ROWS])]
    return EvidenceView(
        source=f"{run.source.value} · {run.evidence.value}",
        channels=sorted({row.kind for row in rows}),
        timeline=rows,
        timeline_hidden=max(0, total - MAX_TIMELINE_ROWS),
        described=_describe(run),
        timeline_note="tool calls in execution order",
        timeline_empty="The session recorded no tool calls.",
    )


def render_session_html(verdict: RunVerdict, run: SessionRun) -> str:
    """Render a self-contained HTML report of a session verdict. All agent-controlled text,
    commands and their output included, is HTML-escaped."""
    return render_report(verdict, session_evidence(verdict, run))


def write_session_html(verdict: RunVerdict, run: SessionRun, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_session_html(verdict, run), encoding="utf-8")
    return path
