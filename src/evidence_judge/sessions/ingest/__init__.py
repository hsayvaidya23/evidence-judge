"""Readers for coding-agent session records."""

from __future__ import annotations

from itertools import islice
from pathlib import Path
from typing import Final

from evidence_judge.ingest._json import as_str
from evidence_judge.sessions.ingest._lines import LineStats, UnrecognisedSessionError, iter_records
from evidence_judge.sessions.ingest.claude_code import IGNORED_TYPES, load_transcript, locate
from evidence_judge.sessions.ingest.codex import (
    ROLLOUT_TYPES,
    STREAM_TYPES,
    load_exec_stream,
    load_rollout,
    sniff,
)
from evidence_judge.sessions.ingest.evidence_log import load_evidence_log
from evidence_judge.sessions.models import SessionRun

__all__ = [
    "UnrecognisedSessionError",
    "load_codex",
    "load_evidence_log",
    "load_exec_stream",
    "load_rollout",
    "load_session",
    "load_transcript",
    "locate",
]


_SNIFF_RECORDS: Final = 100


def looks_like(path: Path) -> str:
    """What a session file looks like, from its first records, for an input error."""
    stats = LineStats()
    for _, record in islice(iter_records(path, stats), _SNIFF_RECORDS):
        kind = as_str(record.get("type"))
        if "type" not in record and {"v", "agent", "event"} <= record.keys():
            return "it looks like an evidence-recorder log (--evidence-log)"
        if kind in ROLLOUT_TYPES or kind in STREAM_TYPES:
            return "it looks like a Codex session (--codex)"
        if kind in ("user", "assistant") or kind in IGNORED_TYPES:
            return "it looks like a Claude Code transcript (--claude-code)"
    if not stats.lines:
        return "the file is empty"
    return "it is not a session format evidence-judge reads"


def load_codex(path: Path) -> SessionRun:
    """A `codex exec --json` stream or a rollout file, told apart by their first record."""
    return load_rollout(path) if sniff(path) == "rollout" else load_exec_stream(path)


def load_session(
    *,
    claude_code: Path | None = None,
    codex: Path | None = None,
    evidence_log: Path | None = None,
    answer: str | None = None,
    log_key: Path | None = None,
    log_anchor: Path | None = None,
) -> SessionRun:
    """Read one session. With an evidence log and a transcript, tool calls come from the log
    and only the task and final answer from the transcript when the log lacks them. `answer`
    replaces the recorded final answer. A file with no record of the format it was given as
    raises UnrecognisedSessionError, saying what the file looks like."""
    try:
        run = _read(claude_code, codex, evidence_log, log_key, log_anchor)
    except UnrecognisedSessionError as e:
        raise UnrecognisedSessionError(e.path, e.what, e.detail, looks_like(e.path)) from None
    if answer is not None:
        run = run.model_copy(update={"final_answer": answer})
    return run


def _read(
    claude_code: Path | None,
    codex: Path | None,
    evidence_log: Path | None,
    log_key: Path | None,
    log_anchor: Path | None,
) -> SessionRun:
    if evidence_log is None:
        if claude_code is not None:
            return load_transcript(claude_code)
        if codex is not None:
            return load_codex(codex)
        raise ValueError("no session given")
    run = load_evidence_log(evidence_log, key_file=log_key, anchor_file=log_anchor)
    transcript = (
        load_transcript(claude_code) if claude_code else load_codex(codex) if codex else None
    )
    if transcript is None:
        return run
    return run.model_copy(
        update={
            "task": run.task or transcript.task,
            "final_answer": run.final_answer or transcript.final_answer,
            "harness_version": run.harness_version or transcript.harness_version,
            "git_branch": transcript.git_branch,
            "session_id": run.session_id or transcript.session_id,
        }
    )
