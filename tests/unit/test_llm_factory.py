from pathlib import Path

import pytest
from pydantic import SecretStr

from evidence_judge.checks.screenshot import ScreenshotAnswer
from evidence_judge.config import Settings
from evidence_judge.llm.anthropic_client import AnthropicClient
from evidence_judge.llm.base import LLMConfigError, LLMRequest
from evidence_judge.llm.budget import Budget
from evidence_judge.llm.cache import CachedClient
from evidence_judge.llm.claude_code import ClaudeCodeClient
from evidence_judge.llm.cli_client import CLIClient
from evidence_judge.llm.codex import CodexClient
from evidence_judge.llm.concurrency import ConcurrencyLimitedClient
from evidence_judge.llm.factory import build_llm
from evidence_judge.llm.gemini import GeminiClient
from tests.unit.fake_cli import FakeCLI, jsonl


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


@pytest.mark.parametrize(
    ("backend", "executable", "client_type"),
    [
        ("claude-code", "claude", ClaudeCodeClient),
        ("codex", "codex", CodexClient),
        ("gemini", "gemini", GeminiClient),
    ],
)
def test_builds_a_cached_limited_cli_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    executable: str,
    client_type: type[CLIClient],
):
    FakeCLI(tmp_path / "cli", monkeypatch).install(executable)
    settings = Settings.model_validate(
        {"llm_backend": backend, "anthropic_api_key": None, "cache_dir": tmp_path / "cache"}
    )
    budget = Budget(1.0)
    client = build_llm(settings, budget=budget)
    assert isinstance(client, CachedClient)
    assert client.name == f"cached:{backend}"
    assert isinstance(client.inner, ConcurrencyLimitedClient)
    inner = client.inner.inner
    assert isinstance(inner, client_type)
    assert inner.budget is budget


def test_a_cli_backend_does_not_need_an_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    FakeCLI(tmp_path, monkeypatch).install("codex")
    settings = Settings.model_validate(
        {"llm_backend": "codex", "anthropic_api_key": None, "cache_dir": None}
    )
    client = build_llm(settings)
    assert isinstance(client, ConcurrencyLimitedClient)
    assert isinstance(client.inner, CodexClient)


def test_a_cli_backend_whose_cli_is_missing_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", str(tmp_path))
    settings = Settings.model_validate({"llm_backend": "gemini", "cache_dir": None})
    with pytest.raises(LLMConfigError, match="gemini"):
        build_llm(settings)


async def test_cli_cache_entries_are_keyed_by_backend_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = FakeCLI(tmp_path / "cli", monkeypatch)
    fake.install("codex")
    answer = '{"answer": "yes", "reason": "shown"}'
    fake.replies(
        {
            "stdout": jsonl(
                {"type": "item.completed", "item": {"type": "agent_message", "text": answer}}
            )
        }
    )
    settings = Settings.model_validate(
        {"llm_backend": "codex", "cache_dir": tmp_path / "cache", "cli_model_fast": "m1"}
    )
    client = build_llm(settings)
    assert isinstance(client, CachedClient)
    request = LLMRequest(system="s", prompt="p")

    first, _ = await client.generate(request, ScreenshotAnswer)
    second, usage = await client.generate(request, ScreenshotAnswer)

    assert first == second
    assert usage.llm_calls == 0
    assert len(fake.calls) == 1
    other = build_llm(settings.model_copy(update={"cli_model_fast": "m2"}))
    assert other is not None
    await other.generate(request, ScreenshotAnswer)
    assert len(fake.calls) == 2
