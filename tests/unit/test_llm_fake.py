import pytest
from pydantic import BaseModel

from evidence_judge.llm.base import LLMError, LLMRequest
from evidence_judge.llm.fake import FakeLLM


class Answer(BaseModel):
    value: int


class Other(BaseModel):
    label: str


async def test_queued_responses_are_returned_in_order_per_schema():
    llm = FakeLLM({"Answer": [Answer(value=1), {"value": 2}]})
    llm.queue(Other, {"label": "x"})
    first, usage = await llm.generate(LLMRequest(system="s", prompt="p"), Answer)
    other, _ = await llm.generate(LLMRequest(system="s", prompt="p"), Other)
    second, _ = await llm.generate(LLMRequest(system="s", prompt="q"), Answer)
    assert (first.value, second.value, other.label) == (1, 2, "x")
    assert usage.llm_calls == 1
    assert [c.request.prompt for c in llm.calls] == ["p", "p", "q"]
    assert llm.pending(Answer) == 0


async def test_handler_is_used_when_queue_empty():
    llm = FakeLLM(handler=lambda request, schema: {"value": len(request.prompt)})
    result, _ = await llm.generate(LLMRequest(system="s", prompt="abcd"), Answer)
    assert result.value == 4


async def test_queued_exception_is_raised():
    llm = FakeLLM()
    llm.queue(Answer, LLMError("boom"))
    with pytest.raises(LLMError, match="boom"):
        await llm.generate(LLMRequest(system="s", prompt="p"), Answer)


async def test_missing_response_raises_llm_error():
    with pytest.raises(LLMError, match="no response queued"):
        await FakeLLM().generate(LLMRequest(system="s", prompt="p"), Answer)


async def test_invalid_mapping_raises_llm_error():
    llm = FakeLLM({"Answer": [{"value": "not-a-number"}]})
    with pytest.raises(LLMError, match="does not match"):
        await llm.generate(LLMRequest(system="s", prompt="p"), Answer)
