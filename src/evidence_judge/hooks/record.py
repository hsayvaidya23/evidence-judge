"""The evidence recorder: append one JSON line per agent tool call, prompt and stop to a
per-session log outside the workspace (log format version 1, see
``docs/design/coding-agent-sessions.md``).

Lines are hash-chained (``seq`` from 0, ``prev`` = SHA-256 hex of the previous line's bytes) and
written under an exclusive lock with append + fsync, so concurrent async hooks cannot interleave
or fork the chain. Excerpts are redacted and bounded; ``stdout_sha256`` hashes the full raw
output, and ``output_truncated`` is true whenever the excerpt is cut or redaction changed it, so
a redacted excerpt never counts as the whole output.

Exit codes are never guessed:

* Claude Code: ``PostToolUse`` of ``Bash`` without ``returnCodeInterpretation`` is 0; a
  ``PostToolUseFailure`` whose ``error`` starts ``Exit code N`` is N; anything else is null.
* Codex: the hook payload's ``exit_code`` / ``exitCode`` when present, else the
  first ``Process exited with code N`` line of the tool's text output (Codex writes it before
  the command's own output), else null.
* Gemini CLI: the ``Exit Code: N`` line of a ``run_shell_command`` result, else null.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast

from evidence_judge.config import Settings, stable_redaction_key
from evidence_judge.redact import Redactor

FORMAT_VERSION: Final = 1
DEFAULT_LOG_DIR: Final = Path.home() / ".evidence-judge" / "sessions"
EXCERPT_CHARS: Final = 2_000
MAX_ANSWER_CHARS: Final = 50_000
_INPUT_STRING_CHARS: Final = 1_000
_MAX_DEPTH: Final = 6
_MAX_ITEMS: Final = 50
_TAIL_READ: Final = 1024 * 1024
_CLAUDE_EXIT: Final = re.compile(r"\AExit code (-?\d+)\b")
_GEMINI_EXIT: Final = re.compile(r"^Exit Code: (-?\d+)\b", re.M)
_CODEX_EXIT: Final = re.compile(r"^Process exited with code (-?\d+)\b", re.M)
_SESSION_ID: Final = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")
_CLAUDE_RESULT_KEYS: Final = ("structuredPatch", "type", "userModified")
_CODEX_RESULT_KEYS: Final = ("changes",)

type Agent = Literal["claude-code", "codex", "gemini"]
type Status = Literal["ok", "error", "denied", "interrupted"]
type JsonValue = object
type Payload = Mapping[str, JsonValue]
type Entry = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _Call:
    status: Status | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    result: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])


def log_path(log_dir: Path, session_id: str) -> Path:
    """The session's log file; an unusual id is replaced by its hash, so it cannot escape."""
    if _SESSION_ID.match(session_id) and session_id not in (".", ".."):
        name = session_id
    else:
        name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return log_dir / f"{name}.jsonl"


def resolve_log_dir(option: str | None) -> Path:
    """``--log-dir`` when it is a usable absolute path (an unexpanded ``${...}`` is not), else
    ``EVJ_HOOK_LOG_DIR``, else ``~/.evidence-judge/sessions``."""
    for value in (option, os.environ.get("EVJ_HOOK_LOG_DIR")):
        if value and "${" not in value and Path(value).is_absolute():
            return Path(value)
    return DEFAULT_LOG_DIR


def redactor() -> Redactor:
    """The installation's stable redactor, so equal values get equal tokens across hooks."""
    try:
        settings = Settings()
        key = stable_redaction_key(settings)
        return Redactor(key) if key is not None else Redactor.from_settings(settings)
    except Exception:
        return Redactor(os.urandom(32))


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n… [{len(text) - limit} characters omitted] …\n{text[-half:]}"


def _excerpt(text: str, redact: Redactor, limit: int = EXCERPT_CHARS) -> tuple[str, bool]:
    """(redacted excerpt, whether it differs from ``text``)."""
    out = redact.redact_text(_cut(text, limit))[0]
    return out, out != text


def _bounded(value: JsonValue, redact: Redactor, depth: int = 0) -> JsonValue:
    """A redacted copy of a JSON value with long strings and large containers cut."""
    if isinstance(value, str):
        return _excerpt(value, redact, _INPUT_STRING_CHARS)[0]
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= _MAX_DEPTH:
        return "…"
    if isinstance(value, Mapping):
        items = list(cast(Mapping[object, JsonValue], value).items())[:_MAX_ITEMS]
        return {str(k): _bounded(v, redact, depth + 1) for k, v in items}
    if isinstance(value, list | tuple):
        values = cast(list[JsonValue], value)[:_MAX_ITEMS]
        return [_bounded(v, redact, depth + 1) for v in values]
    return str(value)[:_INPUT_STRING_CHARS]


def _as_text(value: JsonValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _fields(value: JsonValue) -> Payload:
    return cast(Payload, value) if isinstance(value, Mapping) else {}


def _int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(data: Payload, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _pick(fields: Payload, keys: tuple[str, ...]) -> dict[str, JsonValue]:
    return {k: fields[k] for k in keys if k in fields}


def _claude_code(event: str, tool: str, data: Payload) -> _Call:
    if event == "PermissionDenied":
        return _Call("denied", None, None, _as_text(data.get("reason")))
    if event == "PostToolUseFailure":
        error = _as_text(data.get("error")) or ""
        found = _CLAUDE_EXIT.match(error)
        if data.get("is_interrupt") is True:
            return _Call("interrupted", None, error, None)
        code = int(found.group(1)) if found and tool == "Bash" else None
        return _Call("error", code, error, None)
    response = data.get("tool_response")
    fields = _fields(response)
    result = _pick(fields, _CLAUDE_RESULT_KEYS)
    if "stdout" in fields or "stderr" in fields:
        stdout, stderr = _as_text(fields.get("stdout")), _as_text(fields.get("stderr"))
        if fields.get("interrupted") is True:
            return _Call("interrupted", None, stdout, stderr, result)
        plain = tool == "Bash" and "returnCodeInterpretation" not in fields
        return _Call("ok", 0 if plain else None, stdout, stderr, result)
    stdout = None if result and isinstance(response, Mapping) else _as_text(response)
    return _Call("ok", None, stdout, None, result)


def _codex(data: Payload) -> _Call:
    response = data.get("tool_response")
    fields = _fields(response)
    code = _int(fields.get("exit_code"))
    if code is None:
        code = _int(fields.get("exitCode"))
    stdout = _as_text(fields.get("stdout")) if "stdout" in fields else _as_text(response)
    if code is None:
        found = _CODEX_EXIT.search(_as_text(response) or "")
        code = int(found.group(1)) if found else None
    status: Status | None = None if code is None else ("ok" if code == 0 else "error")
    result = _pick(fields, _CODEX_RESULT_KEYS) or _pick(
        _fields(data.get("tool_input")), ("changes",)
    )
    return _Call(status, code, stdout, _as_text(fields.get("stderr")), result)


def _gemini(tool: str, data: Payload) -> _Call:
    fields = _fields(data.get("tool_response"))
    content = fields.get("llmContent")
    stdout = _as_text(content if content is not None else fields.get("returnDisplay"))
    error = fields.get("error")
    stderr = _as_text(_fields(error).get("message") or error) if error else None
    found = _GEMINI_EXIT.search(stdout or "") if tool == "run_shell_command" else None
    code = int(found.group(1)) if found else None
    status: Status = "error" if error is not None or (code is not None and code != 0) else "ok"
    return _Call(status, code, stdout, stderr)


_TOOL_EVENTS: Final[Mapping[Agent, frozenset[str]]] = {
    "claude-code": frozenset({"PostToolUse", "PostToolUseFailure", "PermissionDenied"}),
    "codex": frozenset({"PostToolUse"}),
    "gemini": frozenset({"AfterTool"}),
}
_PROMPT_EVENTS: Final[Mapping[Agent, str]] = {
    "claude-code": "UserPromptSubmit",
    "codex": "UserPromptSubmit",
    "gemini": "BeforeAgent",
}


def _base(agent: Agent, data: Payload, now: datetime | None) -> Entry:
    return {
        "v": FORMAT_VERSION,
        "agent": agent,
        "ts": (now or datetime.now(UTC)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "session_id": _str(data, "session_id") or "",
    }


def _tool_entry(agent: Agent, event: str, tool: str, data: Payload, redact: Redactor) -> Entry:
    if agent == "claude-code":
        call = _claude_code(event, tool, data)
    elif agent == "codex":
        call = _codex(data)
    else:
        call = _gemini(tool, data)
    stdout_excerpt, cut = _excerpt(call.stdout, redact) if call.stdout is not None else ("", False)
    stderr_excerpt = _excerpt(call.stderr, redact)[0] if call.stderr is not None else ""
    tool_input: JsonValue = _bounded(_fields(data.get("tool_input")), redact)
    entry: Entry = {
        "event": "tool",
        "tool": tool,
        "input": tool_input,
        "exit_code": call.exit_code,
        "stdout_sha256": (
            None if call.stdout is None else hashlib.sha256(call.stdout.encode()).hexdigest()
        ),
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
        "cwd": _str(data, "cwd") or "",
        "output_truncated": cut,
    }
    if call.stdout is not None:
        entry["stdout_bytes"] = len(call.stdout.encode("utf-8"))
    optional: dict[str, JsonValue] = {
        "status": call.status,
        "tool_use_id": _str(data, "tool_use_id"),
        "agent_id": _str(data, "agent_id"),
        "duration_ms": _int(data.get("duration_ms")),
        "result": _bounded(call.result, redact) if call.result else None,
    }
    entry.update({k: v for k, v in optional.items() if v is not None})
    return entry


def build_record(
    agent: Agent, data: Payload, redact: Redactor, now: datetime | None = None
) -> Entry | None:
    """The log line for one hook call (without ``seq``/``prev``), or None for an event the
    recorder does not log."""
    event = _str(data, "hook_event_name")
    tool = _str(data, "tool_name")
    if event in _TOOL_EVENTS[agent] and tool:
        return _base(agent, data, now) | _tool_entry(agent, event, tool, data, redact)
    if event == _PROMPT_EVENTS[agent]:
        prompt = _str(data, "prompt") or ""
        text = redact.redact_text(prompt[:MAX_ANSWER_CHARS])[0]
        return _base(agent, data, now) | {"event": "prompt", "text": text}
    return None


def stop_record(
    agent: Agent, data: Payload, answer: str | None, redact: Redactor, now: datetime | None = None
) -> Entry:
    """The ``stop`` line that carries the agent's final answer."""
    text = redact.redact_text((answer or "")[:MAX_ANSWER_CHARS])[0]
    entry = _base(agent, data, now) | {"event": "stop", "last_assistant_message": text}
    agent_id = _str(data, "agent_id")
    return entry | {"agent_id": agent_id} if agent_id else entry


@contextmanager
def _locked(fd: int) -> Generator[None]:
    try:
        import fcntl
    except ImportError:
        yield
        return
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _last_line(fd: int) -> bytes | None:
    size = os.fstat(fd).st_size
    if size == 0:
        return None
    start = max(0, size - _TAIL_READ)
    data = os.pread(fd, size - start, start).rstrip(b"\n")
    return data.rsplit(b"\n", 1)[-1]


def _chain(previous: bytes | None) -> tuple[int, str | None]:
    if previous is None:
        return 0, None
    try:
        seq = json.loads(previous).get("seq")
    except (ValueError, AttributeError):
        seq = None
    nxt = seq + 1 if isinstance(seq, int) and not isinstance(seq, bool) else 0
    return nxt, hashlib.sha256(previous).hexdigest()


def append(log_dir: Path, entry: Mapping[str, JsonValue]) -> Path:
    """Append ``entry`` with its ``seq``/``prev`` to its session's log (directory 0700, file
    0600), under an exclusive lock, and fsync it."""
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = entry.get("session_id")
    path = log_path(log_dir, session if isinstance(session, str) and session else "unknown")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        with _locked(fd):
            seq, prev = _chain(_last_line(fd))
            line = json.dumps({**entry, "seq": seq, "prev": prev}, ensure_ascii=False, default=str)
            os.write(fd, line.encode("utf-8") + b"\n")
            os.fsync(fd)
    finally:
        os.close(fd)
    return path


def record(agent: Agent, data: Payload, log_dir: Path) -> Path | None:
    """Log one hook call. Returns the log file, or None when the event is not logged."""
    entry = build_record(agent, data, redactor())
    return None if entry is None else append(log_dir, entry)
