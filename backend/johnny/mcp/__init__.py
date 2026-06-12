"""MCP connector (Johnny-trt.36) — the third capability source.

In the three-layer capability model TOOLS execute, SKILLS instruct, and MCP
servers *contribute tools*: every tool a configured server exposes becomes a
delegatable kind named ``mcp__<server>__<tool>``, flowing through the same
catalog → router → worker-executor chain as skills, vetted by the same
capability policy (Johnny-trt.38 globs like ``mcp__shady__*``).

Import discipline mirrors :mod:`johnny.skills`: :mod:`johnny.mcp.config` and
:mod:`johnny.mcp.catalog` are stdlib-only (safe for the gate / catalog
assembly); the ``mcp`` SDK is imported only inside :mod:`johnny.mcp.client`,
which only the worker's executor pass and the api's probe endpoint touch.
"""

from johnny.mcp.config import (
    MCP_TOOL_PREFIX,
    McpConfigError,
    McpServerConfig,
    filter_tool_names,
    is_mcp_kind,
    parse_qualified_tool_name,
    qualified_tool_name,
)

__all__ = [
    "MCP_TOOL_PREFIX",
    "McpConfigError",
    "McpServerConfig",
    "filter_tool_names",
    "is_mcp_kind",
    "parse_qualified_tool_name",
    "qualified_tool_name",
]
