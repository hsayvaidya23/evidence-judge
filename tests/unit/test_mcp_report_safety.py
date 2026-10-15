"""judge_run's report paths: an existing file is replaced only when it is an earlier
evidence-judge report, unless the caller passes overwrite."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import evidence_judge.mcp.service as service
from evidence_judge.config import Settings
from evidence_judge.mcp import Roots, ServerConfig, ToolInputError
from evidence_judge.mcp.server import build_server
from evidence_judge.mcp.service import JudgeRequest, is_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def config(tmp_path: Path) -> ServerConfig:
    shutil.copytree(FIXTURES / "runs" / "forged_success", tmp_path / "run")
    return ServerConfig(
        roots=Roots.of([tmp_path]),
        settings=lambda: Settings(_env_file=None, anthropic_api_key=None, cache_dir=None),  # pyright: ignore[reportCallIssue]
    )


def _request(**extra: object) -> JudgeRequest:
    args: dict[str, object] = {
        "trace": "run/trace.zip",
        "answer_file": "run/answer.txt",
        "spec": "run/spec.yaml",
    }
    return JudgeRequest(**{**args, **extra})  # pyright: ignore[reportArgumentType]


async def _judge(config: ServerConfig, req: JudgeRequest) -> None:
    await service.judge(config, req, service.prepare_judge(config, req))


@pytest.mark.parametrize("field", ["json_report", "html_report"])
def test_a_file_that_is_not_a_report_is_never_overwritten(
    config: ServerConfig, tmp_path: Path, field: str
) -> None:
    victim = tmp_path / "run" / "spec.yaml"
    before = victim.read_bytes()
    with pytest.raises(ToolInputError, match=r"is not an evidence-judge \w+ report; refusing"):
        service.prepare_judge(config, _request(**{field: "run/spec.yaml"}))
    assert victim.read_bytes() == before


async def test_an_earlier_report_is_replaced(config: ServerConfig, tmp_path: Path) -> None:
    req = _request(json_report="v.json", html_report="v.html")
    await _judge(config, req)
    assert is_report(tmp_path / "v.json", "json")
    assert is_report(tmp_path / "v.html", "html")
    await _judge(config, req)
    assert is_report(tmp_path / "v.json", "json")


async def test_overwrite_replaces_any_file(config: ServerConfig, tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("keep me?")
    await _judge(config, _request(json_report="notes.txt", overwrite=True))
    assert is_report(tmp_path / "notes.txt", "json")


def test_is_report_rejects_lookalikes(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"outcome": "pass"}')
    (tmp_path / "a.html").write_text("<html>evidence-judge</html>")
    assert not is_report(tmp_path / "a.json", "json")
    assert not is_report(tmp_path / "a.html", "html")
    assert not is_report(tmp_path / "missing.json", "json")


def test_judge_run_is_annotated_destructive(config: ServerConfig) -> None:
    tools = {tool.name: tool for tool in build_server(config)._tool_manager.list_tools()}  # pyright: ignore[reportPrivateUsage]
    annotations = tools["judge_run"].annotations
    assert annotations is not None
    assert annotations.destructive_hint is True
    assert "overwrite" in tools["judge_run"].parameters["properties"]
