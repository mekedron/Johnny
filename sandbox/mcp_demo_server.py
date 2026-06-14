"""Demo stdio MCP server for the Johnny sandbox (Johnny-3gx).

A secret-free, genuinely-useful companion to ``mcp_fixture_server.py`` so a
clean install has something the answer agent can actually discover and call
end-to-end: ``list_mcp_servers`` → ``list_mcp_tools('demo-tools')`` →
``call_mcp_tool('demo-tools', …)``.

Stdlib-only, like everything in this image. Speaks newline-delimited JSON-RPC
2.0 over stdin/stdout per the Model Context Protocol: ``initialize`` (echoes
the client's protocolVersion), the ``initialized`` notification, ``ping``,
``tools/list``, and ``tools/call`` for five no-credentials tools:

* ``current_time``   — the current UTC time, ISO-8601 (no arguments);
* ``uuid``           — a fresh random UUID v4 (no arguments);
* ``base64_encode``  — base64-encode ``text``;
* ``base64_decode``  — decode base64 ``data`` back to text;
* ``random_number``  — a random integer in ``[minimum, maximum]``.

Registered as a stdio server (command ``python3``, args
``["/opt/sandbox/mcp_demo_server.py"]``); its tools appear as
``mcp__demo-tools__current_time`` etc. in the catalog.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import json
import random
import sys
import uuid as uuid_mod
from typing import Any

SERVER_INFO = {"name": "johnny-mcp-demo", "version": "1.0.0"}

TOOLS = [
    {
        "name": "current_time",
        "description": "Return the current date and time in UTC, ISO-8601 format.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "uuid",
        "description": "Generate and return a fresh random UUID (version 4).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "base64_encode",
        "description": "Base64-encode the given UTF-8 text and return the encoded string.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "base64_decode",
        "description": "Decode the given base64 string back to UTF-8 text.",
        "inputSchema": {
            "type": "object",
            "properties": {"data": {"type": "string"}},
            "required": ["data"],
            "additionalProperties": False,
        },
    },
    {
        "name": "random_number",
        "description": "Return a random integer between minimum and maximum (inclusive).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "minimum": {"type": "integer"},
                "maximum": {"type": "integer"},
            },
            "required": ["minimum", "maximum"],
            "additionalProperties": False,
        },
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
    if name == "current_time":
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        return _text_result(now.isoformat())
    if name == "uuid":
        return _text_result(str(uuid_mod.uuid4()))
    if name == "base64_encode":
        text = arguments.get("text")
        if not isinstance(text, str):
            return _text_result("base64_encode needs a string 'text'", is_error=True)
        return _text_result(base64.b64encode(text.encode("utf-8")).decode("ascii"))
    if name == "base64_decode":
        data = arguments.get("data")
        if not isinstance(data, str):
            return _text_result("base64_decode needs a string 'data'", is_error=True)
        try:
            decoded = base64.b64decode(data, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return _text_result("that is not valid base64 text", is_error=True)
        return _text_result(decoded)
    if name == "random_number":
        try:
            low = int(arguments["minimum"])
            high = int(arguments["maximum"])
        except (KeyError, TypeError, ValueError):
            return _text_result(
                "random_number needs integer 'minimum' and 'maximum'", is_error=True
            )
        if low > high:
            low, high = high, low
        return _text_result(str(random.randint(low, high)))
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
            _reply_error(msg_id, -32601, f"method not supported by demo: {method!r}")


if __name__ == "__main__":
    main()
