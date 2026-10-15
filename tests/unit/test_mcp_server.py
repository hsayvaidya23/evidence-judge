"""The MCP tools, called through an in-process MCP client session."""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

import evidence_judge.mcp.service
from evidence_judge.config import Settings
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.mcp import DEFAULT_TOOLSETS, Roots, ServerConfig, build_server
from evidence_judge.mcp.models import Explanation, InspectResult, JudgeResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EMAIL = "alice@example.com"


def _settings(**overrides: Any) -> Callable[[], Settings]:
    def make() -> Settings:
        return Settings(_env_file=None, anthropic_api_key=None, cache_dir=None, **overrides)  # pyright: ignore[reportCallIssue]

    return make


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for name in ("forged_success", "honest_success"):
        shutil.copytree(FIXTURES / "runs" / name, tmp_path / name)
    return tmp_path


def _connect(workspace: Path, **options: Any) -> Client:
    """An in-process client session, entered and left inside one test task."""
    settings = options.pop("settings", _settings())
    config = ServerConfig(roots=Roots.of([workspace]), settings=settings, **options)
    return Client(build_server(config))


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def _ok(client: Client, tool: str, args: dict[str, Any]) -> CallToolResult:
    result = await client.call_tool(tool, args)
    assert not result.is_error, _text(result)
    assert isinstance(result.structured_content, dict)
    return result


async def _error(client: Client, tool: str, args: dict[str, Any]) -> str:
    result = await client.call_tool(tool, args)
    assert result.is_error
    assert result.structured_content is None
    return _text(result)


async def _call(workspace: Path, tool: str, args: dict[str, Any]) -> CallToolResult:
    async with _connect(workspace) as client:
        return await _ok(client, tool, args)


async def _fail(workspace: Path, tool: str, args: dict[str, Any], **options: Any) -> str:
    async with _connect(workspace, **options) as client:
        return await _error(client, tool, args)


def _run_args(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "trace": f"{name}/trace.zip",
        "har": f"{name}/run.har",
        "answer_file": f"{name}/answer.txt",
        "spec": f"{name}/spec.yaml",
        **extra,
    }


async def test_tools_are_listed_with_input_and_output_schemas(workspace: Path) -> None:
    async with _connect(workspace) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "judge_run",
        "inspect_run",
        "explain_verdict",
        "validate_spec",
        "find_recordings",
    }
    for tool in tools.values():
        assert tool.output_schema is not None
        assert tool.description
        assert tool.annotations is not None
    judge = tools["judge_run"]
    props = judge.input_schema["properties"]
    assert props["offline"]["default"] is True
    assert "description" in props["trace"]
    assert judge.output_schema is not None
    assert set(judge.output_schema["properties"]) >= {"verdict", "reports", "redaction"}
    inspect = tools["inspect_run"].annotations
    assert inspect is not None
    assert inspect.read_only_hint is True


async def test_server_reports_its_name_and_instructions(workspace: Path) -> None:
    async with _connect(workspace) as client:
        assert client.server_info is not None
        assert client.server_info.name == "evidence-judge"
        assert client.instructions is not None
        assert "judge_run" in client.instructions


async def test_judge_run_fails_a_forged_success_and_writes_reports(workspace: Path) -> None:
    result = await _call(
        workspace,
        "judge_run",
        _run_args("forged_success", json_report="verdict.json", html_report="report.html"),
    )
    judged = JudgeResult.model_validate(result.structured_content)
    assert judged.verdict.outcome is Outcome.FAIL
    assert not judged.used_llm
    assert any(c.citations for c in judged.verdict.claims)
    assert judged.verdict.tamper_detection is not None
    assert [r.kind for r in judged.reports] == ["json", "html"]
    text = _text(result)
    assert text.startswith("FAIL score")
    assert "contradicted" in text
    assert "wrote json report" in text
    saved = RunVerdict.model_validate_json((workspace / "verdict.json").read_text("utf-8"))
    assert saved.outcome is Outcome.FAIL
    assert (workspace / "report.html").stat().st_size > 0


async def test_judge_run_passes_an_honest_success(workspace: Path) -> None:
    result = await _call(workspace, "judge_run", _run_args("honest_success"))
    judged = JudgeResult.model_validate(result.structured_content)
    assert judged.verdict.outcome is Outcome.PASS, judged.verdict.summary


async def test_judge_run_takes_an_inline_spec_and_answer(workspace: Path) -> None:
    spec = (workspace / "honest_success" / "spec.yaml").read_text("utf-8")
    answer = (workspace / "honest_success" / "answer.txt").read_text("utf-8")
    args = _run_args("honest_success", spec_yaml=spec, answer=answer)
    del args["spec"], args["answer_file"]
    result = await _call(workspace, "judge_run", args)
    assert JudgeResult.model_validate(result.structured_content).verdict.outcome is Outcome.PASS


async def test_judge_run_redacts_the_answer(workspace: Path) -> None:
    args = _run_args("forged_success", answer=f"I placed the order and emailed {EMAIL}.")
    del args["answer_file"]
    result = await _call(workspace, "judge_run", args)
    assert EMAIL not in json.dumps(result.structured_content) + _text(result)
    assert "redacted" in JudgeResult.model_validate(result.structured_content).redaction


async def test_judge_run_respects_redaction_settings(workspace: Path) -> None:
    async with _connect(workspace, settings=_settings(redact=False)) as client:
        result = await _ok(client, "judge_run", _run_args("forged_success"))
    assert "disabled" in JudgeResult.model_validate(result.structured_content).redaction


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "provide evidence"),
        ({"trace": "forged_success/trace.zip", "answer": "x", "answer_file": "x"}, "either"),
        (
            {"trace": "forged_success/trace.zip", "spec": "x", "spec_yaml": "id: x"},
            "either spec or spec_yaml",
        ),
        ({"trace": "forged_success/trace.zip", "spec_yaml": "id: x\n"}, "task (line 1, column 1)"),
        ({"trace": "forged_success/trace.zip", "site": "github.com/org"}, "site"),
        ({"trace": "nope.zip"}, "does not exist"),
        ({"trace": "forged_success/answer.txt"}, "could not load evidence"),
        ({"trace": "forged_success"}, "not a regular file"),
        ({"trace": "forged_success/trace.zip", "answer": "x" * 200_001}, "answer_file"),
        ({"trace": "forged_success/trace.zip", "har": "other.har"}, "allow_mismatch"),
        ({"trace": "forged_success/trace.zip", "json_report": "missing/v.json"}, "does not exist"),
    ],
)
async def test_judge_run_input_errors_are_tool_errors(
    workspace: Path, args: dict[str, Any], message: str
) -> None:
    har = json.loads((workspace / "forged_success" / "run.har").read_text("utf-8"))
    for entry in har["log"]["entries"]:
        entry["request"]["url"] = "https://elsewhere.test/unrelated"
    (workspace / "other.har").write_text(json.dumps(har), encoding="utf-8")
    assert message in await _fail(workspace, "judge_run", args)


async def test_bad_argument_types_are_tool_errors(workspace: Path) -> None:
    async with _connect(workspace) as client:
        assert "valid string" in await _error(client, "judge_run", {"trace": 5})
        assert "limit" in await _error(client, "find_recordings", {"limit": 0})
        assert "verdict_path" in await _error(client, "explain_verdict", {})


@pytest.mark.parametrize("field", ["trace", "har", "answer_file", "spec", "json_report"])
async def test_paths_outside_the_roots_are_refused(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory, field: str
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    message = await _fail(
        workspace, "judge_run", {"trace": "forged_success/trace.zip", field: str(secret)}
    )
    assert "outside the allowed roots" in message
    assert "top secret" not in message


async def test_symlinks_cannot_escape_the_roots(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    shutil.copy(FIXTURES / "runs" / "forged_success" / "trace.zip", outside / "trace.zip")
    (workspace / "escape.zip").symlink_to(outside / "trace.zip")
    (workspace / "escape_dir").symlink_to(outside, target_is_directory=True)
    async with _connect(workspace) as client:
        for path in ("escape.zip", "escape_dir/trace.zip", f"../{outside.name}/trace.zip"):
            message = await _error(client, "inspect_run", {"trace": path})
            assert "outside the allowed roots" in message


async def test_online_judging_needs_a_model(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence_judge.mcp.service.factory, "build_llm", lambda settings: None)
    message = await _fail(workspace, "judge_run", _run_args("forged_success", offline=False))
    assert "ANTHROPIC_API_KEY" in message


async def test_offline_only_servers_refuse_online_judging(workspace: Path) -> None:
    args = _run_args("forged_success", offline=False)
    assert "--offline-only" in await _fail(workspace, "judge_run", args, allow_llm=False)


async def test_invalid_settings_are_tool_errors(workspace: Path) -> None:
    message = await _fail(
        workspace,
        "inspect_run",
        {"trace": "forged_success/trace.zip"},
        settings=_settings(max_cost_usd=-1),
    )
    assert "invalid evidence-judge settings" in message


async def test_inspect_run_summarises_a_recording(workspace: Path) -> None:
    result = await _call(
        workspace,
        "inspect_run",
        {"trace": "forged_success/trace.zip", "har": "forged_success/run.har"},
    )
    summary = InspectResult.model_validate(result.structured_content)
    assert summary.source == "playwright-trace+har"
    assert {s.name for s in summary.sources} == {"playwright-trace", "har"}
    assert "network" in summary.captured
    assert summary.counts["network"] > 0
    assert summary.mutations
    assert summary.redacted
    assert "captured" in _text(result)


async def test_inspect_run_lists_injected_scripts(workspace: Path) -> None:
    shutil.copy(FIXTURES / "injection" / "evaluate.zip", workspace / "evaluate.zip")
    result = await _call(workspace, "inspect_run", {"trace": "evaluate.zip"})
    summary = InspectResult.model_validate(result.structured_content)
    assert any(i.sha256 for i in summary.injections)


async def test_inspect_run_reports_load_warnings(workspace: Path) -> None:
    shutil.copytree(FIXTURES / "browser_use", workspace / "bu")
    result = await _call(workspace, "inspect_run", {"browser_use": "bu"})
    summary = InspectResult.model_validate(result.structured_content)
    assert summary.source == "browser-use"
    assert any(w.startswith("browser-use: ") for w in summary.warnings)


async def test_explain_verdict_explains_each_failure(workspace: Path) -> None:
    async with _connect(workspace) as client:
        await _ok(client, "judge_run", _run_args("forged_success", json_report="v.json"))
        result = await _ok(client, "explain_verdict", {"verdict_path": "v.json"})
    explanation = Explanation.model_validate(result.structured_content)
    assert explanation.outcome is Outcome.FAIL
    assert {"claim", "expect", "constraint"} <= {i.kind for i in explanation.items}
    first = explanation.items[0]
    assert (first.kind, first.key, first.status) == ("claim", True, "contradicted")
    assert any(i.evidence for i in explanation.items)
    text = _text(result)
    assert text == explanation.text
    assert text.startswith("**FAIL**")
    assert "evidence: net#" in text


async def test_explain_verdict_on_a_pass(workspace: Path) -> None:
    async with _connect(workspace) as client:
        await _ok(client, "judge_run", _run_args("honest_success", json_report="v.json"))
        result = await _ok(client, "explain_verdict", {"verdict_path": "v.json"})
    explanation = Explanation.model_validate(result.structured_content)
    assert explanation.outcome is Outcome.PASS
    assert explanation.supported_claims > 0
    assert "supported by the evidence" in explanation.text


async def test_explain_verdict_notes_limited_tamper_detection(workspace: Path) -> None:
    args = {
        "har": "honest_success/run.har",
        "answer_file": "honest_success/answer.txt",
        "json_report": "v.json",
    }
    async with _connect(workspace) as client:
        await _ok(client, "judge_run", args)
        result = await _ok(client, "explain_verdict", {"verdict_path": "v.json"})
    explanation = Explanation.model_validate(result.structured_content)
    assert explanation.outcome is Outcome.NEEDS_REVIEW
    assert any(i.kind == "tamper_detection" for i in explanation.items)
    assert any(i.kind == "dimension" for i in explanation.items)


async def test_explain_verdict_input_errors(workspace: Path) -> None:
    (workspace / "bad.json").write_text('{"outcome": "pass"}', encoding="utf-8")
    async with _connect(workspace) as client:
        message = await _error(client, "explain_verdict", {"verdict_path": "bad.json"})
        assert "not an evidence-judge verdict" in message
        message = await _error(client, "explain_verdict", {"verdict_path": "no.json"})
        assert "does not exist" in message


async def test_validate_spec_reports_errors_without_failing(workspace: Path) -> None:
    result = await _call(workspace, "validate_spec", {"spec_yaml": "id: x\ntask: [\n"})
    assert result.structured_content is not None
    assert result.structured_content["valid"] is False
    assert result.structured_content["errors"][0]["type"] == "yaml_syntax"
    assert _text(result).startswith("invalid spec")


async def test_validate_spec_reads_files(workspace: Path) -> None:
    result = await _call(workspace, "validate_spec", {"path": "forged_success/spec.yaml"})
    assert result.structured_content is not None
    assert result.structured_content["valid"] is True
    assert "valid spec 'buy-red-kettle'" in _text(result)


@pytest.mark.parametrize("args", [{}, {"path": "forged_success/spec.yaml", "spec_yaml": "id: x"}])
async def test_validate_spec_needs_exactly_one_source(
    workspace: Path, args: dict[str, Any]
) -> None:
    assert "exactly one" in await _fail(workspace, "validate_spec", args)


async def test_validate_spec_refuses_large_files(workspace: Path) -> None:
    (workspace / "big.yaml").write_text("#" * (2 * 1024 * 1024), encoding="utf-8")
    assert "limit" in await _fail(workspace, "validate_spec", {"path": "big.yaml"})


async def test_find_recordings_lists_newest_first(workspace: Path) -> None:
    live = workspace / ".playwright-mcp" / "traces"
    live.mkdir(parents=True)
    (live / "trace-1.trace").write_text("{}", encoding="utf-8")
    (live / "trace-1.network").write_text("", encoding="utf-8")
    (workspace / "not-a-trace.zip").write_bytes(b"nope")
    with zipfile.ZipFile(workspace / "other.zip", "w") as archive:
        archive.writestr("readme.txt", "x")
    (workspace / "node_modules").mkdir()
    shutil.copy(workspace / "honest_success" / "run.har", workspace / "node_modules" / "x.har")
    shutil.copy(FIXTURES / "browser_use" / "history.json", workspace / "agent_history.json")
    (workspace / "link.har").symlink_to(workspace / "honest_success" / "run.har")

    async with _connect(workspace) as client:
        found = (await _ok(client, "find_recordings", {"limit": 50})).structured_content
        limited = await _ok(client, "find_recordings", {"directory": "forged_success", "limit": 1})
    assert found is not None
    recordings = found["recordings"]
    assert {r["kind"] for r in recordings} == {
        "browser_use",
        "har",
        "playwright_trace",
        "playwright_trace_dir",
    }
    paths = {Path(r["path"]).relative_to(workspace).as_posix() for r in recordings}
    assert ".playwright-mcp/traces" in paths
    assert "agent_history.json" in paths
    assert not paths & {"node_modules/x.har", "not-a-trace.zip", "other.zip", "link.har"}
    modified = [r["modified"] for r in recordings]
    assert modified == sorted(modified, reverse=True)
    assert found["truncated"] is False
    assert limited.structured_content is not None
    assert limited.structured_content["truncated"] is True
    assert len(limited.structured_content["recordings"]) == 1


async def test_find_recordings_stays_inside_the_roots(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    message = await _fail(workspace, "find_recordings", {"directory": str(outside)})
    assert "outside the allowed roots" in message


async def test_find_recordings_stops_at_its_file_limit(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence_judge.mcp.service, "SCAN_MAX_FILES", 2)
    result = await _call(workspace, "find_recordings", {})
    assert result.structured_content is not None
    assert result.structured_content["truncated"] is True
    assert "more not shown" in _text(result)


async def test_extra_toolsets_register_more_tools(workspace: Path) -> None:
    def extra(server: MCPServer, config: ServerConfig) -> None:
        @server.tool()
        def ping() -> str:  # pyright: ignore[reportUnusedFunction]
            return str(config.roots.default)

    config = ServerConfig(roots=Roots.of([workspace]), settings=_settings())
    async with Client(build_server(config, (*DEFAULT_TOOLSETS, extra))) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"ping", "judge_run"} <= names
