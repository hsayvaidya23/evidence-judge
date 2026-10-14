from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.core.evidence import Channel
from evidence_judge.ingest import BrowserUseFormatError, load_browser_use_history

HISTORY = Path(__file__).resolve().parent.parent / "fixtures" / "browser_use" / "history.json"
PNG = (HISTORY.parent / "screenshots" / "step-1.png").read_bytes()


def write_history(path: Path, *steps: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"history": list(steps)}))
    return path


def test_answer_and_success_come_from_done() -> None:
    run = load_browser_use_history(HISTORY)

    assert run.source == "browser-use"
    assert run.run_id == "history"
    assert run.agent_answer == (
        "I bought the Red Kettle for $24.00 (under $30). Order #1001 is confirmed."
    )
    assert run.metadata["agent_reported_success"] is True


def test_actions_are_normalised_and_done_is_not_an_action() -> None:
    run = load_browser_use_history(HISTORY)

    assert [(a.name, a.selector, a.value) for a in run.actions] == [
        ("goto", None, "http://127.0.0.1:8000/"),
        ("click", "xpath=html/body/main/div[1]/h2/a", None),
        ("fill", "#qty", "1"),
        ("click", "#add-to-cart", None),
        ("click", "index=12", None),
        ("goto", None, "http://127.0.0.1:8000/checkout"),
        ("click", "#confirm-order", None),
    ]
    assert [a.id for a in run.actions] == [f"act#{i}" for i in range(1, 8)]
    assert run.actions[1].url == "http://127.0.0.1:8000/"
    assert run.actions[4].error is not None and "not available" in run.actions[4].error
    assert [a.ts for a in run.actions] == [0.0, 6500.0, 13000.0, 13000.0, 19500.0, 26000.0, 32500.0]


def test_actions_without_results_were_not_executed() -> None:
    run = load_browser_use_history(HISTORY)

    assert run.metadata["skipped_actions"] == 1
    assert "index=9" not in [a.selector for a in run.actions]


def test_reasoning_has_one_entry_per_step() -> None:
    run = load_browser_use_history(HISTORY)

    assert len(run.agent_reasoning) == 7
    second = run.agent_reasoning[1].splitlines()
    assert second[0] == "Step 2"
    assert second[1].startswith("Thinking: The home page lists three kettles.")
    assert second[2].startswith("Evaluation: Success")
    assert second[3].startswith("Memory: ")
    assert second[4] == "Next goal: Open the Red Kettle product page."


def test_screenshots_load_from_relative_paths() -> None:
    run = load_browser_use_history(HISTORY)

    assert run.captured == {Channel.ACTIONS, Channel.SCREENSHOTS}
    assert [(s.id, s.resource, s.mime_type) for s in run.screenshots] == [
        ("shot#1", "browser-use/step-1.png", "image/png"),
        ("shot#2", "browser-use/step-2.png", "image/png"),
        ("shot#3", "browser-use/step-3.png", "image/png"),
    ]
    assert run.blobs["browser-use/step-1.png"] == PNG
    assert run.metadata["warnings"] == [
        "screenshot not found: /tmp/browser_use_agent_0686f0d2/screenshots/step_4.png"
    ]


def test_final_state_has_url_but_no_page_text() -> None:
    run = load_browser_use_history(HISTORY)

    final = run.final_state
    assert final is not None
    assert final.url == "http://127.0.0.1:8000/orders/1001"
    assert final.title == "Order #1001 | Kettle Co."
    assert final.text == ""
    assert final.screenshot_id is None
    assert Channel.FINAL_STATE not in run.captured
    assert run.network == [] and run.snapshots == []


def test_legacy_current_state_and_inline_screenshot(tmp_path: Path) -> None:
    history = write_history(
        tmp_path / "legacy.json",
        {
            "model_output": {
                "current_state": {
                    "evaluation_previous_goal": "Unknown",
                    "memory": "Start",
                    "next_goal": "Open the shop",
                },
                "action": [
                    {"go_to_url": {"url": "https://shop.test/"}},
                    {"input_text": {"index": 3, "text": "kettle"}},
                    {"scroll_down": {"amount": None}},
                ],
            },
            "result": [{"extracted_content": "ok"}, {}, {}],
            "state": {
                "url": "about:blank",
                "title": "",
                "screenshot": base64.b64encode(PNG).decode(),
            },
        },
    )

    run = load_browser_use_history(history)
    assert [(a.name, a.selector, a.value) for a in run.actions] == [
        ("goto", None, "https://shop.test/"),
        ("fill", "index=3", "kettle"),
        ("other", None, None),
    ]
    assert run.metadata["synthetic_timestamps"] is True
    assert run.agent_reasoning == [
        "Step 1\nEvaluation: Unknown\nMemory: Start\nNext goal: Open the shop"
    ]
    assert run.agent_answer is None
    assert run.blobs["browser-use/step-1.png"] == PNG
    assert run.final_state is not None and run.final_state.screenshot_id == "shot#1"


def test_step_without_model_output(tmp_path: Path) -> None:
    history = write_history(
        tmp_path / "h.json",
        {"model_output": None, "result": [{"error": "LLM call failed"}], "state": {"url": ""}},
    )

    run = load_browser_use_history(history)
    assert run.actions == []
    assert run.final_state is None


@pytest.mark.parametrize(
    ("content", "message"),
    [("{", "not valid JSON"), ('{"steps": []}', "missing 'history'"), ("[1]", "missing 'history'")],
)
def test_malformed_history(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content)

    with pytest.raises(BrowserUseFormatError, match=message):
        load_browser_use_history(path)


def test_empty_history_is_valid(tmp_path: Path) -> None:
    run = load_browser_use_history(write_history(tmp_path / "empty.json"))

    assert run.actions == [] and run.agent_answer is None
