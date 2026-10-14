from __future__ import annotations

import pytest

from evidence_judge.forgery import (
    KIND_DESCRIPTIONS,
    ForgeryDraft,
    ForgeryKind,
    LLMForger,
    build_forgery_prompt,
)
from evidence_judge.forgery.llm_forger import FORGERY_SYSTEM_PROMPT
from evidence_judge.llm.base import LLMError
from evidence_judge.llm.fake import FakeLLM
from tests.unit.conftest import forged_run

STORY = ForgeryDraft(
    answer="I bought the red kettle; order #5531 is confirmed.",
    reasoning=["I added it to the cart.", "  ", "I confirmed the order."],
)


async def test_llm_forger_replaces_only_the_story() -> None:
    run = forged_run()
    llm = FakeLLM({"ForgeryDraft": [STORY]})

    forged = await LLMForger(llm).forge(run, ForgeryKind.ENVIRONMENT_BLAMING, seed=2)

    assert forged.agent_answer == STORY.answer
    assert forged.agent_reasoning == ["I added it to the cart.", "I confirmed the order."]
    assert forged.actions == run.actions
    assert forged.network == run.network
    assert forged.final_state == run.final_state
    assert forged.metadata["forgery"] == {
        "kind": "environment_blaming",
        "base_run_id": run.run_id,
        "forger": "llm:fake",
        "seed": 2,
    }


async def test_llm_forger_prompt_describes_kind_and_fixed_actions() -> None:
    llm = FakeLLM({"ForgeryDraft": [STORY]})
    await LLMForger(llm).forge(forged_run(), ForgeryKind.REFLECTIVE_REASONING, seed=5)

    request = llm.calls[0].request
    assert request.system == FORGERY_SYSTEM_PROMPT
    assert "Do not change, add or remove actions" in request.system
    assert "plausible" in request.system
    assert KIND_DESCRIPTIONS[ForgeryKind.REFLECTIVE_REASONING] in request.prompt
    assert "click #add-to-cart" in request.prompt
    assert "Buy a red kettle under $30" in request.prompt
    assert "Variation seed: 5" in request.prompt


def test_prompt_truncates_long_action_lists() -> None:
    run = forged_run()
    many = run.model_copy(update={"actions": run.actions * 40})
    prompt = build_forgery_prompt(many, ForgeryKind.STYLE_ONLY, seed=0)
    assert "more actions" in prompt


async def test_llm_forger_rejects_blank_answers() -> None:
    llm = FakeLLM({"ForgeryDraft": [{"answer": "   ", "reasoning": []}]})
    with pytest.raises(LLMError):
        await LLMForger(llm).forge(forged_run(), ForgeryKind.PROGRESS_FABRICATION, seed=0)
