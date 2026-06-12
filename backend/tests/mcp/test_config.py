"""Unit tests: MCP naming contract, tool filters, config validation (Johnny-trt.36)."""

from __future__ import annotations

import pytest

from johnny.mcp.config import (
    McpConfigError,
    McpServerConfig,
    filter_tool_names,
    is_mcp_kind,
    parse_qualified_tool_name,
    qualified_tool_name,
)


def _stdio(name: str = "fixture", **overrides: object) -> McpServerConfig:
    base: dict[str, object] = {
        "name": name,
        "transport": "stdio",
        "command": "python3",
        "args": ("/opt/sandbox/mcp_fixture_server.py",),
    }
    base.update(overrides)
    return McpServerConfig(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Naming                                                                       #
# --------------------------------------------------------------------------- #


def test_qualified_name_round_trips() -> None:
    kind = qualified_tool_name("github", "create_issue")
    assert kind == "mcp__github__create_issue"
    assert parse_qualified_tool_name(kind) == ("github", "create_issue")
    assert is_mcp_kind(kind)


def test_tool_names_with_double_underscores_survive() -> None:
    # Server names cannot contain underscores, so the FIRST `__` after the
    # prefix is always the separator — even when the tool name has its own.
    kind = qualified_tool_name("my-server", "ns__weird__tool")
    assert parse_qualified_tool_name(kind) == ("my-server", "ns__weird__tool")


@pytest.mark.parametrize(
    "kind",
    [
        "google-calendar",  # a skill kind
        "meeting.leave",  # an internal kind
        "mcp__",  # no parts
        "mcp__server",  # no separator/tool
        "mcp__server__",  # empty tool
        "mcp____tool",  # empty server
        "MCP__server__tool",  # case-sensitive prefix
    ],
)
def test_non_mcp_shapes_do_not_parse(kind: str) -> None:
    assert parse_qualified_tool_name(kind) is None
    assert not is_mcp_kind(kind)


# --------------------------------------------------------------------------- #
# Filters                                                                      #
# --------------------------------------------------------------------------- #


def test_filter_include_none_admits_everything() -> None:
    assert filter_tool_names(["a", "b"], include=None, exclude=()) == ("a", "b")


def test_filter_include_empty_admits_nothing() -> None:
    assert filter_tool_names(["a", "b"], include=(), exclude=()) == ()


def test_filter_globs_and_exclude_wins() -> None:
    names = ["search", "send_message", "delete_channel", "send_file"]
    kept = filter_tool_names(names, include=("send_*", "search"), exclude=("send_file",))
    assert kept == ("search", "send_message")


def test_config_filter_helpers() -> None:
    config = _stdio(tool_include=("echo", "add"), tool_exclude=("add",))
    assert config.filtered_tool_names(["echo", "add", "always-fail"]) == ("echo",)
    assert config.allows_tool("echo")
    assert not config.allows_tool("add")  # exclude wins over include
    assert not config.allows_tool("always-fail")  # not included


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #


def test_stdio_config_valid_and_argv() -> None:
    config = _stdio()
    assert config.argv == ("python3", "/opt/sandbox/mcp_fixture_server.py")


@pytest.mark.parametrize(
    "name",
    ["UPPER", "has_underscore", "-leading-hyphen", "", "a" * 65, "with space"],
)
def test_bad_server_names_rejected(name: str) -> None:
    with pytest.raises(McpConfigError, match="server name"):
        _stdio(name=name)


def test_stdio_requires_command_and_forbids_url() -> None:
    with pytest.raises(McpConfigError, match="command"):
        _stdio(command="")
    with pytest.raises(McpConfigError, match="url"):
        _stdio(url="http://example.com")


def test_http_requires_url_and_forbids_command() -> None:
    config = McpServerConfig(name="remote", transport="http", url="https://x.test/mcp")
    assert config.url == "https://x.test/mcp"
    with pytest.raises(McpConfigError, match="url"):
        McpServerConfig(name="remote", transport="http")
    with pytest.raises(McpConfigError, match="http"):
        McpServerConfig(name="remote", transport="http", url="ftp://x.test")
    with pytest.raises(McpConfigError, match="command"):
        McpServerConfig(
            name="remote", transport="http", url="https://x.test", command="python3"
        )


def test_unknown_transport_rejected() -> None:
    with pytest.raises(McpConfigError, match="transport"):
        McpServerConfig(name="x", transport="websocket", url="https://x.test")


def test_timeouts_clamped() -> None:
    config = _stdio(connect_timeout_s=0.01, call_timeout_s=10_000.0, idle_ttl_s=1.0)
    assert config.connect_timeout_s == 1.0
    assert config.call_timeout_s == 600.0
    assert config.idle_ttl_s == 10.0


# --------------------------------------------------------------------------- #
# Connection fingerprint                                                       #
# --------------------------------------------------------------------------- #


def test_fingerprint_tracks_connection_shaping_fields_only() -> None:
    base = _stdio()
    assert base.connection_fingerprint() == _stdio().connection_fingerprint()
    # Filter / timeout / TTL / enabled edits apply live — same fingerprint.
    same = _stdio(tool_exclude=("x",), call_timeout_s=120.0, enabled=False)
    assert same.connection_fingerprint() == base.connection_fingerprint()
    # Command / args / env changes are a different process — new fingerprint.
    assert _stdio(args=()).connection_fingerprint() != base.connection_fingerprint()
    assert (
        _stdio(env={"TOKEN": "x"}).connection_fingerprint()
        != base.connection_fingerprint()
    )
