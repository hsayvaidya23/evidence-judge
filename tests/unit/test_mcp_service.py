"""Service-layer paths the MCP client tests do not reach: model use, key files, write errors."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

import evidence_judge.mcp.service as service
from evidence_judge.config import REDACTION_KEY_FILE, Settings
from evidence_judge.core.evidence import Run
from evidence_judge.llm.base import LLMConfigError, LLMError
from evidence_judge.mcp import Roots, ServerConfig, ToolInputError
from evidence_judge.mcp.service import JudgeRequest
from evidence_judge.redact import redactor_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, anthropic_api_key=None, **overrides)  # pyright: ignore[reportCallIssue]


@pytest.fixture
def config(tmp_path: Path) -> ServerConfig:
    shutil.copytree(FIXTURES / "runs" / "forged_success", tmp_path / "run")
    return ServerConfig(roots=Roots.of([tmp_path]), settings=lambda: _settings(cache_dir=None))


def _request(**extra: Any) -> JudgeRequest:
    args = {"trace": "run/trace.zip", "answer_file": "run/answer.txt", "spec": "run/spec.yaml"}
    return JudgeRequest(**{**args, **extra})


def test_a_persistent_redactor_uses_the_key_file(tmp_path: Path) -> None:
    settings = _settings(cache_dir=tmp_path)
    redactor_for(settings, persistent=True)
    assert (tmp_path / REDACTION_KEY_FILE).is_file()


async def test_a_corrupt_key_file_is_an_input_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(FIXTURES / "runs" / "forged_success", tmp_path / "run")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / REDACTION_KEY_FILE).write_text("zz", encoding="ascii")
    config = ServerConfig(
        roots=Roots.of([tmp_path]), settings=lambda: _settings(cache_dir=tmp_path / "cache")
    )
    monkeypatch.setattr(service.factory, "build_llm", lambda settings: object())
    req = _request(offline=False)
    prepared = service.prepare_judge(config, req)
    with pytest.raises(ToolInputError, match=r"redaction key: .* is corrupt"):
        await service.judge(config, req, prepared)


def test_an_unwritable_cache_falls_back_to_a_process_key(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    warnings: list[str] = []
    redactor = redactor_for(
        _settings(cache_dir=blocker / "cache"), persistent=True, warn=warnings.append
    )
    assert redactor.fingerprint("a") == redactor_for(
        _settings(cache_dir=None), persistent=False
    ).fingerprint("a")
    assert len(warnings) == 1 and "per-process key" in warnings[0]


class _FakeJudge:
    error: Exception | None = None

    def __init__(self, settings: Settings, llm: object) -> None:
        self.llm = llm

    async def judge(self, run: Run, options: Any) -> Any:
        assert self.error is not None
        raise self.error


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (LLMConfigError("bad key"), "offline=true to skip"),
        (LLMError("overloaded"), "LLM error: overloaded"),
    ],
)
async def test_model_errors_are_input_errors(
    config: ServerConfig, monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    _FakeJudge.error = error
    monkeypatch.setattr(service.engine, "Judge", _FakeJudge)
    monkeypatch.setattr(service.factory, "build_llm", lambda settings: object())
    req = _request(offline=False)
    prepared = service.prepare_judge(config, req)
    with pytest.raises(ToolInputError, match=message):
        await service.judge(config, req, prepared)


def test_a_failing_model_client_is_an_input_error(
    config: ServerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(settings: Settings) -> None:
        raise LLMError("no SDK")

    monkeypatch.setattr(service.factory, "build_llm", fail)
    with pytest.raises(ToolInputError, match="could not initialise"):
        service._llm(config, _settings(), offline=False)  # pyright: ignore[reportPrivateUsage]


async def test_report_write_errors_are_input_errors(
    config: ServerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(service, "write_json", fail)
    req = _request(json_report="v.json")
    prepared = service.prepare_judge(config, req)
    with pytest.raises(ToolInputError, match="cannot write report: Permission denied"):
        await service.judge(config, req, prepared)


def test_a_spec_that_is_not_utf8_is_an_input_error(config: ServerConfig) -> None:
    (config.roots.default / "bad.yaml").write_bytes(b"\xff\xfe")
    with pytest.raises(ToolInputError, match="cannot read spec"):
        service.prepare_judge(config, _request(spec="bad.yaml"))


def test_oversized_verdicts_are_refused(
    config: ServerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    (config.roots.default / "v.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(service, "MAX_VERDICT_BYTES", 1)
    with pytest.raises(ToolInputError, match="the limit is 1"):
        service.load_verdict(config, "v.json")


def test_sources_without_metadata_and_har_only_requests() -> None:
    run = Run(run_id="r", source="har", metadata={"har_only_requests": [{"id": "net#1"}]})
    sources, warnings = service._sources(run)  # pyright: ignore[reportPrivateUsage]
    assert [s.name for s in sources] == ["har"]
    assert warnings == [
        "1 request(s) appear only in the HAR: they can contradict claims but never prove them"
    ]
