"""The agent-independent part of a stop hook: find the turn's traces, judge them, decide."""

from __future__ import annotations

import tempfile
import time
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from evidence_judge.hooks.judge import (
    Judged,
    JudgeFailed,
    JudgeRequest,
    feedback,
    headline,
    run_judge,
    trace_label,
)
from evidence_judge.hooks.settings import HookSettings
from evidence_judge.hooks.traces import (
    TraceFile,
    fresh_traces,
    kind_of,
    pack_raw_trace,
    recorded_until,
    search_roots,
)
from evidence_judge.hooks.transcripts import Turn

SPEC_FILES: Final = (
    ".evidence-judge/spec.yaml",
    ".evidence-judge/spec.yml",
    "evidence-judge.yaml",
    "evidence-judge.yml",
)
NOTICE_PREFIX: Final = "evidence-judge hook: "
_AGAIN: Final = (
    "WARNING: the agent stopped again after being sent back, and its answer is still not backed "
    "by the evidence. Do not rely on it."
)
MAX_TRACES: Final = 10
_CLOCK_SLACK_S: Final = 2.0
_NO_TRACE: Final = "Playwright was used but no trace was recorded this turn"
_TRACE_HOWTO: Final = (
    "Record the browser work with Playwright MCP started with --caps=devtools: call "
    "browser_start_tracing before it and browser_stop_tracing after it, leave the trace files in "
    "place, then answer again. If you cannot, say plainly that the answer is unverified."
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


def _named_traces(root: Path, turn: Turn) -> tuple[list[TraceFile], list[str]]:
    traces: list[TraceFile] = []
    problems: list[str] = []
    for written in dict.fromkeys(turn.trace_paths):
        path = Path(written)
        path = (path if path.is_absolute() else root / path).resolve()
        kind = kind_of(path.name)
        if kind is None or not path.is_file():
            problems.append(f"{written}: browser_stop_tracing reported it, but it is missing")
            continue
        traces.append(TraceFile(path, path.stat().st_mtime, kind))
    return traces, problems


def _turn_traces(
    root: Path, settings: HookSettings, turn: Turn, since: float
) -> tuple[list[TraceFile], list[str], list[str]]:
    """The traces to judge, the problems that make the evidence unusable, and notes. Traces
    ``browser_stop_tracing`` reported in the transcript are the only ones that count when there
    are any; otherwise every trace modified this turn counts. A trace whose own events end
    before the turn started is a problem, whatever its mtime."""
    if turn.trace_paths:
        candidates, problems = _named_traces(root, turn)
    else:
        candidates, problems = (
            fresh_traces(search_roots(root, settings.trace_dirs), since=since),
            [],
        )
    notes: list[str] = []
    if len(candidates) > MAX_TRACES:
        notes.append(f"{len(candidates)} traces this turn; judged the newest {MAX_TRACES}")
        candidates = candidates[:MAX_TRACES]
    kept: list[TraceFile] = []
    for trace in candidates:
        label = trace_label(trace.path, root)
        try:
            until = recorded_until(trace)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            problems.append(f"{label}: unreadable ({type(exc).__name__})")
            continue
        if until is not None and until < since - _CLOCK_SLACK_S:
            problems.append(f"{label}: its events were recorded before this turn started")
            continue
        kept.append(trace)
    return kept, problems, notes


def _judge_one(trace: TraceFile, request: JudgeRequest, packed: Path) -> Judged | JudgeFailed:
    try:
        zipped = trace.path if trace.kind == "zip" else pack_raw_trace(trace.path, packed)
    except (OSError, ValueError) as exc:
        return JudgeFailed(f"could not read it: {exc}", invalid=True)
    return run_judge(replace(request, trace=zipped))


def _bad_evidence(
    context: HookContext, settings: HookSettings, problems: list[str], instructions: str
) -> Decision:
    listed = "\n".join(f"- {p}" for p in problems)
    if settings.on_bad_evidence == "block" and context.turn.used_playwright:
        return _block(
            context,
            "Evidence Judge: the browser evidence for this turn is missing or unusable, so the "
            f"answer cannot be checked.\n{listed}\n{instructions}",
        )
    return allow(f"the answer was not checked: {'; '.join(problems)}")


def evaluate(context: HookContext, settings: HookSettings) -> Decision:
    """Decide whether the agent may stop. Never raises for expected failures; callers still
    wrap it so that anything unexpected fails open."""
    if not settings.enabled:
        return allow()
    root = _project_root(context.cwd)
    if root is None:
        return allow()
    turn = context.turn
    since = (
        turn.started_at if turn.started_at is not None else time.time() - settings.max_trace_age_s
    )
    traces, problems, notes = _turn_traces(root, settings, turn, since)
    if not traces and not problems:
        if turn.used_playwright:
            return _bad_evidence(context, settings, [_NO_TRACE], _TRACE_HOWTO)
        return allow()
    answer = context.answer or turn.last_text
    if not answer or not answer.strip():
        return allow("the agent's final message is empty; nothing to check")
    request = JudgeRequest(
        trace=root,
        answer=answer,
        task=turn.prompt,
        spec=find_spec(root, settings.spec),
        llm=settings.llm,
        timeout_s=settings.timeout_s,
        pass_env=tuple(settings.pass_env),
    )
    deadline = time.monotonic() + settings.timeout_s
    failures: list[str] = []
    passes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="evj-hook-trace-") as tmp:
        for index, trace in enumerate(traces):
            label = trace_label(trace.path, root)
            left = deadline - time.monotonic()
            if index and left < 1:
                notes.append(f"ran out of time before judging {label}; not blocking")
                break
            result = _judge_one(
                trace, replace(request, timeout_s=max(left, 0.001)), Path(tmp) / f"{index}.zip"
            )
            if isinstance(result, JudgeFailed):
                if result.invalid:
                    problems.append(f"{label}: {result.reason}")
                else:
                    notes.append(f"{label}: {result.reason}; not blocking")
                continue
            blocking = result.outcome == "fail" or (
                result.outcome == "needs_review" and settings.block_on_needs_review
            )
            if blocking:
                failures.append(feedback(result.outcome, result.verdict, label))
            else:
                passes.append(headline(result.outcome, result.verdict, label))
    if failures:
        return _block(context, "\n\n".join(failures))
    if problems:
        return _bad_evidence(context, settings, problems, _TRACE_HOWTO)
    lines = passes + [NOTICE_PREFIX + note for note in notes]
    return Decision(notice="\n".join(lines) if lines else None)


def _block(context: HookContext, reason: str) -> Decision:
    """Keep the agent working, unless it is already continuing because of a stop hook: blocking
    again could loop, so the stop goes through and the user is told plainly why."""
    if not context.stop_hook_active:
        return Decision(block=True, reason=reason)
    return allow(f"{_AGAIN} {reason}")
