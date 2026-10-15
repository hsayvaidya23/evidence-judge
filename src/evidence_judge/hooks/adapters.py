"""Hook I/O for each coding agent: stdin JSON in, a decision JSON object out.

* Claude Code ``Stop`` / ``SubagentStop``: ``{"decision": "block", "reason": ...}`` keeps the
  agent working; ``systemMessage`` shows the user a notice.
* Codex ``Stop``: the same output contract; ``reason`` becomes the continuation prompt.
* Gemini CLI ``AfterAgent``: ``{"decision": "deny", "reason": ...}`` rejects the response and
  sends ``reason`` back as a new prompt.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from evidence_judge.hooks.core import Decision, HookContext
from evidence_judge.hooks.record import Agent
from evidence_judge.hooks.transcripts import Turn, claude_code_turn, codex_turn

type HookInput = Mapping[str, object]
type HookOutput = dict[str, object]


def _str(data: HookInput, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _transcript(data: HookInput, *keys: str) -> Path | None:
    for key in keys:
        value = _str(data, key)
        if value and Path(value).is_absolute() and Path(value).is_file():
            return Path(value)
    return None


def _context(
    data: HookInput,
    answer_key: str,
    turn: Turn,
    transcript: Path | None = None,
    kind: str | None = None,
) -> HookContext:
    return HookContext(
        cwd=_str(data, "cwd") or "",
        stop_hook_active=data.get("stop_hook_active") is True,
        answer=_str(data, answer_key),
        turn=turn,
        session_id=_str(data, "session_id") or "",
        transcript=transcript,
        transcript_kind=kind if transcript is not None else None,
    )


def claude_code_context(data: HookInput) -> HookContext:
    path = _transcript(data, "agent_transcript_path", "transcript_path")
    turn = claude_code_turn(path) if path else Turn()
    return _context(data, "last_assistant_message", turn, path, "claude-code")


def codex_context(data: HookInput) -> HookContext:
    path = _transcript(data, "transcript_path")
    turn = codex_turn(path, _str(data, "turn_id")) if path else Turn()
    return _context(data, "last_assistant_message", turn, path, "codex")


def gemini_context(data: HookInput) -> HookContext:
    return _context(data, "prompt_response", Turn(prompt=_str(data, "prompt")))


def _stop_output(decision: Decision) -> HookOutput:
    if decision.block:
        return {"decision": "block", "reason": decision.reason}
    return {"systemMessage": decision.notice} if decision.notice else {}


def _gemini_output(decision: Decision) -> HookOutput:
    if decision.block:
        return {"decision": "deny", "reason": decision.reason}
    return {"systemMessage": decision.notice} if decision.notice else {}


@dataclass(frozen=True, slots=True)
class Adapter:
    name: Agent
    context: Callable[[HookInput], HookContext]
    output: Callable[[Decision], HookOutput]


CLAUDE_CODE: Final = Adapter("claude-code", claude_code_context, _stop_output)
CODEX: Final = Adapter("codex", codex_context, _stop_output)
GEMINI: Final = Adapter("gemini", gemini_context, _gemini_output)
