"""An MCP server exposing Evidence Judge as tools (``evidence-judge mcp``; needs the ``mcp``
extra). ``build_server`` takes extra ``Toolset`` callables to register more tools."""

from evidence_judge.mcp.paths import PathNotAllowedError, Roots
from evidence_judge.mcp.server import DEFAULT_TOOLSETS, Toolset, build_server, core_tools, serve
from evidence_judge.mcp.service import ServerConfig, ToolInputError

__all__ = [
    "DEFAULT_TOOLSETS",
    "PathNotAllowedError",
    "Roots",
    "ServerConfig",
    "ToolInputError",
    "Toolset",
    "build_server",
    "core_tools",
    "serve",
]
