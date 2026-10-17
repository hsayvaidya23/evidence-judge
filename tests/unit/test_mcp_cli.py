from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from typer.testing import CliRunner

import evidence_judge.mcp
from evidence_judge.cli import ExitCode, app
from evidence_judge.mcp import Roots, ServerConfig
from evidence_judge.mcp import server as server_module

runner = CliRunner()


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def serve(config: ServerConfig, *, transport: str, port: int) -> None:
        calls.append({"config": config, "transport": transport, "port": port})

    monkeypatch.setattr(evidence_judge.mcp, "serve", serve)
    return calls


def test_mcp_defaults_to_stdio_in_the_working_directory(
    served: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 0, result.output
    [call] = served
    assert call["transport"] == "stdio"
    assert call["config"].roots.dirs == (tmp_path.resolve(),)
    assert call["config"].allow_llm is True


def test_mcp_options(served: list[dict[str, Any]], tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    result = runner.invoke(
        app,
        [
            "mcp",
            "--root",
            str(tmp_path / "a"),
            "--root",
            str(tmp_path / "b"),
            "--http",
            "--port",
            "9123",
            "--offline-only",
        ],
    )
    assert result.exit_code == 0, result.output
    [call] = served
    assert call["transport"] == "streamable-http"
    assert call["port"] == 9123
    assert [d.name for d in call["config"].roots.dirs] == ["a", "b"]
    assert call["config"].allow_llm is False


@pytest.mark.parametrize("args", [["--root", "/no/such/dir"], ["--port", "0"]])
def test_mcp_bad_options_are_usage_errors(served: list[dict[str, Any]], args: list[str]) -> None:
    result = runner.invoke(app, ["mcp", *args])
    assert result.exit_code == ExitCode.USAGE
    assert served == []


def test_mcp_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "evidence_judge.mcp", None)
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == ExitCode.USAGE
    text = " ".join(result.output.split())
    assert 'uv tool install --python 3.12 --reinstall "evidence-judge[anthropic,mcp]"' in text
    assert 'pip install "evidence-judge[mcp]"' in text
    assert "pip install 'evidence-judge" not in text


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        ("stdio", ("stdio", {})),
        ("streamable-http", ("streamable-http", {"host": "127.0.0.1", "port": 9})),
    ],
)
def test_serve_runs_the_transport_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: server_module.Transport,
    expected: tuple[str, dict[str, Any]],
) -> None:
    runs: list[tuple[str, dict[str, Any]]] = []

    def run(self: MCPServer, transport: str, **kwargs: Any) -> None:
        runs.append((transport, kwargs))

    monkeypatch.setattr(MCPServer, "run", run)
    server_module.serve(ServerConfig(roots=Roots.of([tmp_path])), transport=transport, port=9)
    assert runs == [expected]
