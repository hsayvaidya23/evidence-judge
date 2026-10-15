"""`evidence-judge mcp` as a real subprocess speaking MCP over stdio."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.types import TextContent

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


async def test_stdio_server_initializes_lists_and_calls_tools(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "runs" / "forged_success", tmp_path / "run")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["EVJ_CACHE_DIR"] = str(tmp_path / "cache")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "evidence_judge", "mcp", "--root", str(tmp_path), "--offline-only"],
        env=env,
        cwd=tmp_path,
    )
    async with Client(params) as client:
        assert client.server_info is not None
        assert client.server_info.name == "evidence-judge"
        names = {t.name for t in (await client.list_tools()).tools}
        assert "judge_run" in names
        result = await client.call_tool(
            "judge_run",
            {
                "trace": "run/trace.zip",
                "answer_file": "run/answer.txt",
                "spec": "run/spec.yaml",
            },
        )
        assert not result.is_error
        assert isinstance(result.structured_content, dict)
        assert result.structured_content["verdict"]["outcome"] == "fail"
        escaped = await client.call_tool("inspect_run", {"trace": "/etc/hosts"})
        assert escaped.is_error
        block = escaped.content[0]
        assert isinstance(block, TextContent)
        assert "outside the allowed roots" in block.text
