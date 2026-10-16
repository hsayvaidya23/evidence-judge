"""Evidence-recorder logs: one JSON line per tool call, written by a harness hook.

Format version 1 is specified in docs/design/coding-agent-sessions.md ("Log format, version
1"). The same format serves Claude Code, Codex and Gemini recorders; tool names stay each
agent's own. A line that does not parse, a record without a required key, a broken
`seq`/`prev` chain or a log that does not end where its head anchor says marks the run
incomplete.

What the log can show of tampering is its `integrity`: `full` only for an HMAC-chained log
whose MACs verify under the installation's key and whose line count and last MAC match the
head anchor the recorder keeps apart from it. Anything less is `partial`, with the reason. Even
`full` only means the log is as the recorder wrote it *if* the agent could not read the key:
the session judge accepts it as tamper evidence only when the user says so (`--trusted-log`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from evidence_judge.core.verdict import TamperDetection, TamperLevel
from evidence_judge.hooks.record import gemini_exit_code
from evidence_judge.hooks.state import (
    LOG_CHAIN,
    LOG_KEY_FILE,
    key_id,
    line_mac,
    log_head_path,
    read_log_head,
    read_log_key,
    resolve_state_dir,
)
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


class _Chain:
    def __init__(self, key: bytes | None) -> None:
        self.key = key
        self.previous: bytes | None = None
        self.used = False
        self.keyed = 0
        self.plain = 0
        self.key_ids: set[object] = set()
        self.broken: str | None = None
        self.count = 0

    def _expected(self, keyed: bool) -> tuple[bool, str | None]:
        """Whether the ``prev`` of the next line can be checked, and its expected value."""
        if self.previous is None:
            return True, None
        if not keyed:
            return True, hashlib.sha256(self.previous).hexdigest()
        if self.key is None:
            return False, None
        return True, line_mac(self.key, self.previous)

    def check(self, raw: bytes, record: JsonObject) -> None:
        seq = record.get("seq")
        prev = record.get("prev", ...)
        if seq is not None or prev is not ...:
            self.used = True
        keyed = record.get("chain") == LOG_CHAIN
        if keyed:
            self.keyed += 1
            self.key_ids.add(record.get("key_id"))
        elif self.used:
            self.plain += 1
        if self.used and self.broken is None:
            checkable, expected = self._expected(keyed)
            if as_int(seq) != self.count:
                self.broken = f"line {self.count + 1} has seq {seq!r}, expected {self.count}"
            elif checkable and prev != expected:
                self.broken = f"line {self.count + 1} does not chain to the line before it"
        self.previous = raw
        self.count += 1


@dataclass(frozen=True, slots=True)
class _Anchor:
    count: int
    head: str


def _anchor_problem(chain: _Chain, anchor: _Anchor | None) -> str | None:
    if anchor is None or chain.key is None or chain.previous is None:
        return None
    if anchor.count != chain.count or anchor.head != line_mac(chain.key, chain.previous):
        return (
            f"the log does not end where its head anchor says ({chain.count} lines, the anchor "
            f"records {anchor.count}): lines were cut off or rewritten"
        )
    return None


def _integrity(chain: _Chain, anchor: _Anchor | None, complete: bool) -> TamperDetection:
    gaps: list[str] = []
    if not chain.used:
        gaps.append("the log is not chained")
    elif not chain.keyed:
        gaps.append(
            "the log is chained with plain SHA-256, which anyone who can write it can recompute"
        )
    elif chain.plain:
        gaps.append(f"{chain.plain} line(s) are not HMAC-chained")
    if chain.keyed:
        if chain.key is None:
            gaps.append("the log key was not found, so the HMAC chain cannot be checked")
        elif chain.key_ids != {key_id(chain.key)}:
            gaps.append("the log was signed with a different key")
        if anchor is None:
            gaps.append("no head anchor was found, so lines cut from the end would not show")
    if not complete:
        gaps.append("the log could not be read completely or does not verify")
    if gaps:
        return TamperDetection(level=TamperLevel.PARTIAL, reason="; ".join(gaps))
    return TamperDetection(
        level=TamperLevel.FULL,
        reason="an evidence log whose HMAC chain and head anchor verify under the "
        "installation's log key",
    )


def _load_anchor(path: Path | None) -> _Anchor | None:
    found = read_log_head(path) if path is not None else None
    return _Anchor(*found) if found is not None else None


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
        exit_code = gemini_exit_code(stdout)
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


def _validate_record(record: JsonObject, line: int) -> str | None:
    """Why a record cannot be read as format version 1, or None when it can."""
    if record.get("v") != FORMAT_VERSION:
        return f"line {line}: unsupported version {record.get('v')!r}"
    event = as_str(record.get("event")) or "tool"
    required = REQUIRED if event == "tool" else _EVENT_REQUIRED
    if missing := [name for name in required if name not in record]:
        return f"line {line}: missing {', '.join(missing)}"
    agent = as_str(record.get("agent")) or ""
    if agent not in _AGENTS:
        return f"line {line}: unknown agent {agent!r}"
    return None


def load_evidence_log(
    path: Path, *, key_file: Path | None = None, anchor_file: Path | None = None
) -> SessionRun:
    """Load an evidence log. The key defaults to the hooks' state directory
    (``EVJ_HOOK_STATE_DIR``, else ``~/.evidence-judge/state``); the head anchor to the log's
    session in the key's directory."""
    state = resolve_state_dir(None) if key_file is None else key_file.parent
    key = read_log_key(key_file if key_file is not None else state / LOG_KEY_FILE)
    stats = LineStats()
    chain = _Chain(key)
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
        if problem := _validate_record(record, chain.count):
            problems.append(problem)
            continue
        event = as_str(record.get("event")) or "tool"
        agent = as_str(record.get("agent")) or ""
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
            continue
        stats.recognised += 1
    stats.require_records(path, "evidence-recorder", problems[0] if problems else None)
    session_id = next(iter(session_ids)) if len(session_ids) == 1 else None
    if anchor_file is None and session_id is not None:
        anchor_file = log_head_path(state, session_id)
    anchor = _load_anchor(anchor_file)
    if cut := _anchor_problem(chain, anchor):
        problems.append(cut)
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
    complete = (
        not (stats.damaged or problems or chain.broken or dropped or stats.not_objects)
        and len(session_ids) <= 1
    )
    integrity = _integrity(chain, anchor, complete)
    return SessionRun(
        source=_AGENTS[agent],
        evidence=EvidenceKind.EVIDENCE_LOG,
        session_id=session_id,
        cwd=cwd,
        task=task,
        final_answer=final,
        started_at=first_ts,
        ended_at=last_ts,
        events=events,
        complete=complete,
        notes=notes,
        integrity=integrity,
    )
