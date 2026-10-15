"""Evidence-recorder logs: one JSON line per tool call, written by a harness hook.

Format version 1 is specified in docs/design/coding-agent-sessions.md ("Log format, version
1"). The same format serves Claude Code, Codex and Gemini recorders; tool names stay each
agent's own. A line that does not parse, a record without a required key, or a broken
`seq`/`prev` hash chain marks the run incomplete.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from evidence_judge.ingest._json import JsonObject, as_int, as_object, as_str
from evidence_judge.sessions.ingest._lines import LineStats, iter_records, parse_ts
from evidence_judge.sessions.models import CallStatus, EvidenceKind, SessionRun, SessionSource
from evidence_judge.sessions.normalize import ToolCall, build_events

FORMAT_VERSION: Final = 1
REQUIRED: Final = (
    "v",
    "agent",
    "ts",
    "session_id",
    "tool",
    "input",
    "exit_code",
    "stdout_sha256",
    "stdout_excerpt",
    "stderr_excerpt",
    "cwd",
)
_EVENT_REQUIRED: Final = ("v", "agent", "ts", "session_id")
_AGENTS: Final = {
    "claude-code": SessionSource.CLAUDE_CODE,
    "codex": SessionSource.CODEX,
    "gemini": SessionSource.GEMINI,
}
_STATUS: Final = {
    "ok": CallStatus.OK,
    "error": CallStatus.ERROR,
    "denied": CallStatus.DENIED,
    "interrupted": CallStatus.INTERRUPTED,
}
_GEMINI_EXIT: Final = re.compile(r"^Exit Code: (-?\d+)\s*$", re.MULTILINE)


class _Chain:
    def __init__(self) -> None:
        self.previous: bytes | None = None
        self.used = False
        self.broken: str | None = None
        self.count = 0

    def check(self, raw: bytes, record: JsonObject) -> None:
        seq = record.get("seq")
        prev = record.get("prev", ...)
        if seq is not None or prev is not ...:
            self.used = True
        if self.used and self.broken is None:
            expected = None if self.previous is None else hashlib.sha256(self.previous).hexdigest()
            if as_int(seq) != self.count:
                self.broken = f"line {self.count + 1} has seq {seq!r}, expected {self.count}"
            elif prev != expected:
                self.broken = f"line {self.count + 1} does not chain to the line before it"
        self.previous = raw
        self.count += 1


def _complete_excerpt(excerpt: str, digest: str | None, flag: Any) -> bool:
    if flag is True:
        return False
    if flag is False:
        return True
    if digest is None:
        return False
    return hashlib.sha256(excerpt.encode("utf-8")).hexdigest() == digest.lower()


def _call(record: JsonObject, agent: str) -> ToolCall:
    tool = as_str(record.get("tool")) or "?"
    stdout = as_str(record.get("stdout_excerpt")) or ""
    stderr = as_str(record.get("stderr_excerpt")) or ""
    output = stdout + ("\n" if stdout and stderr else "") + stderr
    exit_code = as_int(record.get("exit_code"))
    if exit_code is None and agent == "gemini" and tool == "run_shell_command":
        found = _GEMINI_EXIT.findall(stdout)
        exit_code = int(found[-1]) if found else None
    status = _STATUS.get(as_str(record.get("status")) or "")
    if status is None:
        status = CallStatus.OK if exit_code in (None, 0) else CallStatus.ERROR
    complete = _complete_excerpt(
        stdout, as_str(record.get("stdout_sha256")), record.get("output_truncated")
    )
    result = as_object(record.get("result"))
    return ToolCall(
        name=tool,
        input=as_object(record.get("input")),
        status=status,
        exit_code=exit_code,
        output=output,
        output_truncated=not complete,
        background=record.get("background") is True,
        result=result,
        ts=parse_ts(record.get("ts")),
        cwd=as_str(record.get("cwd")),
        agent_id=as_str(record.get("agent_id")),
        by_user=result.get("userModified") is True,
    )


def load_evidence_log(path: Path) -> SessionRun:
    stats = LineStats()
    chain = _Chain()
    calls: list[ToolCall] = []
    problems: list[str] = []
    agents: set[str] = set()
    session_ids: set[str] = set()
    cwd: str | None = None
    task: str | None = None
    final: str | None = None
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    for raw, record in iter_records(path, stats):
        chain.check(raw, record)
        line = chain.count
        if record.get("v") != FORMAT_VERSION:
            problems.append(f"line {line}: unsupported version {record.get('v')!r}")
            continue
        event = as_str(record.get("event")) or "tool"
        required = REQUIRED if event == "tool" else _EVENT_REQUIRED
        missing = [key for key in required if key not in record]
        if missing:
            problems.append(f"line {line}: missing {', '.join(missing)}")
            continue
        agent = as_str(record.get("agent")) or ""
        if agent not in _AGENTS:
            problems.append(f"line {line}: unknown agent {agent!r}")
            continue
        agents.add(agent)
        if sid := as_str(record.get("session_id")):
            session_ids.add(sid)
        ts = parse_ts(record.get("ts"))
        if ts is not None:
            first_ts = first_ts or ts
            last_ts = ts
        if event == "prompt":
            task = task or as_str(record.get("text"))
        elif event == "stop":
            if record.get("agent_id") is None:
                final = as_str(record.get("last_assistant_message")) or final
        elif event == "tool":
            cwd = cwd or as_str(record.get("cwd"))
            calls.append(_call(record, agent))
        else:
            stats.skip(event)
    notes = stats.notes() + problems
    if chain.broken:
        notes.append(f"hash chain broken: {chain.broken}")
    if len(agents) > 1:
        problems.append(f"the log mixes agents: {', '.join(sorted(agents))}")
        notes.append(problems[-1])
    if len(session_ids) > 1:
        notes.append(f"the log holds {len(session_ids)} sessions")
    if not chain.used:
        notes.append("the log has no seq/prev hash chain; edits after the fact cannot be detected")
    events, dropped = build_events(calls, cwd)
    if dropped:
        notes.append(f"{dropped} event(s) over the event cap were dropped")
    agent = next(iter(agents)) if len(agents) == 1 else "claude-code"
    return SessionRun(
        source=_AGENTS[agent],
        evidence=EvidenceKind.EVIDENCE_LOG,
        session_id=next(iter(session_ids)) if len(session_ids) == 1 else None,
        cwd=cwd,
        task=task,
        final_answer=final,
        started_at=first_ts,
        ended_at=last_ts,
        events=events,
        complete=not (stats.damaged or problems or chain.broken or dropped or stats.not_objects)
        and len(session_ids) <= 1,
        notes=notes,
    )
