"""Reference stdio MCP server for the Johnny sandbox (Johnny-trt.36).

Stdlib-only, like everything in this image. Speaks newline-delimited
JSON-RPC 2.0 over stdin/stdout per the Model Context Protocol: handles
``initialize`` (echoes the client's protocolVersion), the ``initialized``
notification, ``ping``, ``tools/list``, and ``tools/call`` for three tools:

* ``echo``    — returns ``message`` verbatim (the happy path);
* ``add``     — returns ``a + b`` (argument plumbing);
* ``always-fail`` — returns ``isError: true`` (the tool-level sad path).

Purpose: the acceptance fixture this bead names — configure it through the
management API as a stdio server (command ``python3``, args
``["/opt/sandbox/mcp_fixture_server.py"]``), probe it, and its tools appear
as ``mcp__<name>__echo`` / ``__add`` / ``__always-fail`` in the catalog.
The integration suite drives it through the real bridge; operators get a
known-good server to verify the MCP plumbing before configuring real ones.
"""

from __future__ import annotations

import json
import sys
from typing import Any

SERVER_INFO = {"name": "johnny-mcp-fixture", "version": "1.0.0"}

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the provided message back, prefixed with 'echo: '.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
    {
        "name": "always-fail",
        "description": "Always return a tool-level error (isError) — for sad-path tests.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _reply(msg_id: Any, result: dict[str, Any]) -> None:
    print(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}), flush=True)


def _reply_error(msg_id: Any, code: int, message: str) -> None:
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        ),
        flush=True,
    )


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    if name == "echo":
        return _text_result(f"echo: {arguments.get('message', '')}")
    if name == "add":
        try:
            total = float(arguments["a"]) + float(arguments["b"])
        except (KeyError, TypeError, ValueError):
            return _text_result("add needs numeric 'a' and 'b'", is_error=True)
        text = str(int(total)) if total == int(total) else str(total)
        return _text_result(text)
    if name == "always-fail":
        return _text_result("this fixture tool always fails", is_error=True)
    raise KeyError(name)


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue  # not ours to diagnose — a real server would log
        if not isinstance(msg, dict):
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if msg_id is None:
            continue  # notifications (initialized, cancelled) need no reply
        if method == "initialize":
            _reply(
                msg_id,
                {
                    # Echo the client's requested version: the fixture exists to
                    # exercise Johnny's client, not to negotiate downgrades.
                    "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            )
        elif method == "ping":
            _reply(msg_id, {})
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                _reply(msg_id, _call_tool(params))
            except KeyError as exc:
                _reply_error(msg_id, -32602, f"unknown tool: {exc.args[0]!r}")
        else:
            _reply_error(msg_id, -32601, f"method not supported by fixture: {method!r}")


if __name__ == "__main__":
    main()
