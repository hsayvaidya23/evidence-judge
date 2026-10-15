"""The claude-code, codex and gemini LLM backends, driven against a fake CLI on PATH."""

from __future__ import annotations

import base64
import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from evidence_judge.checks.screenshot import ScreenshotChecker
from evidence_judge.config import Settings
from evidence_judge.core.predicates import Scope, ScreenshotPredicate
from evidence_judge.core.verdict import Support, Usage
from evidence_judge.llm.base import (
    BudgetExceededError,
    Image,
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMRequest,
    Tier,
)
from evidence_judge.llm.budget import Budget, run_budget
from evidence_judge.llm.claude_code import LOCKDOWN_FLAGS as CLAUDE_LOCKDOWN
from evidence_judge.llm.claude_code import ClaudeCodeClient
from evidence_judge.llm.cli_client import CLIClient
from evidence_judge.llm.codex import DISABLED_FEATURES, CodexClient
from evidence_judge.llm.gemini import DENY_ALL_POLICY, GeminiClient
from tests.unit.conftest import ctx, honest_run
from tests.unit.fake_cli import FakeCLI, jsonl


class Capital(BaseModel):
    country: str
    capital: str = Field(min_length=2)


GOOD = {"country": "France", "capital": "Paris"}
BAD = {"country": "France", "capital": "P"}
SECRET_PROMPT = "What is the capital of France? marker-7f3a"
PNG = b"\x89PNG\r\n\x1a\nfake"


def _same(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _removed(directory: str) -> bool:
    return not Path(directory).exists()


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCLI:
    return FakeCLI(tmp_path, monkeypatch)


def _settings(backend: Literal["claude-code", "codex", "gemini"], **overrides: Any) -> Settings:
    return Settings.model_validate(
        {"llm_backend": backend, "cache_dir": None, "cli_timeout_s": 10, **overrides}
    )


def _request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {"system": "You answer geography questions.", "prompt": SECRET_PROMPT}
    fields.update(overrides)
    return LLMRequest(**fields)


def _claude_result(**fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "result": json.dumps(GOOD),
        "structured_output": GOOD,
        "total_cost_usd": 0.0012,
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 5,
            "output_tokens": 20,
        },
    }
    result.update(fields)
    return result


def _claude_reply(**fields: Any) -> dict[str, Any]:
    return {
        "stdout": jsonl(
            {"type": "system", "subtype": "init", "tools": ["StructuredOutput"], "mcp_servers": []},
            {"type": "assistant", "message": {"content": []}},
            _claude_result(**fields),
        )
    }


def _codex_reply(text: str, **usage: int) -> dict[str, Any]:
    return {
        "stdout": jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "hm"}},
            {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": usage.get("input_tokens", 300),
                    "cached_input_tokens": 0,
                    "output_tokens": usage.get("output_tokens", 7),
                    "reasoning_output_tokens": 2,
                },
            },
        ),
        "stderr": "2026-10-15T10:00:00Z ERROR codex_core: failed to load skill x\n",
    }


def _gemini_reply(
    response: str | None = None, *, exit_code: int = 0, **fields: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": "s1",
        "stats": {
            "models": {
                "gemini-flash": {
                    "tokens": {"prompt": 40, "candidates": 9, "thoughts": 3, "total": 52}
                }
            }
        },
    }
    if response is not None:
        body["response"] = response
    body.update(fields)
    return {"stdout": json.dumps(body), "exit": exit_code}


# claude-code


async def test_claude_code_returns_structured_output_with_usage_and_cost(fake: FakeCLI):
    fake.install("claude")
    fake.replies(_claude_reply())
    client = ClaudeCodeClient(_settings("claude-code"))

    result, usage = await client.generate(_request(effort="low"), Capital)

    assert result == Capital(**GOOD)
    assert usage.llm_calls == 1
    assert usage.input_tokens == 115
    assert usage.output_tokens == 20
    assert usage.cost_usd == pytest.approx(0.0012)
    assert client.totals.cost_usd == pytest.approx(0.0012)
    [call] = fake.calls
    assert call.argv[:7] == [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
    ]
    for flag in CLAUDE_LOCKDOWN:
        assert flag in call.argv
    assert "--disallowedTools" not in call.argv
    assert call.flag("--tools") == ""
    assert call.flag("--max-turns") == "2"
    assert call.flag("--model") == "claude-haiku-5-5"
    assert call.flag("--effort") == "low"
    assert call.flag("--system-prompt") == "You answer geography questions."
    schema = json.loads(call.flag("--json-schema"))
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["country", "capital"]
    assert all(SECRET_PROMPT not in arg for arg in call.argv)
    message = json.loads(call.stdin)
    assert message["type"] == "user"
    assert message["message"]["content"] == [{"type": "text", "text": SECRET_PROMPT}]
    assert call.files == {}
    assert _removed(call.cwd)
    assert call.env["NO_COLOR"] == "1"


async def test_claude_code_sends_screenshots_as_base64_image_blocks(fake: FakeCLI):
    fake.install("claude")
    fake.replies(_claude_reply())
    client = ClaudeCodeClient(_settings("claude-code", cli_model_strong="opus"))

    await client.generate(
        _request(tier=Tier.STRONG, images=[Image(data=PNG, mime_type="image/png")]), Capital
    )

    [call] = fake.calls
    assert call.flag("--model") == "opus"
    image, text = json.loads(call.stdin)["message"]["content"]
    assert image == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(PNG).decode("ascii"),
        },
    }
    assert text["type"] == "text"
    assert call.files == {}


async def test_claude_code_rejects_unsupported_image_types(fake: FakeCLI):
    fake.install("claude")
    client = ClaudeCodeClient(_settings("claude-code"))
    with pytest.raises(LLMError, match="Unsupported image type"):
        await client.generate(_request(images=[Image(data=b"x", mime_type="image/bmp")]), Capital)
    assert fake.calls == []


async def test_claude_code_repairs_a_schema_invalid_reply_once(fake: FakeCLI):
    fake.install("claude")
    fake.replies(_claude_reply(structured_output=BAD), _claude_reply())
    client = ClaudeCodeClient(_settings("claude-code"))

    result, usage = await client.generate(_request(), Capital)

    assert result.capital == "Paris"
    assert usage.llm_calls == 2
    assert usage.cost_usd == pytest.approx(0.0024)
    first, second = fake.calls
    assert "previous_attempt_error" not in first.stdin
    repaired = json.loads(second.stdin)["message"]["content"][-1]["text"]
    assert "<previous_attempt_error>" in repaired
    assert "capital" in repaired


async def test_claude_code_gives_up_after_one_repair(fake: FakeCLI):
    fake.install("claude")
    fake.replies(_claude_reply(structured_output=BAD))
    client = ClaudeCodeClient(_settings("claude-code"))
    with pytest.raises(LLMError, match="did not satisfy Capital after a retry"):
        await client.generate(_request(), Capital)
    assert len(fake.calls) == 2


@pytest.mark.parametrize("subtype", ["error_max_structured_output_retries", "error_max_turns"])
async def test_claude_code_retries_when_it_gave_up_on_the_schema(fake: FakeCLI, subtype: str):
    fake.install("claude")
    gave_up = _claude_reply(
        subtype=subtype, is_error=True, result=None, structured_output=None, errors=["nope"]
    )
    fake.replies(gave_up, _claude_reply())
    client = ClaudeCodeClient(_settings("claude-code"))

    result, usage = await client.generate(_request(), Capital)

    assert result.country == "France"
    assert usage.llm_calls == 2
    assert (
        "gave up on the schema" in json.loads(fake.calls[1].stdin)["message"]["content"][-1]["text"]
    )


async def test_claude_code_falls_back_to_result_text_without_structured_output(fake: FakeCLI):
    fake.install("claude")
    fake.replies(_claude_reply(structured_output=None, result=f"```json\n{json.dumps(GOOD)}\n```"))
    result, _ = await ClaudeCodeClient(_settings("claude-code")).generate(_request(), Capital)
    assert result.capital == "Paris"


async def test_claude_code_not_logged_in_is_an_auth_error(fake: FakeCLI):
    fake.install("claude")
    fake.replies(
        {
            "stdout": jsonl(
                {"type": "system", "subtype": "init"},
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "Not logged in · Please run /login",
                    "total_cost_usd": 0,
                },
            ),
            "exit": 1,
        }
    )
    with pytest.raises(LLMAuthError, match="Not logged in"):
        await ClaudeCodeClient(_settings("claude-code")).generate(_request(), Capital)
    assert len(fake.calls) == 1


async def test_claude_code_unknown_model_is_a_config_error(fake: FakeCLI):
    fake.install("claude")
    fake.replies(
        _claude_reply(
            subtype="error_during_execution",
            is_error=True,
            result="There's an issue with the selected model (claude-nope). It may not exist",
            errors=["model claude-nope not found"],
        )
    )
    client = ClaudeCodeClient(_settings("claude-code", cli_model_fast="claude-nope"))
    with run_budget(Budget(5.0)), pytest.raises(LLMConfigError, match="rejected the model"):
        await client.generate(_request(), Capital)


async def test_claude_code_budget_stop_raises_budget_exceeded(fake: FakeCLI):
    fake.install("claude")
    fake.replies(_claude_reply(subtype="error_max_budget_usd", is_error=True, total_cost_usd=0.3))
    with pytest.raises(BudgetExceededError, match="stopped at the LLM budget"):
        await ClaudeCodeClient(_settings("claude-code")).generate(_request(), Capital)


async def test_claude_code_charges_the_run_budget_and_passes_the_headroom(fake: FakeCLI):
    fake.install("claude")
    fake.replies(_claude_reply(total_cost_usd=0.01))
    client = ClaudeCodeClient(_settings("claude-code"))
    budget = Budget(0.5)
    with run_budget(budget):
        await client.generate(_request(), Capital)
    assert budget.spent_usd == pytest.approx(0.01)
    assert budget.remaining_usd == pytest.approx(0.49)
    assert float(fake.calls[0].flag("--max-budget-usd")) == pytest.approx(0.5)


async def test_claude_code_refuses_to_start_when_the_budget_is_spent(fake: FakeCLI):
    fake.install("claude")
    budget = Budget(0.01)
    budget.settle(0.0, 0.01)
    with run_budget(budget), pytest.raises(BudgetExceededError):
        await ClaudeCodeClient(_settings("claude-code")).generate(_request(), Capital)
    assert fake.calls == []


async def test_claude_code_crash_without_result_reports_stderr(fake: FakeCLI):
    fake.install("claude")
    fake.replies({"stdout": "", "stderr": "Error: something broke\n", "exit": 2})
    with pytest.raises(LLMError, match=r"claude-code failed \(exit 2\): Error: something broke"):
        await ClaudeCodeClient(_settings("claude-code")).generate(_request(), Capital)


# codex


async def test_codex_runs_read_only_with_tools_off_and_a_strict_schema(fake: FakeCLI):
    fake.install("codex")
    fake.replies(_codex_reply(json.dumps(GOOD)))
    client = CodexClient(_settings("codex", cli_model_fast="gpt-test"))

    result, usage = await client.generate(_request(effort="low"), Capital)

    assert result == Capital(**GOOD)
    assert usage.llm_calls == 1
    assert usage.input_tokens == 300
    assert usage.output_tokens == 7
    assert usage.cost_usd == 0
    [call] = fake.calls
    assert call.argv[:2] == ["exec", "--json"]
    assert call.argv[-1] == "-"
    assert call.flag("--sandbox") == "read-only"
    for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules"):
        assert flag in call.argv
    overrides = call.values("-c")
    assert 'approval_policy="never"' in overrides
    assert 'web_search="disabled"' in overrides
    for feature in DISABLED_FEATURES:
        assert f"features.{feature}=false" in overrides
    assert 'model_reasoning_effort="low"' in overrides
    [instructions] = [o for o in overrides if o.startswith("developer_instructions=")]
    parsed = tomllib.loads(instructions)
    assert parsed["developer_instructions"] == "You answer geography questions."
    assert call.flag("--model") == "gpt-test"
    assert _same(call.flag("--cd"), call.cwd)
    assert call.stdin == SECRET_PROMPT
    assert all(SECRET_PROMPT not in arg for arg in call.argv)
    schema = json.loads(call.files["output-schema.json"])
    assert schema["required"] == ["country", "capital"]
    assert schema["additionalProperties"] is False
    assert "minLength" not in schema["properties"]["capital"]
    assert schema["properties"]["capital"]["description"] == "minLength: 2"
    assert _removed(call.cwd)


async def test_codex_attaches_screenshots_as_image_files(fake: FakeCLI):
    fake.install("codex")
    fake.replies(_codex_reply(json.dumps(GOOD)))
    client = CodexClient(_settings("codex"))
    images = [Image(data=PNG, mime_type="image/png"), Image(data=b"jpg", mime_type="image/jpeg")]

    await client.generate(_request(tier=Tier.STRONG, images=images), Capital)

    [call] = fake.calls
    assert "--model" not in call.argv
    paths = call.values("--image")
    assert [Path(p).name for p in paths] == ["screenshot-0.png", "screenshot-1.jpg"]
    assert all(_same(str(Path(p).parent), call.cwd) for p in paths)
    assert call.files["screenshot-1.jpg"] == "jpg"


async def test_codex_rejects_unsupported_image_types(fake: FakeCLI):
    fake.install("codex")
    with pytest.raises(LLMError, match="Unsupported image type"):
        await CodexClient(_settings("codex")).generate(
            _request(images=[Image(data=b"x", mime_type="image/tiff")]), Capital
        )
    assert fake.calls == []


async def test_codex_repairs_a_schema_invalid_reply_once(fake: FakeCLI):
    fake.install("codex")
    fake.replies(_codex_reply("not json at all"), _codex_reply(json.dumps(GOOD)))
    result, usage = await CodexClient(_settings("codex")).generate(_request(), Capital)
    assert result.capital == "Paris"
    assert usage.llm_calls == 2
    assert "<previous_attempt_error>" in fake.calls[1].stdin


async def test_codex_401_is_an_auth_error(fake: FakeCLI):
    fake.install("codex")
    message = "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
    fake.replies(
        {
            "stdout": jsonl(
                {"type": "thread.started", "thread_id": "t"},
                {"type": "error", "message": f"Reconnecting... 1/5 ({message})"},
                {"type": "turn.failed", "error": {"message": message}},
            ),
            "exit": 1,
        }
    )
    with pytest.raises(LLMAuthError, match="codex login"):
        await CodexClient(_settings("codex")).generate(_request(), Capital)


async def test_codex_unknown_model_is_a_config_error(fake: FakeCLI):
    fake.install("codex")
    fake.replies(
        {
            "stdout": jsonl(
                {"type": "turn.failed", "error": {"message": "The model `gpt-nope` does not exist"}}
            ),
            "exit": 1,
        }
    )
    with pytest.raises(LLMConfigError, match="rejected the model"):
        await CodexClient(_settings("codex")).generate(_request(), Capital)


async def test_codex_non_zero_exit_without_events_is_an_llm_error(fake: FakeCLI):
    fake.install("codex")
    fake.replies({"stdout": "", "stderr": "Error: stream disconnected\n", "exit": 1})
    with pytest.raises(LLMError, match=r"codex failed \(exit 1\)") as info:
        await CodexClient(_settings("codex")).generate(_request(), Capital)
    assert not isinstance(info.value, LLMConfigError)


# gemini


async def test_gemini_puts_the_schema_in_the_system_prompt_and_denies_every_tool(fake: FakeCLI):
    fake.install("gemini")
    fake.replies(_gemini_reply(f"```json\n{json.dumps(GOOD)}\n```"))
    client = GeminiClient(_settings("gemini", cli_model_fast="gemini-flash"))

    result, usage = await client.generate(_request(), Capital)

    assert result == Capital(**GOOD)
    assert usage.input_tokens == 40
    assert usage.output_tokens == 12
    assert usage.cost_usd == 0
    [call] = fake.calls
    assert call.flag("--output-format") == "json"
    assert call.flag("--approval-mode") == "default"
    assert "--skip-trust" in call.argv
    assert call.flag("--model") == "gemini-flash"
    assert call.stdin == SECRET_PROMPT
    assert all(SECRET_PROMPT not in arg for arg in call.argv)
    policy = tomllib.loads(call.files["deny-all-tools.toml"])
    assert policy["rule"] == [tomllib.loads(DENY_ALL_POLICY)["rule"][0]]
    assert policy["rule"][0]["toolName"] == "*"
    assert policy["rule"][0]["decision"] == "deny"
    assert Path(call.flag("--policy")).name == "deny-all-tools.toml"
    settings = json.loads(call.files[".gemini/settings.json"])
    assert settings["tools"]["core"] == []
    assert settings["hooksConfig"]["enabled"] is False
    assert Path(call.env["GEMINI_SYSTEM_MD"] or "").name == "system.md"
    system = call.files["system.md"]
    assert system.startswith("You answer geography questions.")
    assert '"capital"' in system
    assert "You have no tools" in system


async def test_gemini_repairs_an_invalid_reply_once(fake: FakeCLI):
    fake.install("gemini")
    fake.replies(_gemini_reply("Sure! Paris."), _gemini_reply(json.dumps(GOOD)))
    result, usage = await GeminiClient(_settings("gemini")).generate(_request(), Capital)
    assert result.capital == "Paris"
    assert usage.llm_calls == 2
    assert "<previous_attempt_error>" in fake.calls[1].stdin


@pytest.mark.parametrize(
    ("exit_code", "error"),
    [(41, LLMAuthError), (42, LLMConfigError), (44, LLMConfigError), (52, LLMConfigError)],
)
async def test_gemini_fatal_exit_codes_are_configuration_errors(
    fake: FakeCLI, exit_code: int, error: type[LLMError]
):
    fake.install("gemini")
    fake.replies(
        _gemini_reply(exit_code=exit_code, error={"type": "Fatal", "message": "cannot go on"})
    )
    with pytest.raises(error, match="cannot go on"):
        await GeminiClient(_settings("gemini")).generate(_request(), Capital)


async def test_gemini_api_error_object_is_an_llm_error(fake: FakeCLI):
    fake.install("gemini")
    fake.replies(_gemini_reply(exit_code=1, error={"type": "ApiError", "message": "quota hit"}))
    with pytest.raises(LLMError, match="quota hit") as info:
        await GeminiClient(_settings("gemini")).generate(_request(), Capital)
    assert not isinstance(info.value, LLMConfigError)


async def test_gemini_unparseable_output_is_an_llm_error(fake: FakeCLI):
    fake.install("gemini")
    fake.replies({"stdout": "Loaded cached credentials.\n", "stderr": "boom", "exit": 1})
    with pytest.raises(LLMError, match="boom"):
        await GeminiClient(_settings("gemini")).generate(_request(), Capital)


async def test_gemini_cannot_send_images_and_never_starts_the_cli(fake: FakeCLI):
    fake.install("gemini")
    client = GeminiClient(_settings("gemini"))
    with pytest.raises(LLMError, match="screenshot checks are unavailable") as info:
        await client.generate(_request(images=[Image(data=PNG, mime_type="image/png")]), Capital)
    assert not isinstance(info.value, LLMConfigError)
    assert fake.calls == []


async def test_screenshot_check_reports_no_evidence_on_a_backend_without_images(fake: FakeCLI):
    fake.install("gemini")
    context = ctx(honest_run(), GeminiClient(_settings("gemini")))
    predicate = ScreenshotPredicate(question="Is the order confirmed?", scope=Scope.FINAL)

    result = await ScreenshotChecker().evaluate(predicate, context)

    assert result.status is Support.NO_EVIDENCE
    assert "screenshot checks are unavailable" in result.reason
    assert fake.calls == []


# shared behaviour


@pytest.mark.parametrize(
    ("client_type", "name"),
    [(ClaudeCodeClient, "claude"), (CodexClient, "codex"), (GeminiClient, "gemini")],
)
def test_a_missing_cli_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client_type: type[CLIClient], name: str
):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(LLMConfigError, match=f"needs the `{name}` CLI on PATH"):
        client_type(_settings("codex"))


async def test_a_cli_that_hangs_is_killed_at_the_timeout(fake: FakeCLI):
    fake.install("codex")
    fake.replies({"stdout": "", "sleep": 30})
    client = CodexClient(_settings("codex", cli_timeout_s=0.5))
    with pytest.raises(LLMError, match=r"did not answer within 0.5s \(EVJ_CLI_TIMEOUT_S\)") as info:
        await client.generate(_request(), Capital)
    assert not isinstance(info.value, LLMConfigError)


async def test_a_cli_that_cannot_be_executed_is_a_config_error(fake: FakeCLI):
    path = fake.install("codex")
    client = CodexClient(_settings("codex"))
    path.chmod(0o600)
    with pytest.raises(LLMConfigError, match="could not start"):
        await client.generate(_request(), Capital)


def test_cache_model_names_the_backend_and_model(fake: FakeCLI):
    fake.install("codex")
    fake.install("claude")
    codex = CodexClient(_settings("codex", cli_model_strong="gpt-big"))
    assert codex.cache_model(Tier.FAST) == "codex:default"
    assert codex.cache_model(Tier.STRONG) == "codex:gpt-big"
    claude = ClaudeCodeClient(_settings("claude-code"))
    assert claude.cache_model(Tier.STRONG) == "claude-code:claude-sonnet-5-5"


async def test_usage_totals_accumulate_across_calls(fake: FakeCLI):
    fake.install("codex")
    fake.replies(_codex_reply(json.dumps(GOOD), input_tokens=5, output_tokens=1))
    client = CodexClient(_settings("codex"))
    await client.generate(_request(), Capital)
    await client.generate(_request(), Capital)
    assert client.totals.model_dump(exclude={"latency_ms"}) == Usage(
        llm_calls=2, input_tokens=10, output_tokens=2
    ).model_dump(exclude={"latency_ms"})
