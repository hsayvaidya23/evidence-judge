"""Read the current turn from an agent's JSONL transcript: when it started, whether it drove a
browser through Playwright MCP, and the agent's last text. Only the file's tail is read, and
anything unreadable yields an empty :class:`Turn` rather than an error."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast

TAIL_BYTES: Final = 16 * 1024 * 1024
_CHUNK: Final = 256 * 1024

type JsonObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class Turn:
    started_at: float | None = None
    used_playwright: bool = False
    prompt: str | None = None
    last_text: str | None = None


def is_playwright_tool(name: str) -> bool:
    """Playwright MCP tools, however the server is registered: ``mcp__playwright__browser_click``,
    ``mcp__plugin_playwright_playwright__browser_click`` and the like."""
    lowered = name.lower()
    return lowered.startswith("mcp__") and "playwright" in lowered


def _reversed_lines(path: Path, limit: int) -> Iterator[bytes]:
    """The file's lines, last first, reading at most ``limit`` bytes from its end. A line cut
    by the limit is dropped."""
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        floor = max(0, size - limit)
        position = size
        rest = b""
        while position > floor:
            step = min(_CHUNK, position - floor)
            position -= step
            handle.seek(position)
            lines = (handle.read(step) + rest).split(b"\n")
            rest = lines[0]
            yield from reversed(lines[1:])
        if floor == 0:
            yield rest


def _records(path: Path, limit: int) -> Iterator[JsonObject]:
    for line in _reversed_lines(path, limit):
        if not line.strip():
            continue
        try:
            record: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield cast(JsonObject, record)


def _timestamp(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _blocks(message: object) -> list[JsonObject]:
    if not isinstance(message, dict):
        return []
    content = cast(JsonObject, message).get("content")
    if not isinstance(content, list):
        return []
    return [cast(JsonObject, b) for b in cast(list[object], content) if isinstance(b, dict)]


def _prompt_text(message: object) -> str | None:
    """The text of a real user prompt; None for tool results."""
    if not isinstance(message, dict):
        return None
    body = cast(JsonObject, message)
    content = body.get("content")
    if isinstance(content, str):
        return content
    texts = [b.get("text") for b in _blocks(body) if b.get("type") == "text"]
    joined = "\n".join(t for t in texts if isinstance(t, str))
    return joined or None


def claude_code_turn(path: Path, *, limit: int = TAIL_BYTES) -> Turn:
    """The last turn of a Claude Code transcript: everything after the last user prompt."""
    used = False
    last_text: str | None = None
    try:
        for record in _records(path, limit):
            kind = record.get("type")
            if kind == "assistant":
                for block in reversed(_blocks(record.get("message"))):
                    name = block.get("name")
                    if block.get("type") == "tool_use" and isinstance(name, str):
                        used = used or is_playwright_tool(name)
                    text = block.get("text")
                    if last_text is None and block.get("type") == "text" and isinstance(text, str):
                        last_text = text
            elif kind == "user" and not record.get("isMeta"):
                prompt = _prompt_text(record.get("message"))
                if prompt is not None:
                    return Turn(_timestamp(record.get("timestamp")), used, prompt, last_text)
    except OSError:
        return Turn()
    return Turn(None, used, None, last_text)


def codex_turn(path: Path, turn_id: str | None, *, limit: int = TAIL_BYTES) -> Turn:
    """The turn ``turn_id`` of a Codex rollout: its ``task_started`` time and whether any tool
    call since then went to a Playwright MCP server. The rollout format is internal to Codex,
    so this is best effort."""
    used = False
    try:
        for record in _records(path, limit):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            body = cast(JsonObject, payload)
            kind = body.get("type")
            if kind in ("function_call", "custom_tool_call", "mcp_tool_call"):
                name = f"{body.get('namespace') or ''}{body.get('name') or ''}"
                used = used or "playwright" in name.lower()
            elif kind == "task_started" and (turn_id is None or body.get("turn_id") == turn_id):
                started = _timestamp(body.get("started_at"))
                return Turn(started or _timestamp(record.get("timestamp")), used)
    except OSError:
        return Turn()
    return Turn(None, used)
