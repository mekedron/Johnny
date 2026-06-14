"""Runnable demo MCP servers shipped with Johnny (Johnny-3gx).

Secret-free reference connectors so a clean install can exercise the answer
agent's MCP gateway tools (list → load → call) end to end. The stdio demos live
in the sandbox image (``sandbox/mcp_fixture_server.py`` /
``sandbox/mcp_demo_server.py``); :mod:`johnny.mcp.demo.http_server` is the
HTTP-transport demo, run as the ``mcp-demo-http`` compose service.
"""
