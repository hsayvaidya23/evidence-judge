"""`--llm` on `judge` and `bench`, and the CLI backends seen end to end through `judge`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import evidence_judge.llm.factory
from evidence_judge.cli import ExitCode, LLMChoice, app
from evidence_judge.config import LLM_BACKENDS, Settings
from evidence_judge.llm.base import LLMClient
from tests.unit.fake_cli import FakeCLI, jsonl

runner = CliRunner()
RUN = Path(__file__).resolve().parents[1] / "fixtures" / "runs" / "honest_success"
EMAIL = "alice.secret@example.com"


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("EVJ_LLM_BACKEND", "ANTHROPIC_API_KEY", "EVJ_REDACT", "EVJ_SCREENSHOTS_TO_LLM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EVJ_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EVJ_REDACTION_KEY", "test-key")
    monkeypatch.chdir(tmp_path)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _record_settings(monkeypatch: pytest.MonkeyPatch) -> list[Settings]:
    seen: list[Settings] = []

    def build_llm(settings: Settings, **_: object) -> LLMClient | None:
        seen.append(settings)
        return None

    monkeypatch.setattr(evidence_judge.llm.factory, "build_llm", build_llm)
    return seen


def test_llm_choices_match_the_settings_backends():
    assert tuple(choice.value for choice in LLMChoice) == LLM_BACKENDS


@pytest.mark.parametrize("backend", LLM_BACKENDS)
def test_judge_llm_overrides_the_configured_backend(monkeypatch: pytest.MonkeyPatch, backend: str):
    monkeypatch.setenv("EVJ_LLM_BACKEND", "codex" if backend != "codex" else "gemini")
    seen = _record_settings(monkeypatch)
    result = runner.invoke(app, ["judge", "--har", str(RUN / "run.har"), "--llm", backend, "-q"])
    assert result.exit_code == ExitCode.INTERNAL, result.output
    assert [s.llm_backend for s in seen] == [backend]


def test_judge_uses_the_configured_backend_without_llm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EVJ_LLM_BACKEND", "claude-code")
    seen = _record_settings(monkeypatch)
    runner.invoke(app, ["judge", "--har", str(RUN / "run.har"), "-q"])
    assert [s.llm_backend for s in seen] == ["claude-code"]


def test_offline_wins_over_llm(monkeypatch: pytest.MonkeyPatch):
    seen = _record_settings(monkeypatch)
    result = runner.invoke(
        app, ["judge", "--har", str(RUN / "run.har"), "--llm", "codex", "--offline", "-q"]
    )
    assert result.exit_code in (0, 1, 2)
    assert seen == []


def test_an_unknown_backend_is_a_usage_error():
    result = runner.invoke(app, ["judge", "--har", str(RUN / "run.har"), "--llm", "gpt"])
    assert result.exit_code == ExitCode.USAGE


def test_an_unknown_configured_backend_is_a_usage_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EVJ_LLM_BACKEND", "gpt")
    result = runner.invoke(app, ["judge", "--har", str(RUN / "run.har")])
    assert result.exit_code == ExitCode.USAGE
    text = " ".join(result.output.split())
    assert "EVJ_LLM_BACKEND: must be one of anthropic, claude-code, codex, gemini" in text
    assert "pydantic" not in text and "'gpt'" not in text


def test_llm_anthropic_without_a_key_exits_internal_naming_the_key():
    result = runner.invoke(app, ["judge", "--har", str(RUN / "run.har"), "--llm", "anthropic"])
    assert result.exit_code == ExitCode.INTERNAL, result.output
    assert "ANTHROPIC_API_KEY" in _flat(result.output)
    assert "judging offline" not in result.output
    offline = runner.invoke(
        app, ["judge", "--har", str(RUN / "run.har"), "--llm", "anthropic", "--offline", "-q"]
    )
    assert offline.exit_code in (0, 1, 2), offline.output


def test_no_llm_option_without_a_key_still_judges_offline():
    result = runner.invoke(app, ["judge", "--har", str(RUN / "run.har")])
    assert result.exit_code in (0, 1, 2), result.output
    assert "judging offline" in result.output


def test_a_missing_cli_exits_internal_with_install_advice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("PATH", str(tmp_path))
    result = runner.invoke(app, ["judge", "--har", str(RUN / "run.har"), "--llm", "claude-code"])
    assert result.exit_code == ExitCode.INTERNAL
    assert "needs the `claude` CLI on PATH" in _flat(result.output)


def test_bench_llm_selects_the_backend_for_online_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    seen = _record_settings(monkeypatch)
    runs = tmp_path / "runs"
    (runs / "honest_success").mkdir(parents=True)
    for name in ("run.har", "answer.txt", "expected.json"):
        (runs / "honest_success" / name).write_bytes((RUN / name).read_bytes())
    result = runner.invoke(app, ["bench", str(runs), "--online", "--llm", "gemini", "-q"])
    assert result.exit_code == ExitCode.INTERNAL
    assert "ANTHROPIC_API_KEY, which is not set" in _flat(result.output)
    assert [s.llm_backend for s in seen] == ["gemini"]


def test_bench_online_without_an_api_key_exits_internal_naming_the_key(tmp_path: Path):
    runs = tmp_path / "runs"
    (runs / "honest_success").mkdir(parents=True)
    for name in ("run.har", "answer.txt", "expected.json"):
        (runs / "honest_success" / name).write_bytes((RUN / name).read_bytes())
    result = runner.invoke(app, ["bench", str(runs), "--online", "-q"])
    assert result.exit_code == ExitCode.INTERNAL, result.output
    assert "ANTHROPIC_API_KEY, which is not set" in _flat(result.output)


def test_judge_with_a_cli_backend_sends_only_redacted_agent_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = FakeCLI(tmp_path / "cli", monkeypatch)
    fake.install("codex")
    fake.replies(
        {
            "stdout": jsonl(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps({"claims": []})},
                },
                {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 1}},
            )
        }
    )
    answer = tmp_path / "answer.txt"
    answer.write_text(f"I bought the Red Kettle and emailed the receipt to {EMAIL}.")
    out = tmp_path / "verdict.json"

    result = runner.invoke(
        app,
        [
            "judge",
            "--har",
            str(RUN / "run.har"),
            "--answer-file",
            str(answer),
            "--llm",
            "codex",
            "--out",
            str(out),
            "-q",
        ],
    )

    assert result.exit_code in (0, 1, 2), result.output
    assert fake.calls, "the codex CLI was never called"
    for call in fake.calls:
        assert EMAIL not in call.stdin
        assert all(EMAIL not in arg for arg in call.argv)
        assert "Red Kettle" in call.stdin
    verdict = json.loads(out.read_text())
    assert verdict["usage"]["llm_calls"] >= 1
