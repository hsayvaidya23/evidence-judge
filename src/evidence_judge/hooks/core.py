"""The agent-independent part of a stop hook: find the turn's traces, judge them, decide."""

from __future__ import annotations

import tempfile
import time
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import yaml

from evidence_judge.hooks.judge import (
    Judged,
    JudgeFailed,
    JudgeRequest,
    SessionRequest,
    feedback,
    headline,
    run_judge,
    run_session_judge,
    session_feedback,
    trace_label,
)
from evidence_judge.hooks.record import log_path
from evidence_judge.hooks.settings import HookSettings
from evidence_judge.hooks.state import (
    BROWSER_SPEC,
    LOG_KEY_FILE,
    SESSION_SPEC,
    digest,
    environment_path,
    load_snapshot,
    log_head_path,
    read_spec,
)
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
SESSION_SPEC_FILES: Final = (".evidence-judge/session.yaml", ".evidence-judge/session.yml")
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
    session_id: str = ""
    transcript: Path | None = None
    """The agent's own session record, when the harness names one."""
    transcript_kind: str | None = None
    """``claude-code`` or ``codex``: how ``session judge`` reads ``transcript``."""


@dataclass(frozen=True, slots=True)
class Decision:
    """``block`` keeps the agent working with ``reason`` as its next instruction; ``notice`` is
    a one-line message for the user."""

    block: bool = False
    reason: str | None = None
    notice: str | None = None


def allow(notice: str | None = None) -> Decision:
    return Decision(notice=None if notice is None else NOTICE_PREFIX + notice)


def find_spec(
    root: Path, configured: Path | None, conventional: tuple[str, ...] = SPEC_FILES
) -> Path | None:
    """The configured spec, else the first conventional one; only files inside ``root``."""
    names = (configured,) if configured is not None else (Path(n) for n in conventional)
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


def _turn_spec(
    root: Path,
    settings: HookSettings,
    state: Path | None,
    session_id: str,
    kind: str = BROWSER_SPEC,
) -> tuple[Path | None, str | None]:
    """The spec to judge with, and what changed about it during the turn. With a snapshot from
    the turn-start hook, the spec as the turn started is used, whatever the agent did to it."""
    live = (
        find_spec(root, settings.spec)
        if kind == BROWSER_SPEC
        else find_spec(root, settings.session_spec, SESSION_SPEC_FILES)
    )
    snapshot = load_snapshot(state, session_id, kind) if state is not None and session_id else None
    if snapshot is None:
        return live, None
    if digest(read_spec(live)) == snapshot.sha256:
        return snapshot.copy, None
    before = snapshot.source or "no spec"
    now = str(live) if live is not None else "no spec"
    return snapshot.copy, (
        f"the task spec changed during the turn ({before} when it started, {now} now); the "
        "answer was judged with the spec as the turn started"
    )


def _with_spec_change(
    context: HookContext, settings: HookSettings, decision: Decision, change: str | None
) -> Decision:
    if change is None:
        return decision
    if not settings.block_on_spec_change:
        if decision.block:
            return replace(decision, reason=f"Evidence Judge: {change}.\n\n{decision.reason}")
        notice = NOTICE_PREFIX + change
        return Decision(
            notice=notice if decision.notice is None else f"{notice}\n{decision.notice}"
        )
    blocked = (
        f"Evidence Judge: {change}. Restore the spec; the agent may not change the "
        "criteria its own answer is checked against. If the user asked for this change, say so "
        "plainly in your answer."
    )
    if decision.block and decision.reason:
        return _block(context, f"{blocked}\n\n{decision.reason}")
    rest = decision.notice or ""
    return _block(context, f"{blocked}\n{rest}".rstrip())


def evaluate(
    context: HookContext,
    settings: HookSettings,
    state: Path | None = None,
    log_dir: Path | None = None,
) -> Decision:
    """Decide whether the agent may stop. Never raises for expected failures; callers still
    wrap it so that anything unexpected fails open. ``state`` is the hooks' private state
    directory, where the turn-start hook saved the specs; ``log_dir`` holds the evidence logs
    when the hooks record them."""
    if not settings.enabled:
        return allow()
    root = _project_root(context.cwd)
    if root is None:
        return allow()
    spec, change = _turn_spec(root, settings, state, context.session_id)
    decision = _judge_turn(context, settings, root, spec)
    decision = _with_spec_change(context, settings, decision, change)
    if decision.block:
        return decision
    session_spec, session_change = _turn_spec(
        root, settings, state, context.session_id, SESSION_SPEC
    )
    rerun = _rerun_turn(context, settings, root, session_spec, state, log_dir)
    rerun = _with_spec_change(context, settings, rerun, session_change)
    if rerun.block:
        return rerun
    notices = [n for n in (decision.notice, rerun.notice) if n]
    return Decision(notice="\n".join(notices) if notices else None)


def has_rerun(spec: Path) -> bool:
    try:
        data: object = yaml.safe_load(read_spec(spec) or b"")
    except yaml.YAMLError:
        return True
    return isinstance(data, dict) and "rerun" in data


def _session_record(
    context: HookContext, state: Path | None, log_dir: Path | None
) -> tuple[str, ...]:
    """``session judge`` options naming what this session left behind: the evidence log with
    its key and anchor, and the agent's own transcript."""
    record: list[str] = []
    if log_dir is not None and context.session_id:
        log = log_path(log_dir, context.session_id)
        if log.is_file():
            record += ["--evidence-log", str(log)]
            if state is not None:
                key = state / LOG_KEY_FILE
                anchor = log_head_path(state, context.session_id)
                record += ["--log-key", str(key)] if key.is_file() else []
                record += ["--log-anchor", str(anchor)] if anchor.is_file() else []
    if context.transcript is not None and context.transcript_kind in ("claude-code", "codex"):
        record += [f"--{context.transcript_kind}", str(context.transcript)]
    return tuple(record)


def _rerun_turn(
    context: HookContext,
    settings: HookSettings,
    root: Path,
    spec: Path | None,
    state: Path | None,
    log_dir: Path | None,
) -> Decision:
    """Re-run the tests the session spec names and judge the session against the result: the
    stop is blocked when the suite, run by the judge itself, contradicts the answer."""
    if spec is None or not has_rerun(spec):
        return Decision()
    snapshot = (
        load_snapshot(state, context.session_id, SESSION_SPEC)
        if state is not None and context.session_id
        else None
    )
    request = SessionRequest(
        root=root,
        spec=spec,
        answer=context.answer or context.turn.last_text or "",
        record=_session_record(context, state, log_dir),
        base=snapshot.git_head if snapshot is not None else None,
        baseline=(
            environment_path(state, context.session_id)
            if state is not None and snapshot is not None and snapshot.source is not None
            else None
        ),
        llm=settings.llm,
        timeout_s=settings.timeout_s,
        pass_env=tuple(settings.pass_env),
    )
    result = run_session_judge(request)
    if isinstance(result, JudgeFailed):
        return allow(f"the test re-run was not judged: {result.reason}; not blocking")
    blocking = result.outcome == "fail" or (
        result.outcome == "needs_review" and settings.block_on_needs_review
    )
    if blocking:
        return _block(context, session_feedback(result.outcome, result.verdict))
    return Decision(notice=headline(result.outcome, result.verdict, "the test re-run"))


def _judge_turn(
    context: HookContext, settings: HookSettings, root: Path, spec: Path | None
) -> Decision:
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
        spec=spec,
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
