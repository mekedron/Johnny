"""MCP server config + the ``mcp__<server>__<tool>`` naming contract.

Stdlib-only on purpose (like :mod:`johnny.agent.task_catalog`): the catalog
assembly, the router gate, and the capability policy all reason about MCP
kinds without importing the ``mcp`` SDK — only :mod:`johnny.mcp.client`
(worker executor pass + api probe) pays that import.

Naming: a server's tools enter the catalog as ``mcp__<server>__<tool>``
(the openclaw convention the Johnny-trt.38 policy namespace already
documents — ``mcp__shady__*`` denies a whole server). Server names therefore
must not contain ``__``; the validation below forbids underscores entirely
(lowercase slug, hyphens only) so :func:`parse_qualified_tool_name` splits
unambiguously even when the *tool* name itself contains ``__``.

Configs live in each workspace's ``.johnny/.mcp.json`` (the FastMCP
``mcpServers`` file — :mod:`johnny.mcp.store` maps entries to this frozen
value object, expanding ``${VAR}`` in ``env`` / ``headers`` on the way out,
Johnny-hp1). This module never reads any store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

MCP_TOOL_PREFIX = "mcp__"
_QUALIFIED_SEPARATOR = "__"

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
MCP_TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)

SERVER_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
"""Lowercase slug, hyphens only — NO underscores, so the qualified-name
separator ``__`` can never appear inside a server name."""

DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_CALL_TIMEOUT_S = 60.0
DEFAULT_IDLE_TTL_S = 300.0

_TIMEOUT_MIN_S = 1.0
_TIMEOUT_MAX_S = 600.0
_IDLE_TTL_MIN_S = 10.0
_IDLE_TTL_MAX_S = 3600.0


class McpConfigError(ValueError):
    """A server config that cannot be persisted/used — message is operator-facing."""


def qualified_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>`` — the catalog kind for one server tool."""
    return f"{MCP_TOOL_PREFIX}{server}{_QUALIFIED_SEPARATOR}{tool}"


def parse_qualified_tool_name(kind: str) -> tuple[str, str] | None:
    """Split a qualified kind back into ``(server, tool)``; ``None`` if not MCP-shaped.

    Server names cannot contain underscores (enforced at config validation),
    so the first ``__`` after the prefix is always the separator — a tool
    name containing ``__`` survives round-tripping.
    """
    if not kind.startswith(MCP_TOOL_PREFIX):
        return None
    rest = kind[len(MCP_TOOL_PREFIX) :]
    server, sep, tool = rest.partition(_QUALIFIED_SEPARATOR)
    if not sep or not server or not tool:
        return None
    return server, tool


def is_mcp_kind(kind: str) -> bool:
    """Whether ``kind`` is an MCP-qualified tool name (prefix + both parts)."""
    return parse_qualified_tool_name(kind) is not None


def filter_tool_names(
    names: list[str] | tuple[str, ...],
    *,
    include: tuple[str, ...] | None,
    exclude: tuple[str, ...],
) -> tuple[str, ...]:
    """Apply a server's tool include/exclude globs (``fnmatch`` case-sensitive).

    ``include=None`` admits everything (the unset default); an *empty* include
    list admits nothing (an operator explicitly allow-listing zero tools).
    Exclude wins over include, mirroring the deny-wins rule of the
    capability-policy engine (Johnny-trt.38).
    """
    kept: list[str] = []
    for name in names:
        if include is not None and not any(fnmatchcase(name, glob) for glob in include):
            continue
        if any(fnmatchcase(name, glob) for glob in exclude):
            continue
        kept.append(name)
    return tuple(kept)


def _clamped(value: float, *, minimum: float, maximum: float) -> float:
    return min(max(float(value), minimum), maximum)


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One configured MCP server, validated and ready for the client layer.

    ``env`` / ``headers`` are PLAINTEXT here (``${VAR}`` expanded by the store
    on the way out of ``.mcp.json``) — this object stays in process memory
    only and must never be logged whole or serialized into API responses;
    responses mask values to key names (the provider-settings pattern).
    """

    name: str
    transport: str
    enabled: bool = True
    # stdio transport: the server process spawned INSIDE the skills-sandbox
    # container (same security boundary as CLI skills, Johnny-trt.35).
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # http transport: a streamable-HTTP endpoint the worker/api connect to
    # directly (no sandbox hop — there is no process to contain).
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # Per-server tool filters: include=None ⇒ all tools; exclude wins.
    tool_include: tuple[str, ...] | None = None
    tool_exclude: tuple[str, ...] = ()
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    idle_ttl_s: float = DEFAULT_IDLE_TTL_S

    def __post_init__(self) -> None:
        if not SERVER_NAME_PATTERN.match(self.name):
            raise McpConfigError(
                "server name must be a lowercase slug (a-z, 0-9, hyphens; max 64 "
                f"chars, no underscores — it prefixes mcp__<name>__<tool>): {self.name!r}"
            )
        if self.transport not in MCP_TRANSPORTS:
            raise McpConfigError(
                f"transport must be one of {', '.join(MCP_TRANSPORTS)}: {self.transport!r}"
            )
        if self.transport == TRANSPORT_STDIO:
            if not self.command.strip():
                raise McpConfigError("stdio transport requires a non-empty 'command'")
            if self.url:
                raise McpConfigError("stdio transport must not set 'url'")
        else:
            if not self.url.strip():
                raise McpConfigError("http transport requires a non-empty 'url'")
            if not self.url.startswith(("http://", "https://")):
                raise McpConfigError(
                    f"http transport 'url' must be http(s)://…: {self.url!r}"
                )
            if self.command or self.args:
                raise McpConfigError("http transport must not set 'command'/'args'")
        object.__setattr__(
            self,
            "connect_timeout_s",
            _clamped(self.connect_timeout_s, minimum=_TIMEOUT_MIN_S, maximum=_TIMEOUT_MAX_S),
        )
        object.__setattr__(
            self,
            "call_timeout_s",
            _clamped(self.call_timeout_s, minimum=_TIMEOUT_MIN_S, maximum=_TIMEOUT_MAX_S),
        )
        object.__setattr__(
            self,
            "idle_ttl_s",
            _clamped(self.idle_ttl_s, minimum=_IDLE_TTL_MIN_S, maximum=_IDLE_TTL_MAX_S),
        )

    @property
    def argv(self) -> tuple[str, ...]:
        """The stdio spawn argv: command + args (command is argv[0], not a shell line)."""
        return (self.command, *self.args)

    def filtered_tool_names(self, names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """This server's include/exclude globs applied to raw tool names."""
        return filter_tool_names(
            names, include=self.tool_include, exclude=self.tool_exclude
        )

    def allows_tool(self, tool: str) -> bool:
        """Whether one tool name survives this server's filters (claim-time check)."""
        return bool(self.filtered_tool_names((tool,)))

    def connection_fingerprint(self) -> str:
        """Identity of the *connection* this config describes.

        A cached live connection is reused only while the fingerprint
        matches — editing the command/env/url tears it down on next use,
        while filter/timeout/TTL edits apply live (they are read from the
        latest config at call/sweep time, no reconnect needed).
        """
        payload = {
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "env": dict(sorted(self.env.items())),
            "url": self.url,
            "headers": dict(sorted(self.headers.items())),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_IDLE_TTL_S",
    "MCP_TOOL_PREFIX",
    "MCP_TRANSPORTS",
    "McpConfigError",
    "McpServerConfig",
    "SERVER_NAME_PATTERN",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "filter_tool_names",
    "is_mcp_kind",
    "parse_qualified_tool_name",
    "qualified_tool_name",
]
