from pathlib import Path

from pydantic import SecretStr

from evidence_judge.config import Settings
from evidence_judge.llm.anthropic_client import AnthropicClient
from evidence_judge.llm.cache import CachedClient
from evidence_judge.llm.concurrency import ConcurrencyLimitedClient
from evidence_judge.llm.factory import build_llm


def test_no_api_key_returns_none():
    assert build_llm(Settings.model_validate({"anthropic_api_key": None})) is None
    assert build_llm(Settings.model_validate({"anthropic_api_key": SecretStr("")})) is None


def test_builds_cached_limited_client_when_cache_dir_set(tmp_path: Path):
    settings = Settings.model_validate(
        {
            "anthropic_api_key": SecretStr("sk-test"),
            "cache_dir": tmp_path,
            "max_concurrent_llm_calls": 3,
        }
    )
    client = build_llm(settings)
    assert isinstance(client, CachedClient)
    assert isinstance(client.inner, ConcurrencyLimitedClient)
    assert client.inner.max_concurrent == 3
    assert isinstance(client.inner.inner, AnthropicClient)
    assert client.name == "cached:anthropic"


def test_builds_limited_client_without_cache():
    settings = Settings.model_validate(
        {"anthropic_api_key": SecretStr("sk-test"), "cache_dir": None}
    )
    client = build_llm(settings)
    assert isinstance(client, ConcurrencyLimitedClient)
    assert client.max_concurrent == 4
    assert isinstance(client.inner, AnthropicClient)


def test_client_has_no_lifetime_budget_unless_one_is_passed():
    settings = Settings.model_validate(
        {"anthropic_api_key": SecretStr("sk-test"), "cache_dir": None}
    )
    client = build_llm(settings)
    assert isinstance(client, ConcurrencyLimitedClient)
    assert isinstance(client.inner, AnthropicClient)
    assert client.inner.budget is None
