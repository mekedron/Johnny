"""HTTP-transport demo MCP server (Johnny-3gx).

A tiny, secret-free streamable-HTTP MCP server so a clean install demonstrates
the answer agent's MCP gateway over the **http** transport (the stdio demos
cover the other path). Run as the ``mcp-demo-http`` compose service — it reuses
the backend image (which already pins ``mcp``), so there is no extra dependency
to install and no runtime ``pip install`` (the clean-install rule).

Registered in the default workspace seed as an ``http`` connector pointing at
``http://mcp-demo-http:9000/mcp``; its tools appear as
``mcp__demo-http__ping`` etc. Listens on the compose network only (no published
host port) — nothing here is reachable from outside the stack.
"""

from __future__ import annotations

import datetime
import json
import os

from mcp.server.fastmcp import FastMCP

_HOST = os.environ.get("JOHNNY_DEMO_MCP_HOST", "0.0.0.0")
_PORT = int(os.environ.get("JOHNNY_DEMO_MCP_PORT", "9000"))

mcp = FastMCP(
    "johnny-demo-http",
    instructions="A secret-free demo connector for verifying Johnny's MCP plumbing.",
    host=_HOST,
    port=_PORT,
    # Stateless so every request stands alone — simplest for a demo connector
    # and friendly to the probe's ephemeral connect-then-close.
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def ping() -> str:
    """Health check — returns 'pong'."""
    return "pong"


@mcp.tool()
def reverse_text(text: str) -> str:
    """Reverse the characters of the given text."""
    return text[::-1]


@mcp.tool()
def word_count(text: str) -> int:
    """Count the whitespace-separated words in the given text."""
    return len(text.split())


@mcp.tool()
def server_time() -> str:
    """Return this server's current UTC time, ISO-8601 format."""
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()


@mcp.tool()
def list_inventory() -> str:
    """List warehouse inventory as a JSON array (structured machine output).

    Unlike the other demo tools (which return ear-ready scalars), this returns
    structured JSON — the shape a third-party server like Metabase's
    ``list_dashboards`` sends back. It exercises the Johnny-d6w.30 result
    voicer: a delegated MCP result must be SUMMARIZED for the ear, never read
    out as raw JSON. The data is fixed so a clean install reproduces it.
    """
    return json.dumps(
        [
            {"sku": "WID-1", "name": "Widget", "in_stock": 42},
            {"sku": "GRO-7", "name": "Grommet", "in_stock": 8},
            {"sku": "SPR-3", "name": "Spring", "in_stock": 0},
        ]
    )


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
