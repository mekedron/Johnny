"""Unit tests: McpClientManager lifecycle — lazy connect, reuse, eviction.

The connection factory is faked (no SDK / network): these tests pin the
*lifecycle contract* of Johnny-trt.36 — connect on first reference, reuse
while the config fingerprint matches, idle-evict after TTL with transparent
reconnect, immediate eviction of poisoned (timed-out / lost) connections.
"""

from __future__ import annotations

from typing import Any

import pytest

from johnny.mcp.client import (
    McpCallResult,
    McpCallTimeoutError,
    McpClientManager,
    McpToolError,
    McpUnavailableError,
)
from johnny.mcp.config import McpServerConfig


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeConnection:
    """Duck-typed McpConnection: records calls, scriptable failures."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        sandbox_url: str,
        clock: Any,
        fail_connect: str | None = None,
    ) -> None:
        self.config = config
        self.sandbox_url = sandbox_url
        self._clock = clock
        self.last_used = clock()
        self.closed = False
        self.dead = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_error: Exception | None = None
        self._fail_connect = fail_connect

    async def ensure_ready(self) -> None:
        if self._fail_connect:
            raise McpUnavailableError(self._fail_connect)

    async def call_tool(
        self, tool: str, arguments: dict[str, Any], *, timeout_s: float
    ) -> McpCallResult:
        self.last_used = self._clock()
        if self.next_error is not None:
            error, self.next_error = self.next_error, None
            raise error
        self.calls.append((tool, arguments))
        return McpCallResult(text=f"ran {tool}", is_error=False, duration_ms=1)

    async def aclose(self) -> None:
        self.closed = True
        self.dead = True


class FakeFactory:
    def __init__(self, clock: FakeClock, *, fail_connect: str | None = None) -> None:
        self._clock = clock
        self.fail_connect = fail_connect
        self.created: list[FakeConnection] = []

    def __call__(
        self, config: McpServerConfig, *, sandbox_url: str, clock: Any
    ) -> FakeConnection:
        connection = FakeConnection(
            config,
            sandbox_url=sandbox_url,
            clock=clock,
            fail_connect=self.fail_connect,
        )
        self.created.append(connection)
        return connection


def _config(**overrides: Any) -> McpServerConfig:
    base: dict[str, Any] = {
        "name": "fixture",
        "transport": "stdio",
        "command": "python3",
        "idle_ttl_s": 300.0,
    }
    base.update(overrides)
    return McpServerConfig(**base)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def factory(clock: FakeClock) -> FakeFactory:
    return FakeFactory(clock)


@pytest.fixture
def manager(clock: FakeClock, factory: FakeFactory) -> McpClientManager:
    return McpClientManager(clock=clock, connection_factory=factory)


async def test_lazy_connect_and_reuse(
    manager: McpClientManager, factory: FakeFactory
) -> None:
    config = _config()
    assert factory.created == []  # nothing connects until first reference
    result = await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={"m": "hi"}
    )
    assert result.text == "ran echo"
    await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="add", arguments={}
    )
    assert len(factory.created) == 1  # one connection serves both calls
    assert factory.created[0].calls == [("echo", {"m": "hi"}), ("add", {})]


async def test_fingerprint_change_reconnects(
    manager: McpClientManager, factory: FakeFactory
) -> None:
    await manager.call_tool(
        _config(), sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    # Filter/TTL edits keep the connection …
    await manager.call_tool(
        _config(tool_exclude=("x",), idle_ttl_s=60.0),
        sandbox_url="http://sb:8088",
        tool="echo",
        arguments={},
    )
    assert len(factory.created) == 1
    # … a command/env edit is a new process: old closed, new dialed.
    await manager.call_tool(
        _config(env={"TOKEN": "y"}),
        sandbox_url="http://sb:8088",
        tool="echo",
        arguments={},
    )
    assert len(factory.created) == 2
    assert factory.created[0].closed


async def test_idle_ttl_evicts_and_reconnects_transparently(
    manager: McpClientManager, factory: FakeFactory, clock: FakeClock
) -> None:
    config = _config(idle_ttl_s=300.0)
    await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    clock.advance(299.0)
    assert await manager.sweep_idle() == 0  # not idle long enough
    assert not factory.created[0].closed
    clock.advance(2.0)
    assert await manager.sweep_idle() == 1  # past TTL — evicted
    assert factory.created[0].closed
    # Next use transparently reconnects.
    result = await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    assert result.text == "ran echo"
    assert len(factory.created) == 2


async def test_live_ttl_edit_applies_without_reconnect(
    manager: McpClientManager, factory: FakeFactory, clock: FakeClock
) -> None:
    await manager.call_tool(
        _config(idle_ttl_s=300.0), sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    # The operator shortens the TTL; same fingerprint so no reconnect …
    await manager.call_tool(
        _config(idle_ttl_s=60.0), sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    assert len(factory.created) == 1
    clock.advance(61.0)
    # … and the NEW ttl drives the sweep.
    assert await manager.sweep_idle() == 1


async def test_unavailable_call_evicts_poisoned_connection(
    manager: McpClientManager, factory: FakeFactory
) -> None:
    config = _config()
    await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    factory.created[0].next_error = McpUnavailableError("bridge lost")
    with pytest.raises(McpUnavailableError):
        await manager.call_tool(
            config, sandbox_url="http://sb:8088", tool="echo", arguments={}
        )
    assert factory.created[0].closed
    # Reconnect on next use.
    await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    assert len(factory.created) == 2


async def test_timeout_evicts_but_tool_error_does_not(
    manager: McpClientManager, factory: FakeFactory
) -> None:
    config = _config()
    await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    factory.created[0].next_error = McpToolError("unknown tool")
    with pytest.raises(McpToolError):
        await manager.call_tool(
            config, sandbox_url="http://sb:8088", tool="nope", arguments={}
        )
    assert not factory.created[0].closed  # protocol worked — stay connected
    factory.created[0].next_error = McpCallTimeoutError("too slow")
    with pytest.raises(McpCallTimeoutError):
        await manager.call_tool(
            config, sandbox_url="http://sb:8088", tool="echo", arguments={}
        )
    assert factory.created[0].closed  # orphaned request state — poisoned


async def test_dead_connection_replaced_on_acquire(
    manager: McpClientManager, factory: FakeFactory
) -> None:
    config = _config()
    await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    factory.created[0].dead = True  # server crashed while idle
    await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    assert len(factory.created) == 2


async def test_connect_failure_propagates_and_caches_nothing(
    clock: FakeClock,
) -> None:
    factory = FakeFactory(clock, fail_connect="connection refused")
    manager = McpClientManager(clock=clock, connection_factory=factory)
    config = _config()
    with pytest.raises(McpUnavailableError, match="connection refused"):
        await manager.call_tool(
            config, sandbox_url="http://sb:8088", tool="echo", arguments={}
        )
    assert factory.created[0].closed
    # A later attempt dials fresh (e.g. the server came back).
    factory.fail_connect = None
    await manager.call_tool(
        config, sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    assert len(factory.created) == 2


async def test_stdio_sandbox_url_is_part_of_identity(
    manager: McpClientManager, factory: FakeFactory
) -> None:
    config = _config()
    await manager.call_tool(
        config, sandbox_url="http://sb-a:8088", tool="echo", arguments={}
    )
    await manager.call_tool(
        config, sandbox_url="http://sb-b:8088", tool="echo", arguments={}
    )
    # Same server name, different sandbox → reconnected there (Phase-7 seam).
    assert len(factory.created) == 2
    assert factory.created[1].sandbox_url == "http://sb-b:8088"


async def test_aclose_closes_everything(
    manager: McpClientManager, factory: FakeFactory
) -> None:
    await manager.call_tool(
        _config(), sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    await manager.call_tool(
        _config(name="other"), sandbox_url="http://sb:8088", tool="echo", arguments={}
    )
    await manager.aclose()
    assert all(connection.closed for connection in factory.created)
