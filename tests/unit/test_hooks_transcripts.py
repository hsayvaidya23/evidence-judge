"""Reading the current turn from synthetic Claude Code transcripts and Codex rollouts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_judge.hooks.transcripts import (
    Turn,
    claude_code_turn,
    codex_turn,
    is_playwright_tool,
    trace_links,
)
from tests.unit.hook_fixtures import (
    FORGED_ANSWER,
    claude_transcript,
    codex_rollout,
    stop_tracing_transcript,
)

PROMPT_AT = 1_791_000_000.0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__playwright__browser_click", True),
        ("mcp__plugin_playwright_playwright__browser_navigate", True),
        ("mcp__chrome-devtools__click", False),
        ("Bash", False),
        ("playwright", False),
    ],
)
def test_is_playwright_tool(name: str, expected: bool) -> None:
    assert is_playwright_tool(name) is expected


def test_claude_code_turn_reads_the_last_prompt_and_tools(tmp_path: Path) -> None:
    turn = claude_code_turn(claude_transcript(tmp_path / "t.jsonl", PROMPT_AT))
    assert turn == Turn(
        PROMPT_AT, True, "Buy a red kettle under $30 on the test shop", FORGED_ANSWER
    )


def test_claude_code_turn_with_text_block_prompt_and_no_browser(tmp_path: Path) -> None:
    lines = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "Fix the bug"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"type": "assistant", "message": {"content": "not a list"}},
        {"type": "assistant", "message": "not an object"},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))
    assert claude_code_turn(path) == Turn(None, False, "Fix the bug", None)


def test_claude_code_turn_without_a_prompt_in_the_tail(tmp_path: Path) -> None:
    path = claude_transcript(tmp_path / "t.jsonl", PROMPT_AT)
    turn = claude_code_turn(path, limit=600)
    assert turn.started_at is None
    assert turn.prompt is None
    assert turn.last_text == FORGED_ANSWER


def test_claude_code_turn_reads_across_chunks(tmp_path: Path) -> None:
    filler = {"type": "attachment", "pad": "x" * 300_000}
    path = claude_transcript(tmp_path / "t.jsonl", PROMPT_AT)
    path.write_text(path.read_text() + json.dumps(filler) + "\n")
    assert claude_code_turn(path).started_at == PROMPT_AT


def test_a_missing_transcript_is_an_empty_turn(tmp_path: Path) -> None:
    assert claude_code_turn(tmp_path / "nope.jsonl") == Turn()
    assert codex_turn(tmp_path / "nope.jsonl", "t") == Turn()


def test_codex_turn_finds_its_start_and_playwright(tmp_path: Path) -> None:
    path = codex_rollout(tmp_path / "r.jsonl", PROMPT_AT)
    assert codex_turn(path, "turn-2") == Turn(float(int(PROMPT_AT)), True)
    assert codex_turn(path, "turn-1") == Turn(1_767_258_000.0, True)
    assert codex_turn(path, None).started_at == float(int(PROMPT_AT))
    assert codex_turn(path, "turn-9") == Turn(None, True)


def test_codex_turn_falls_back_to_the_record_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    record = {
        "timestamp": "2026-10-15T09:00:00Z",
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "t", "started_at": "soon"},
    }
    path.write_text(json.dumps(record) + "\n" + json.dumps({"payload": "x"}) + "\n")
    turn = codex_turn(path, "t")
    assert turn.started_at is not None
    assert turn.used_playwright is False


def test_claude_code_turn_reads_the_traces_stop_tracing_reported(tmp_path: Path) -> None:
    path = stop_tracing_transcript(tmp_path / "t.jsonl", ".playwright-mcp/traces/trace-7.trace")
    turn = claude_code_turn(path)
    assert turn.trace_paths == (".playwright-mcp/traces/trace-7.trace",)
    assert turn.used_playwright is True


def test_trace_links_only_match_the_trace_link() -> None:
    text = "- [Trace](/a b/t-1.trace)\n- [Network log](/a/t-1.network)\n[Trace](x.zip)"
    assert trace_links(text) == ["/a b/t-1.trace"]


def test_codex_turn_reads_reported_traces(tmp_path: Path) -> None:
    path = codex_rollout(tmp_path / "r.jsonl", PROMPT_AT)
    output = {
        "ordinal": 9,
        "timestamp": "2026-10-15T09:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "c9",
            "output": "Trace recording stopped.\n- [Trace](.playwright-mcp/traces/trace-9.trace)",
        },
    }
    structured = output | {
        "payload": output["payload"] | {"output": {"content": "- [Trace](b.trace)"}}
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(output) + "\n" + json.dumps(structured) + "\n")
    turn = codex_turn(path, "turn-2")
    assert turn.trace_paths == (".playwright-mcp/traces/trace-9.trace", "b.trace")
