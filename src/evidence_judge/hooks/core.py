"""The agent-independent part of a stop hook: find a fresh trace, judge it, decide."""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from evidence_judge.hooks.judge import (
    JudgeFailed,
    JudgeRequest,
    feedback,
    headline,
    run_judge,
    trace_label,
)
from evidence_judge.hooks.settings import HookSettings
from evidence_judge.hooks.traces import newest_trace, pack_raw_trace, search_roots
from evidence_judge.hooks.transcripts import Turn

SPEC_FILES: Final = (
    ".evidence-judge/spec.yaml",
    ".evidence-judge/spec.yml",
    "evidence-judge.yaml",
    "evidence-judge.yml",
)
NOTICE_PREFIX: Final = "evidence-judge hook: "
_NO_TRACE: Final = (
    "Playwright was used but no trace was recorded this turn, so the answer was not checked. "
    "Start Playwright MCP with --caps=devtools and call browser_start_tracing / "
    "browser_stop_tracing."
)


@dataclass(frozen=True, slots=True)
class HookContext:
    cwd: str
    stop_hook_active: bool
    answer: str | None
    turn: Turn


@dataclass(frozen=True, slots=True)
class Decision:
    """``block`` keeps the agent working with ``reason`` as its next instruction; ``notice`` is
    a one-line message for the user."""

    block: bool = False
    reason: str | None = None
    notice: str | None = None


def allow(notice: str | None = None) -> Decision:
    return Decision(notice=None if notice is None else NOTICE_PREFIX + notice)


def find_spec(root: Path, configured: Path | None) -> Path | None:
    """The configured spec, else the first conventional one; only files inside ``root``."""
    names = (configured,) if configured is not None else (Path(n) for n in SPEC_FILES)
    for name in names:
        candidate = (name if name.is_absolute() else root / name).resolve()
        if candidate.is_file() and (configured is not None or candidate.is_relative_to(root)):
            return candidate
    return None


def _project_root(cwd: str) -> Path | None:
    if not cwd:
        return None
    path = Path(cwd)
    if not path.is_absolute():
        return None
    resolved = path.resolve()
    return resolved if resolved.is_dir() else None


def evaluate(context: HookContext, settings: HookSettings) -> Decision:
    """Decide whether the agent may stop. Never raises for expected failures; callers still
    wrap it so that anything unexpected fails open."""
    if not settings.enabled or context.stop_hook_active:
        return allow()
    root = _project_root(context.cwd)
    if root is None:
        return allow()
    turn = context.turn
    since = (
        turn.started_at if turn.started_at is not None else time.time() - settings.max_trace_age_s
    )
    trace = newest_trace(search_roots(root, settings.trace_dirs), since=since)
    if trace is None:
        return allow(_NO_TRACE) if turn.used_playwright else allow()
    answer = context.answer or turn.last_text
    if not answer or not answer.strip():
        return allow("the agent's final message is empty; nothing to check")
    label = trace_label(trace.path, root)
    spec = find_spec(root, settings.spec)
    with tempfile.TemporaryDirectory(prefix="evj-hook-trace-") as tmp:
        try:
            zipped = (
                trace.path
                if trace.kind == "zip"
                else pack_raw_trace(trace.path, Path(tmp) / "trace.zip")
            )
        except (OSError, ValueError) as exc:
            return allow(f"could not read {label}: {exc}; not checked")
        result = run_judge(
            JudgeRequest(
                trace=zipped,
                answer=answer,
                task=turn.prompt,
                spec=spec,
                llm=settings.llm,
                timeout_s=settings.timeout_s,
                pass_env=tuple(settings.pass_env),
            )
        )
    if isinstance(result, JudgeFailed):
        return allow(f"{result.reason}; not blocking")
    blocking = result.outcome == "fail" or (
        result.outcome == "needs_review" and settings.block_on_needs_review
    )
    if blocking:
        return Decision(block=True, reason=feedback(result.outcome, result.verdict, label))
    return Decision(notice=headline(result.outcome, result.verdict, label))
