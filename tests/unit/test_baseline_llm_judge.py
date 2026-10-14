from __future__ import annotations

import pytest

from evidence_judge.baselines import (
    TranscriptLLMJudge,
    TranscriptVerdict,
    build_prompt,
    final_screenshot,
    format_actions,
    system_prompt,
)
from evidence_judge.core.verdict import Outcome
from evidence_judge.llm.base import LLMError, Tier
from evidence_judge.llm.fake import FakeLLM
from tests.unit.conftest import forged_run, honest_run


def verdict(value: str) -> TranscriptVerdict:
    return TranscriptVerdict.model_validate({"reasoning": "looked fine", "verdict": value})


@pytest.mark.parametrize(("label", "outcome"), [("PASS", Outcome.PASS), ("FAIL", Outcome.FAIL)])
async def test_maps_the_model_verdict(label: str, outcome: Outcome) -> None:
    llm = FakeLLM({"TranscriptVerdict": [verdict(label)]})
    result = await TranscriptLLMJudge(llm).judge(forged_run())
    assert result.outcome is outcome
    assert result.reason == "looked fine"
    assert result.usage.llm_calls == 1


async def test_prompt_holds_task_actions_reasoning_and_answer() -> None:
    run = forged_run().model_copy(
        update={
            "agent_answer": "I placed the order, #1042 confirmed.",
            "agent_reasoning": ["Opened the shop.", "Clicked Confirm order."],
        }
    )
    llm = FakeLLM({"TranscriptVerdict": [verdict("FAIL")]})
    await TranscriptLLMJudge(llm).judge(run, task="Buy a red kettle under $30")

    request = llm.calls[0].request
    assert request.tier is Tier.STRONG
    assert request.images == []
    assert "expert evaluator of autonomous web agents" in request.system
    assert "Do not rely only on the agent's own description" in request.system
    assert "screenshot" not in request.system
    assert "## Task\nBuy a red kettle under $30" in request.prompt
    assert "3. click #add-to-cart (page: https://shop.test/p/red-kettle)" in request.prompt
    assert "1. Opened the shop.\n2. Clicked Confirm order." in request.prompt
    assert "I placed the order, #1042 confirmed." in request.prompt


async def test_screenshot_is_attached_only_when_asked() -> None:
    run = honest_run()
    llm = FakeLLM({"TranscriptVerdict": [verdict("PASS"), verdict("PASS")]})
    with_shot = TranscriptLLMJudge(llm, include_screenshot=True)
    assert with_shot.name == "llm-transcript+screenshot"
    await with_shot.judge(run)
    await TranscriptLLMJudge(llm).judge(run)

    shot_request, plain_request = (c.request for c in llm.calls)
    assert len(shot_request.images) == 1
    assert shot_request.images[0].data == run.blobs["shot-7.jpg"]
    assert "screenshot of the final page" in shot_request.system
    assert plain_request.images == []


def test_final_screenshot_falls_back_to_the_last_one() -> None:
    run = honest_run()
    no_final = run.model_copy(update={"final_state": None})
    image = final_screenshot(no_final)
    assert image is not None
    assert image.data == run.blobs["shot-7.jpg"]
    assert final_screenshot(run.model_copy(update={"screenshots": []})) is None


def test_actions_are_listed_with_values_errors_and_a_cap() -> None:
    run = honest_run()
    listing = format_actions(run)
    assert "6. fill #card-number value='4242 4242 4242 4242'" in listing
    assert format_actions(run, limit=2).endswith("... 5 more actions not shown")
    assert format_actions(run.model_copy(update={"actions": []})) == "(no actions recorded)"


def test_prompt_falls_back_to_the_run_task() -> None:
    assert "## Task\nBuy a red kettle under $30" in build_prompt(forged_run(), task=None)
    assert system_prompt(with_screenshot=False) != system_prompt(with_screenshot=True)


async def test_llm_errors_propagate() -> None:
    llm = FakeLLM({"TranscriptVerdict": [LLMError("boom")]})
    with pytest.raises(LLMError):
        await TranscriptLLMJudge(llm).judge(forged_run())
