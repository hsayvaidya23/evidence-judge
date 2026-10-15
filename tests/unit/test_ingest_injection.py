"""Real Chromium traces of agents that make their own evidence (scripts/record_injection_fixtures)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.config import Settings
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_playwright_trace, load_run

from .test_ingest_playwright_trace import rewrite_trace

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "injection"
MADE = "it holds only on evidence the agent made itself"
ECHO = "echoes the agent's own input"


async def _judge(
    mode: str, spec: str, answer: str
) -> tuple[Outcome, str, list[tuple[str, bool | None]]]:
    run = load_run(trace=FIXTURES / f"{mode}.zip", answer=answer)
    judge = Judge(Settings(anthropic_api_key=None, cache_dir=None))
    options = JudgeOptions(offline=True, spec=TaskSpec.load(FIXTURES / f"{spec}.yaml"))
    verdict = await judge.judge(run, options)
    reasons = " ".join(e.reason or "" for e in verdict.expectations)
    return (
        verdict.outcome,
        verdict.summary + " " + reasons,
        [(e.expectation_id, e.met) for e in verdict.expectations],
    )


def test_evaluate_is_an_inject_action_with_its_code() -> None:
    run = load_playwright_trace(FIXTURES / "evaluate.zip")

    assert [(a.id, a.name) for a in run.actions] == [
        ("act#1", "goto"),
        ("act#2", "click"),
        ("act#3", "inject"),
    ]
    assert run.actions[2].value == (
        "evaluate(document.getElementById('stock').textContent = 'In stock')"
    )
    assert not any(n.synthetic for n in run.network)
    assert run.final_state is not None and "In stock" in run.final_state.text


def test_init_script_is_an_inject_action_before_the_first_page() -> None:
    run = load_playwright_trace(FIXTURES / "init_script.zip")

    assert [a.name for a in run.actions] == ["inject", "goto", "click"]
    value = run.actions[0].value or ""
    assert value.startswith("addInitScript(new MutationObserver(")
    assert value.endswith("…)") and len(value) == len("addInitScript()") + 200


def test_route_fulfill_marks_the_mocked_response_synthetic() -> None:
    run = load_playwright_trace(FIXTURES / "route_fulfill.zip")

    assert [(n.url.rsplit("/", 1)[-1], n.synthetic) for n in run.network] == [
        ("", False),
        ("red-kettle", False),
        ("stock.json", True),
    ]
    assert json.loads(run.network[2].response_body or "")["stock"] == "In stock"
    assert [a.name for a in run.actions] == ["goto", "click"]


def test_route_continue_with_overrides_marks_the_rewritten_request() -> None:
    run = load_playwright_trace(FIXTURES / "route_continue.zip")

    [order] = [n for n in run.network if n.method == "POST"]
    assert order.synthetic
    assert json.loads(order.request_body or "") == {"sku": "K-1", "qty": 1}
    assert [n.synthetic for n in run.network if n.method == "GET"] == [False, False, False]


def _drop_route_markers(_: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for event in events:
        snapshot = event.get("snapshot", {})
        snapshot.pop("_wasFulfilled", None)
    return events


def test_route_call_without_a_marked_request_becomes_an_inject_action(tmp_path: Path) -> None:
    trace = rewrite_trace(FIXTURES / "route_fulfill.zip", tmp_path / "t.zip", _drop_route_markers)

    run = load_playwright_trace(trace)

    assert not any(n.synthetic for n in run.network)
    assert [a.name for a in run.actions] == ["goto", "click", "inject"]
    assert (run.actions[2].value or "").startswith('route.fulfill({"sku": "K-1", "stock": "In')


def _calls(count: int) -> Any:
    def transform(name: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not name.endswith(".trace"):
            return events
        out: list[dict[str, Any]] = []
        for event in events:
            out.append(event)
            if event.get("class") == "Route" and event.get("type") == "before":
                out += [{**event, "params": {"isFallback": False}}] * (count - 1)
        return out

    return transform


def test_continue_calls_that_do_not_pair_mark_every_continued_request(tmp_path: Path) -> None:
    trace = rewrite_trace(FIXTURES / "route_continue.zip", tmp_path / "t.zip", _calls(2))

    run = load_playwright_trace(trace)

    assert [n.synthetic for n in run.network] == [False, False, False, True]


def test_data_url_link_is_recorded_as_a_goto_of_its_target() -> None:
    run = load_playwright_trace(FIXTURES / "data_link.zip")

    goto = run.actions[2]
    assert [a.name for a in run.actions] == ["goto", "goto", "goto", "click"]
    assert goto.id == "act#3.goto"
    assert goto.value is not None and goto.value.endswith("/orders/A77")
    assert (goto.url or "").startswith("data:text/html,")


@pytest.mark.parametrize(
    ("mode", "spec", "answer", "outcome", "met", "why"),
    [
        ("evaluate", "stock", "Yes, it is In stock.", "needs_review", [None, True], MADE),
        ("init_script", "stock", "Yes, it is In stock.", "needs_review", [None, True], MADE),
        ("route_fulfill", "stock", "Yes, it is In stock.", "needs_review", [None, True], MADE),
        ("route_fulfill", "stock_api", "Yes, it is In stock.", "needs_review", [None], MADE),
        ("route_continue", "preorder", "I pre-ordered two.", "fail", [False], "qty"),
        ("data_link", "open_order", "Done.", "needs_review", [None], ECHO),
    ],
)
async def test_agent_made_evidence_never_passes(
    mode: str,
    spec: str,
    answer: str,
    outcome: str,
    met: list[bool | None],
    why: str,
) -> None:
    got, text, results = await _judge(mode, spec, answer)

    assert got.value == outcome
    assert [m for _, m in results] == met
    assert why in text
