import asyncio
from pathlib import Path

from pydantic import BaseModel

from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import Image, LLMRequest, Tier
from evidence_judge.llm.cache import CachedClient, cache_key
from evidence_judge.llm.fake import FakeLLM


class Answer(BaseModel):
    value: int


class Renamed(BaseModel):
    other: str


def _fake(*values: int) -> FakeLLM:
    return FakeLLM(
        {"Answer": [{"value": v} for v in values]},
        usage=Usage(llm_calls=1, input_tokens=10, cost_usd=0.5),
    )


async def test_miss_then_hit_reports_zero_cost(tmp_path: Path):
    inner = _fake(7)
    client = CachedClient(inner, tmp_path)
    request = LLMRequest(system="s", prompt="p")
    first, first_usage = await client.generate(request, Answer)
    second, second_usage = await client.generate(request, Answer)
    assert first == second == Answer(value=7)
    assert first_usage.cost_usd == 0.5
    assert second_usage == Usage()
    assert (client.hits, client.misses) == (1, 1)
    assert len(inner.calls) == 1


async def test_cache_survives_new_client_instance(tmp_path: Path):
    request = LLMRequest(system="s", prompt="p")
    await CachedClient(_fake(1), tmp_path).generate(request, Answer)
    inner = _fake()
    result, _ = await CachedClient(inner, tmp_path).generate(request, Answer)
    assert result.value == 1
    assert inner.calls == []


async def test_key_changes_with_prompt_image_tier_effort_and_schema(tmp_path: Path):
    base = LLMRequest(system="s", prompt="p")
    keys = {
        cache_key("m", base, "Answer", "{}"),
        cache_key("m", LLMRequest(system="s", prompt="q"), "Answer", "{}"),
        cache_key("m", LLMRequest(system="s", prompt="p", images=[Image(b"a")]), "Answer", "{}"),
        cache_key("m", LLMRequest(system="s", prompt="p", images=[Image(b"b")]), "Answer", "{}"),
        cache_key("m2", base, "Answer", "{}"),
        cache_key("m", base, "Other", "{}"),
        cache_key("m", base, "Answer", '{"x": 1}'),
        cache_key("m", LLMRequest(system="t", prompt="p"), "Answer", "{}"),
        cache_key("m", LLMRequest(system="s", prompt="p", effort="low"), "Answer", "{}"),
        cache_key("m", LLMRequest(system="s", prompt="p", max_output_tokens=99), "Answer", "{}"),
    }
    assert len(keys) == 10

    inner = _fake(1, 2)
    client = CachedClient(inner, tmp_path, model_for=lambda tier: f"model-{tier}")
    await client.generate(LLMRequest(system="s", prompt="p", tier=Tier.FAST), Answer)
    await client.generate(LLMRequest(system="s", prompt="p", tier=Tier.STRONG), Answer)
    assert len(inner.calls) == 2


def _corrupt_all(root: Path) -> None:
    for path in root.rglob("*.json"):
        path.write_text("{not json", encoding="utf-8")


async def test_corrupt_entry_is_treated_as_miss(tmp_path: Path):
    request = LLMRequest(system="s", prompt="p")
    await CachedClient(_fake(1), tmp_path).generate(request, Answer)
    _corrupt_all(tmp_path)
    inner = _fake(2)
    result, _ = await CachedClient(inner, tmp_path).generate(request, Answer)
    assert result.value == 2
    assert len(inner.calls) == 1


async def test_concurrent_identical_requests_share_one_upstream_call(tmp_path: Path):
    inner = _fake(5)
    client = CachedClient(inner, tmp_path)
    request = LLMRequest(system="s", prompt="p")
    results = await asyncio.gather(*(client.generate(request, Answer) for _ in range(5)))
    assert {r.value for r, _ in results} == {5}
    assert len(inner.calls) == 1
    assert client.hits == 4


async def test_unwritable_cache_dir_does_not_fail_the_call(tmp_path: Path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    client = CachedClient(_fake(3), blocker / "cache")
    result, _ = await client.generate(LLMRequest(system="s", prompt="p"), Answer)
    assert result.value == 3
