"""``evidence-judge hook ...``: entry points coding agents run after tool calls and on stop."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Final, TextIO, cast

import typer
from pydantic import ValidationError

from evidence_judge.hooks.adapters import CLAUDE_CODE, CODEX, GEMINI, Adapter, HookInput
from evidence_judge.hooks.core import NOTICE_PREFIX, Decision, allow, evaluate
from evidence_judge.hooks.record import (
    Agent,
    append,
    record,
    redactor,
    resolve_log_dir,
    stop_record,
)
from evidence_judge.hooks.settings import HookSettings

MAX_INPUT_CHARS: Final = 8 * 1024 * 1024

hook_app = typer.Typer(
    name="hook",
    help=(
        "Hooks for coding agents. Stop hooks read the hook JSON on stdin, judge the newest "
        "trace and write the decision JSON on stdout; record hooks append tool calls to the "
        "session's evidence log. Always exit 0; problems become a notice."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _read_input(stream: TextIO) -> HookInput:
    text = stream.read(MAX_INPUT_CHARS + 1)
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError("hook input is too large")
    data: object = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    return cast(HookInput, data)


def _log_stop(adapter: Adapter, data: HookInput, answer: str | None, log_dir: Path) -> None:
    try:
        append(log_dir, stop_record(adapter.name, data, answer, redactor()))
    except Exception as exc:
        sys.stderr.write(f"{NOTICE_PREFIX}could not record the stop ({type(exc).__name__})\n")


def decide(adapter: Adapter, stream: TextIO, log_dir: Path | None = None) -> Decision:
    """The decision for one hook call. Fails open: any error allows the stop with a notice.
    With ``log_dir``, the stop and the agent's final answer are also appended to the session's
    evidence log."""
    try:
        settings = HookSettings()
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        return allow(f"invalid EVJ_HOOK_* settings ({fields}); not checking")
    try:
        data = _read_input(stream)
        context = adapter.context(data)
        if log_dir is not None:
            _log_stop(adapter, data, context.answer, log_dir)
        return evaluate(context, settings)
    except ValueError as exc:
        return allow(f"unreadable hook input ({exc}); not checking")
    except Exception as exc:
        return allow(f"internal error ({type(exc).__name__}: {exc}); not blocking")


def _respond(adapter: Adapter, log_dir: str | None) -> None:
    target = resolve_log_dir(log_dir) if log_dir or os.environ.get("EVJ_HOOK_LOG_DIR") else None
    decision = decide(adapter, sys.stdin, target)
    sys.stdout.write(json.dumps(adapter.output(decision), ensure_ascii=False) + "\n")
    sys.stdout.flush()


_LogDir = Annotated[
    str | None,
    typer.Option(
        help="Evidence-log directory (default ~/.evidence-judge/sessions); stop hooks log only "
        "when this or EVJ_HOOK_LOG_DIR is set"
    ),
]


@hook_app.command("claude-code-stop")
def claude_code_stop(log_dir: _LogDir = None) -> None:
    """Claude Code Stop / SubagentStop hook."""
    _respond(CLAUDE_CODE, log_dir)


def _record(agent: Agent, log_dir: str | None) -> None:
    """Log one tool call or prompt. Never fails the agent: a problem is one line on stderr."""
    try:
        record(agent, _read_input(sys.stdin), resolve_log_dir(log_dir))
    except Exception as exc:
        sys.stderr.write(f"{NOTICE_PREFIX}could not record the tool call ({type(exc).__name__})\n")


@hook_app.command("claude-code-record")
def claude_code_record(log_dir: _LogDir = None) -> None:
    """Claude Code PostToolUse, PostToolUseFailure, PermissionDenied and UserPromptSubmit
    hook: log the call or prompt, print nothing."""
    _record("claude-code", log_dir)


@hook_app.command("codex-record")
def codex_record(log_dir: _LogDir = None) -> None:
    """Codex CLI PostToolUse and UserPromptSubmit hook: log the call or prompt, print nothing."""
    _record("codex", log_dir)


@hook_app.command("gemini-record")
def gemini_record(log_dir: _LogDir = None) -> None:
    """Gemini CLI AfterTool and BeforeAgent hook: log the call or prompt, print ``{}``."""
    _record("gemini", log_dir)
    sys.stdout.write("{}\n")


@hook_app.command("codex-stop")
def codex_stop(log_dir: _LogDir = None) -> None:
    """Codex CLI Stop hook."""
    _respond(CODEX, log_dir)


@hook_app.command("gemini-after-agent")
def gemini_after_agent(log_dir: _LogDir = None) -> None:
    """Gemini CLI AfterAgent hook."""
    _respond(GEMINI, log_dir)
