"""Turn coding-agent sessions recorded on a toy project into committable fixtures.

Record a session in a fresh directory that holds only the toy project, then pass its
transcript (Claude Code) or saved `codex exec --json` stdout (Codex) here with the sandbox
directory. The fixture keeps only what the session judge reads:

- Claude Code: `user` and `assistant` records only (never `attachment`, `api-request*` or other
  bookkeeping records, which carry harness context such as system prompts and account ids), an
  allow-list of keys per record and content block, and no `thinking` blocks.
- Codex: the thread and turn events, command and file-change items that stay inside the sandbox,
  and the final agent message. Items that read files outside the sandbox are dropped.

Then every string has the sandbox path, the home directory and the account name replaced by
placeholders (`/work/toy`, `/home`, `user`).

    uv run python -m scripts.record_session_fixtures claude-code SESSION.jsonl SANDBOX OUT
    uv run python -m scripts.record_session_fixtures codex EVENTS.jsonl SANDBOX OUT
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
from pathlib import Path
from typing import Any, Final

from scripts.fixture_hygiene import scrub_text

PLACEHOLDER: Final = "/work/toy"
_RECORD_KEYS: Final = (
    "type",
    "uuid",
    "parentUuid",
    "isSidechain",
    "isMeta",
    "timestamp",
    "cwd",
    "sessionId",
    "gitBranch",
    "version",
    "message",
    "toolUseResult",
)
_ASSISTANT_KEYS: Final = ("id", "type", "role", "model", "content", "stop_reason")
_BLOCK_KEYS: Final = {
    "text": ("type", "text"),
    "tool_use": ("type", "id", "name", "input"),
    "tool_result": ("type", "tool_use_id", "content", "is_error"),
}
_OUTSIDE: Final = re.compile(r"/Users/|/home/|~/|\.codex/|\.claude/|\.agents/")


def _scrubber(sandbox: Path) -> Any:
    account = getpass.getuser()
    roots = sorted({str(sandbox), str(sandbox.resolve())}, key=len, reverse=True)

    def scrub(text: str) -> str:
        for root in roots:
            text = text.replace(root, PLACEHOLDER)
        text = scrub_text(text)
        return re.sub(rf"\b{re.escape(account)}\b", "user", text)

    return scrub


def _walk(value: Any, scrub: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_walk(v, scrub) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, scrub) for k, v in value.items()}
    return value


def _pick(obj: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: obj[k] for k in keys if k in obj}


def _content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        keys = _BLOCK_KEYS.get(str(block.get("type"))) if isinstance(block, dict) else None
        if keys:
            blocks.append(_pick(block, keys))
    return blocks


def claude_code(source: Path, sandbox: Path, out: Path) -> int:
    scrub = _scrubber(sandbox)
    kept: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") not in ("user", "assistant"):
            continue
        slim = _pick(record, _RECORD_KEYS)
        message = record.get("message", {})
        if record["type"] == "assistant":
            slim["message"] = _pick(message, _ASSISTANT_KEYS)
        else:
            slim["message"] = {"role": message.get("role", "user")}
        slim["message"]["content"] = _content(message.get("content"))
        if record["type"] == "assistant" and not slim["message"]["content"]:
            continue
        kept.append(json.dumps(_walk(slim, scrub), ensure_ascii=False))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return len(kept)


def codex(source: Path, sandbox: Path, out: Path) -> int:
    scrub = _scrubber(sandbox)
    records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    messages = [
        r["item"]["id"]
        for r in records
        if r.get("type") == "item.completed" and r.get("item", {}).get("type") == "agent_message"
    ]
    final = messages[-1] if messages else None
    kept: list[str] = []
    for record in records:
        item = record.get("item")
        if isinstance(item, dict):
            kind = item.get("type")
            if kind == "agent_message" and item.get("id") != final:
                continue
            if kind not in ("agent_message", "command_execution", "file_change"):
                continue
            text = json.dumps(item).replace(str(sandbox), PLACEHOLDER)
            if _OUTSIDE.search(text):
                continue
        kept.append(json.dumps(_walk(record, scrub), ensure_ascii=False))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return len(kept)


def main() -> None:
    parser = argparse.ArgumentParser(description="Make a session fixture from a toy session.")
    parser.add_argument("format", choices=("claude-code", "codex"))
    parser.add_argument("source", type=Path)
    parser.add_argument("sandbox", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    convert = claude_code if args.format == "claude-code" else codex
    print(f"wrote {convert(args.source, args.sandbox, args.out)} records to {args.out}")


if __name__ == "__main__":
    main()
