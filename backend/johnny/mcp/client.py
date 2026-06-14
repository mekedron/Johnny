"""MCP connections: sandbox-bridged stdio + direct streamable HTTP.

This is the only module in :mod:`johnny.mcp` that imports the ``mcp`` SDK —
it is touched exclusively by the worker's executor pass (lazy, claim-time
connections through :class:`McpClientManager`) and the api's probe endpoint
(:func:`probe_mcp_server`, one ephemeral session per probe). Catalog
assembly and the router never import it.

Transports (the Johnny-trt.36 placement rule):

* **stdio** — the server process spawns INSIDE the skills-sandbox container
  (the same security boundary as CLI skills; no MCP process ever runs on
  the host or in the api/worker containers). The SDK still drives the
  protocol: :func:`sandbox_stdio_client` mirrors the SDK's own
  ``stdio_client`` but pumps the newline-delimited JSON-RPC lines over the
  sandbox daemon's ``/mcp/start|send|recv|stop`` bridge instead of a local
  subprocess.
* **http** — :func:`mcp.client.streamable_http.streamablehttp_client`
  straight from this process; there is no subprocess to contain.

Lifecycle (:class:`McpClientManager`): connections open lazily on first
tool reference, are cached per server name while the config fingerprint
matches, and are evicted after ``idle_ttl_s`` without use (the worker's
sweep) — the next use reconnects transparently. A connection that errors or
times out mid-call is evicted immediately rather than trusted again.
Failures surface as typed errors the executor maps to spoken-form
"tool unavailable" results; nothing here may crash the executor pass.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from johnny.mcp.catalog import McpToolInfo
from johnny.mcp.config import TRANSPORT_STDIO, McpServerConfig

logger = logging.getLogger(__name__)

RECV_POLL_S = 25.0
"""Bridge long-poll window — under the daemon's 30s recv cap."""

_HTTP_MARGIN_S = 10.0
_CONNECT_MARGIN_S = 5.0
_CLOSE_TIMEOUT_S = 10.0


class McpClientError(Exception):
    """Base class; messages are operator-facing diagnostics."""


class McpUnavailableError(McpClientError):
    """The server could not be reached / started / kept alive."""


class McpCallTimeoutError(McpClientError):
    """One tool call exceeded the server's ``call_timeout_s``."""


class McpToolError(McpClientError):
    """The server answered with a JSON-RPC error (e.g. unknown tool)."""


def describe_mcp_failure(exc: BaseException) -> str:
    """One-line diagnostic for an arbitrary transport/SDK failure.

    Task groups (ours and the SDK's) surface child errors as exception
    groups — unwrap to the first non-cancellation leaf so the operator sees
    "ConnectError: …" instead of "unhandled errors in a TaskGroup".
    """
    seen: set[int] = set()

    def _leaf(e: BaseException) -> BaseException:
        if id(e) in seen:  # defensive: __cause__ cycles
            return e
        seen.add(id(e))
        if isinstance(e, BaseExceptionGroup):
            for child in e.exceptions:
                if not isinstance(child, asyncio.CancelledError):
                    return _leaf(child)
            return e
        if isinstance(e, McpClientError) or e.__cause__ is None:
            return e
        return e if str(e).strip() else _leaf(e.__cause__)

    leaf = _leaf(exc)
    if isinstance(leaf, McpClientError):
        return str(leaf)
    text = str(leaf).strip() or repr(leaf)
    return f"{type(leaf).__name__}: {text}"


def _bridge_payload(response: httpx.Response) -> dict[str, Any]:
    """Parse a bridge reply; non-2xx becomes :class:`McpUnavailableError`."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise McpUnavailableError(
            f"sandbox mcp bridge returned non-JSON (HTTP {response.status_code})"
        ) from exc
    if not isinstance(payload, dict):
        raise McpUnavailableError("sandbox mcp bridge returned a non-object body")
    if response.status_code >= 400:
        raise McpUnavailableError(
            str(payload.get("error") or f"bridge HTTP {response.status_code}")
        )
    return payload


@asynccontextmanager
async def sandbox_stdio_client(
    config: McpServerConfig,
    *,
    sandbox_url: str,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[tuple[Any, Any]]:
    """The SDK ``stdio_client`` shape, bridged through the sandbox daemon.

    Spawns ``config.argv`` inside the skills-sandbox via ``POST /mcp/start``,
    then pumps :class:`SessionMessage` traffic: writer → ``POST /mcp/send``
    (one JSON-RPC line per message), reader ← long-polled ``GET /mcp/recv``.
    Yields ``(read_stream, write_stream)`` exactly like the SDK transports;
    on exit the bridge session is stopped (shielded best-effort) and the
    sandbox daemon SIGTERM/SIGKILLs the server's process group.
    """
    own_http = http_client is None
    http = http_client or httpx.AsyncClient(base_url=sandbox_url.rstrip("/"))
    try:
        try:
            response = await http.post(
                "/mcp/start",
                json={"argv": list(config.argv), "env": dict(config.env)},
                timeout=config.connect_timeout_s + _HTTP_MARGIN_S,
            )
        except httpx.HTTPError as exc:
            raise McpUnavailableError(f"skills sandbox unreachable: {exc}") from exc
        sid = str(_bridge_payload(response).get("sid") or "")
        if not sid:
            raise McpUnavailableError("sandbox mcp bridge returned no session id")

        read_send, read_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](0)
        write_send, write_recv = anyio.create_memory_object_stream[SessionMessage](0)

        async def _reader() -> None:
            try:
                while True:
                    response = await http.get(
                        "/mcp/recv",
                        params={"sid": sid, "timeout": RECV_POLL_S},
                        timeout=RECV_POLL_S + _HTTP_MARGIN_S,
                    )
                    payload = _bridge_payload(response)
                    line = payload.get("line")
                    if isinstance(line, str) and line:
                        try:
                            message = mcp_types.JSONRPCMessage.model_validate_json(line)
                        except ValidationError as exc:
                            # The SDK contract: unparseable lines ride the read
                            # stream as the exception for the session to handle.
                            await read_send.send(exc)
                            continue
                        await read_send.send(SessionMessage(message))
                        continue
                    if payload.get("exited"):
                        detail = str(payload.get("error") or "").strip()
                        tail = str(payload.get("stderr_tail") or "").strip()
                        exit_code = payload.get("exit_code")
                        parts = [f"mcp server '{config.name}' exited (code {exit_code})"]
                        if detail:
                            parts.append(detail)
                        if tail:
                            parts.append(f"stderr: {tail[-500:]}")
                        raise McpUnavailableError("; ".join(parts))
                    # idle poll window elapsed — keep listening
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass  # session torn down under us — normal shutdown order
            except httpx.HTTPError as exc:
                raise McpUnavailableError(
                    f"sandbox mcp bridge lost: {exc}"
                ) from exc
            finally:
                await read_send.aclose()

        async def _writer() -> None:
            try:
                async with write_recv:
                    async for session_message in write_recv:
                        line = session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        )
                        response = await http.post(
                            "/mcp/send",
                            json={"sid": sid, "line": line},
                            timeout=config.connect_timeout_s + _HTTP_MARGIN_S,
                        )
                        _bridge_payload(response)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass
            except httpx.HTTPError as exc:
                raise McpUnavailableError(
                    f"sandbox mcp bridge lost while sending: {exc}"
                ) from exc

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(_reader)
                task_group.start_soon(_writer)
                try:
                    yield read_recv, write_send
                finally:
                    task_group.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await http.post(
                        "/mcp/stop", json={"sid": sid}, timeout=_CLOSE_TIMEOUT_S
                    )
                except (httpx.HTTPError, McpClientError):
                    pass  # reaper backstop owns orphans
    finally:
        if own_http:
            await http.aclose()


@asynccontextmanager
async def open_mcp_session(
    config: McpServerConfig,
    *,
    sandbox_url: str,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[ClientSession]:
    """One initialized :class:`ClientSession` for either transport.

    ``http_client`` (tests) is only meaningful for the stdio bridge; the
    streamable-HTTP transport owns its connection per the SDK.
    """
    if config.transport == TRANSPORT_STDIO:
        transport_cm = sandbox_stdio_client(
            config, sandbox_url=sandbox_url, http_client=http_client
        )
        async with transport_cm as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=config.call_timeout_s),
            ) as session:
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=config.connect_timeout_s
                )
                session.johnny_init_result = init_result  # probe's report
                yield session
    else:
        async with streamablehttp_client(
            url=config.url,
            headers=dict(config.headers) or None,
            timeout=config.connect_timeout_s,
            sse_read_timeout=config.call_timeout_s + RECV_POLL_S,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=config.call_timeout_s),
            ) as session:
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=config.connect_timeout_s
                )
                session.johnny_init_result = init_result  # probe's report
                yield session


async def _list_all_tools(session: ClientSession) -> tuple[McpToolInfo, ...]:
    """``tools/list`` with cursor pagination drained."""
    infos: list[McpToolInfo] = []
    cursor: str | None = None
    while True:
        result = await session.list_tools(cursor=cursor)
        for tool in result.tools:
            name = (tool.name or "").strip()
            if not name or any(ch.isspace() for ch in name):
                logger.warning("mcp: skipping oddly-named tool %r", tool.name)
                continue
            # The SDK's Tool.inputSchema is a plain JSON-Schema dict; carry it so
            # the live ``list_mcp_tools`` path can show the model how to call the
            # tool. Defensive: only attach a dict (never a stray non-mapping).
            raw_schema = getattr(tool, "inputSchema", None)
            input_schema = raw_schema if isinstance(raw_schema, dict) else None
            infos.append(
                McpToolInfo(
                    name=name,
                    description=tool.description or "",
                    input_schema=input_schema,
                )
            )
        cursor = result.nextCursor
        if not cursor:
            break
    return tuple(infos)


@dataclass(frozen=True, slots=True)
class McpProbeResult:
    """What one explicit probe (connect + initialize + list_tools) found."""

    ok: bool
    tools: tuple[McpToolInfo, ...] = ()
    error: str = ""
    server_info: str = ""
    duration_ms: int = 0


async def probe_mcp_server(
    config: McpServerConfig,
    *,
    sandbox_url: str,
    http_client: httpx.AsyncClient | None = None,
) -> McpProbeResult:
    """Ephemeral connect → initialize → list_tools → close; never raises.

    The api's probe endpoint (and tests) call this; the worker's lazy
    connections go through :class:`McpClientManager` instead.
    """
    started = time.monotonic()
    try:
        async with open_mcp_session(
            config, sandbox_url=sandbox_url, http_client=http_client
        ) as session:
            tools = await asyncio.wait_for(
                _list_all_tools(session), timeout=config.call_timeout_s
            )
            server_info = ""
            init_result = getattr(session, "johnny_init_result", None)
            if init_result is not None:
                info = getattr(init_result, "serverInfo", None)
                if info is not None:
                    server_info = f"{info.name} {info.version}".strip()
        return McpProbeResult(
            ok=True,
            tools=tools,
            server_info=server_info,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — a probe reports, never crashes
        return McpProbeResult(
            ok=False,
            error=describe_mcp_failure(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """One successful ``tools/call`` round trip (tool-level errors included)."""

    text: str
    is_error: bool
    duration_ms: int


def _joined_text(result: mcp_types.CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


class McpConnection:
    """One held-open server connection (a dedicated task owns the SDK stack).

    The SDK's transports/sessions are async context managers bound to their
    opening task, so a long-lived connection runs them inside a holder task
    that parks on a close event; calls run in the caller's task against the
    shared :class:`ClientSession` (the SDK session is task-safe for
    concurrent requests).
    """

    def __init__(
        self,
        config: McpServerConfig,
        *,
        sandbox_url: str,
        clock: Callable[[], float] = time.monotonic,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._sandbox_url = sandbox_url
        self._http_client = http_client
        self._clock = clock
        self._ready = asyncio.Event()
        self._close_requested = asyncio.Event()
        self._session: ClientSession | None = None
        self._failure: str | None = None
        self.last_used = clock()
        self._holder = asyncio.create_task(
            self._hold(), name=f"mcp-connection-{config.name}"
        )

    async def _hold(self) -> None:
        try:
            async with open_mcp_session(
                self._config,
                sandbox_url=self._sandbox_url,
                http_client=self._http_client,
            ) as session:
                self._session = session
                self._ready.set()
                await self._close_requested.wait()
        except asyncio.CancelledError:
            self._failure = "connection closed"
        except BaseException as exc:  # noqa: BLE001 — recorded for the next caller
            self._failure = describe_mcp_failure(exc)
            logger.info(
                "mcp: connection to '%s' ended: %s", self._config.name, self._failure
            )
        finally:
            self._session = None
            self._ready.set()

    @property
    def dead(self) -> bool:
        return self._holder.done() or (self._ready.is_set() and self._session is None)

    async def ensure_ready(self) -> None:
        timeout = self._config.connect_timeout_s + _CONNECT_MARGIN_S
        try:
            await asyncio.wait_for(
                asyncio.shield(self._ready.wait()), timeout=timeout
            )
        except TimeoutError:
            raise McpUnavailableError(
                f"connecting to MCP server '{self._config.name}' timed out "
                f"after {timeout:.0f}s"
            ) from None
        if self._session is None:
            raise McpUnavailableError(
                self._failure
                or f"MCP server '{self._config.name}' connection closed"
            )

    async def call_tool(
        self, tool: str, arguments: dict[str, Any], *, timeout_s: float
    ) -> McpCallResult:
        session = self._session
        if session is None:
            raise McpUnavailableError(
                self._failure or f"MCP server '{self._config.name}' is not connected"
            )
        self.last_used = self._clock()
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool, arguments), timeout=timeout_s
            )
        except TimeoutError:
            raise McpCallTimeoutError(
                f"tool '{tool}' on '{self._config.name}' timed out after "
                f"{timeout_s:.0f}s"
            ) from None
        except McpError as exc:
            raise McpToolError(
                f"'{self._config.name}' rejected tool '{tool}': {exc}"
            ) from exc
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # closed streams, transport loss, …
            raise McpUnavailableError(describe_mcp_failure(exc)) from exc
        self.last_used = self._clock()
        return McpCallResult(
            text=_joined_text(result),
            is_error=bool(result.isError),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def list_tools(self, *, timeout_s: float) -> tuple[McpToolInfo, ...]:
        session = self._session
        if session is None:
            raise McpUnavailableError(
                self._failure or f"MCP server '{self._config.name}' is not connected"
            )
        self.last_used = self._clock()
        try:
            tools = await asyncio.wait_for(_list_all_tools(session), timeout=timeout_s)
        except TimeoutError:
            raise McpCallTimeoutError(
                f"tools/list on '{self._config.name}' timed out after {timeout_s:.0f}s"
            ) from None
        except McpError as exc:
            raise McpToolError(f"tools/list on '{self._config.name}' failed: {exc}") from exc
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise McpUnavailableError(describe_mcp_failure(exc)) from exc
        self.last_used = self._clock()
        return tools

    async def aclose(self) -> None:
        self._close_requested.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._holder), timeout=_CLOSE_TIMEOUT_S)
        except TimeoutError:
            self._holder.cancel()
            try:
                await self._holder
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


@dataclass(slots=True)
class _ManagerEntry:
    connection: McpConnection
    fingerprint: str
    idle_ttl_s: float


ConnectionFactory = Callable[..., McpConnection]


class McpClientManager:
    """Lazy per-server connections with fingerprint reuse + idle-TTL eviction.

    One instance per worker process (it lives on the
    :class:`~app.services.task_worker.SandboxExecutorProvider`). Keyed by
    server *name*: a config edit that changes the connection fingerprint
    (command/env/url/headers) tears the old connection down on next use;
    filter/timeout/TTL edits apply live without a reconnect.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        connection_factory: ConnectionFactory = McpConnection,
    ) -> None:
        self._clock = clock
        self._connection_factory = connection_factory
        self._entries: dict[str, _ManagerEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    @staticmethod
    def _fingerprint(config: McpServerConfig, sandbox_url: str) -> str:
        # The sandbox is part of a stdio connection's identity (Phase 7
        # per-agent sandboxes re-key through the resolver, never this code).
        suffix = sandbox_url if config.transport == TRANSPORT_STDIO else ""
        return f"{config.connection_fingerprint()}|{suffix}"

    async def _server_lock(self, name: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[name] = lock
            return lock

    async def _evict(self, name: str) -> None:
        entry = self._entries.pop(name, None)
        if entry is not None:
            await entry.connection.aclose()

    async def _acquire(
        self, config: McpServerConfig, sandbox_url: str
    ) -> McpConnection:
        lock = await self._server_lock(config.name)
        async with lock:
            fingerprint = self._fingerprint(config, sandbox_url)
            entry = self._entries.get(config.name)
            if entry is not None and (
                entry.fingerprint != fingerprint or entry.connection.dead
            ):
                await self._evict(config.name)
                entry = None
            if entry is None:
                connection = self._connection_factory(
                    config, sandbox_url=sandbox_url, clock=self._clock
                )
                try:
                    await connection.ensure_ready()
                except BaseException:
                    await connection.aclose()
                    raise
                entry = _ManagerEntry(
                    connection=connection,
                    fingerprint=fingerprint,
                    idle_ttl_s=config.idle_ttl_s,
                )
                self._entries[config.name] = entry
                logger.info(
                    "mcp: connected to '%s' (%s)", config.name, config.transport
                )
            else:
                entry.idle_ttl_s = config.idle_ttl_s
            return entry.connection

    async def call_tool(
        self,
        config: McpServerConfig,
        *,
        sandbox_url: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> McpCallResult:
        connection = await self._acquire(config, sandbox_url)
        try:
            return await connection.call_tool(
                tool, arguments, timeout_s=config.call_timeout_s
            )
        except (McpUnavailableError, McpCallTimeoutError):
            # A lost or timed-out connection is poisoned (orphaned request
            # state) — evict so the next use reconnects fresh. McpToolError
            # stays connected: the protocol round-trip itself worked.
            lock = await self._server_lock(config.name)
            async with lock:
                if self._entries.get(config.name) is not None and (
                    self._entries[config.name].connection is connection
                ):
                    await self._evict(config.name)
            raise

    async def list_tools(
        self, config: McpServerConfig, *, sandbox_url: str
    ) -> tuple[McpToolInfo, ...]:
        connection = await self._acquire(config, sandbox_url)
        return await connection.list_tools(timeout_s=config.call_timeout_s)

    async def sweep_idle(self) -> int:
        """Close connections idle past their TTL; returns how many were evicted."""
        now = self._clock()
        evicted = 0
        for name in list(self._entries):
            entry = self._entries.get(name)
            if entry is None:
                continue
            if now - entry.connection.last_used < entry.idle_ttl_s:
                continue
            lock = await self._server_lock(name)
            async with lock:
                entry = self._entries.get(name)
                if entry is not None and (
                    now - entry.connection.last_used >= entry.idle_ttl_s
                ):
                    logger.info(
                        "mcp: evicting idle connection to '%s' (>%gs unused)",
                        name,
                        entry.idle_ttl_s,
                    )
                    await self._evict(name)
                    evicted += 1
        return evicted

    async def aclose(self) -> None:
        for name in list(self._entries):
            lock = await self._server_lock(name)
            async with lock:
                await self._evict(name)


__all__ = [
    "McpCallResult",
    "McpCallTimeoutError",
    "McpClientError",
    "McpClientManager",
    "McpConnection",
    "McpProbeResult",
    "McpToolError",
    "McpUnavailableError",
    "describe_mcp_failure",
    "open_mcp_session",
    "probe_mcp_server",
    "sandbox_stdio_client",
]
