"""Readers for coding-agent session records."""

from __future__ import annotations

from pathlib import Path

from evidence_judge.sessions.ingest.claude_code import load_transcript, locate
from evidence_judge.sessions.ingest.codex import load_exec_stream, load_rollout, sniff
from evidence_judge.sessions.ingest.evidence_log import load_evidence_log
from evidence_judge.sessions.models import SessionRun

__all__ = [
    "load_codex",
    "load_evidence_log",
    "load_exec_stream",
    "load_rollout",
    "load_session",
    "load_transcript",
    "locate",
]


def load_codex(path: Path) -> SessionRun:
    """A `codex exec --json` stream or a rollout file, told apart by their first record."""
    return load_rollout(path) if sniff(path) == "rollout" else load_exec_stream(path)


def load_session(
    *,
    claude_code: Path | None = None,
    codex: Path | None = None,
    evidence_log: Path | None = None,
    answer: str | None = None,
) -> SessionRun:
    """Read one session. With an evidence log and a transcript, tool calls come from the log
    and only the task and final answer from the transcript when the log lacks them. `answer`
    replaces the recorded final answer."""
    if evidence_log is not None:
        run = load_evidence_log(evidence_log)
        transcript = (
            load_transcript(claude_code) if claude_code else load_codex(codex) if codex else None
        )
        if transcript is not None:
            run = run.model_copy(
                update={
                    "task": run.task or transcript.task,
                    "final_answer": run.final_answer or transcript.final_answer,
                    "harness_version": run.harness_version or transcript.harness_version,
                    "git_branch": transcript.git_branch,
                    "session_id": run.session_id or transcript.session_id,
                }
            )
    elif claude_code is not None:
        run = load_transcript(claude_code)
    elif codex is not None:
        run = load_codex(codex)
    else:
        raise ValueError("no session given")
    if answer is not None:
        run = run.model_copy(update={"final_answer": answer})
    return run
