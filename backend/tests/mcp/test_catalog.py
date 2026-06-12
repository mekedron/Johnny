"""Unit tests: MCP catalog entries + known-kinds composition (Johnny-trt.36)."""

from __future__ import annotations

from johnny.agent.internal_tools import (
    executor_known_kinds,
    internal_catalog_entries,
    merge_task_catalog,
)
from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.mcp.catalog import (
    ONE_LINER_CAP_CHARS,
    McpServerSnapshot,
    McpToolInfo,
    mcp_catalog_entries,
    mcp_known_kinds,
)
from johnny.mcp.config import McpServerConfig


def _snapshot(
    *,
    name: str = "fixture",
    enabled: bool = True,
    tools: tuple[McpToolInfo, ...] = (
        McpToolInfo(name="echo", description="Echo a message."),
        McpToolInfo(name="add", description="Add two numbers."),
    ),
    probe_ok: bool | None = True,
    include: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] = (),
) -> McpServerSnapshot:
    return McpServerSnapshot(
        config=McpServerConfig(
            name=name,
            transport="stdio",
            command="python3",
            enabled=enabled,
            tool_include=include,
            tool_exclude=exclude,
        ),
        tools=tools,
        probe_ok=probe_ok,
    )


def test_entries_qualified_and_described() -> None:
    entries = mcp_catalog_entries([_snapshot()])
    assert [e.kind for e in entries] == ["mcp__fixture__echo", "mcp__fixture__add"]
    echo = entries[0]
    assert echo.one_liner == "Echo a message."
    assert echo.available
    assert echo.keywords == ()  # no trt.50 scorer hints in v1
    assert not echo.hidden


def test_disabled_server_contributes_nothing() -> None:
    snapshot = _snapshot(enabled=False)
    assert mcp_catalog_entries([snapshot]) == ()
    assert mcp_known_kinds([snapshot]) == frozenset()


def test_never_probed_server_contributes_nothing() -> None:
    snapshot = _snapshot(tools=(), probe_ok=None)
    assert mcp_catalog_entries([snapshot]) == ()


def test_filters_apply_at_read_time() -> None:
    entries = mcp_catalog_entries([_snapshot(exclude=("add",))])
    assert [e.kind for e in entries] == ["mcp__fixture__echo"]
    kinds = mcp_known_kinds([_snapshot(include=("add",))])
    assert kinds == frozenset({"mcp__fixture__add"})


def test_probe_failed_server_renders_unavailable_with_reason() -> None:
    entries = mcp_catalog_entries([_snapshot(probe_ok=False)])
    assert len(entries) == 2
    for entry in entries:
        assert not entry.available
        assert "fixture connector isn't reachable" in entry.unavailable_reason
        assert entry.keywords == ()
    # The kinds STAY known: the gate degrades to the spoken decline, and the
    # worker still attempts the lazy reconnect for a queued row.
    assert mcp_known_kinds([_snapshot(probe_ok=False)]) == {
        "mcp__fixture__echo",
        "mcp__fixture__add",
    }


def test_long_descriptions_first_line_capped() -> None:
    long_tool = McpToolInfo(name="t", description=("x" * 500) + "\nsecond line")
    entries = mcp_catalog_entries([_snapshot(tools=(long_tool,))])
    assert len(entries[0].one_liner) <= ONE_LINER_CAP_CHARS
    assert entries[0].one_liner.endswith("…")


def test_blank_description_gets_fallback_one_liner() -> None:
    entries = mcp_catalog_entries([_snapshot(tools=(McpToolInfo(name="t"),))])
    assert "t tool from the fixture connector" in entries[0].one_liner


# --------------------------------------------------------------------------- #
# Composition seams (internal_tools)                                           #
# --------------------------------------------------------------------------- #


def test_executor_known_kinds_includes_mcp() -> None:
    kinds = executor_known_kinds(["google-calendar"], mcp_kinds=["mcp__fixture__echo"])
    assert "google-calendar" in kinds
    assert "mcp__fixture__echo" in kinds
    assert "meeting.leave" in kinds  # internal tools always present


def test_merge_task_catalog_resolution_order_wins() -> None:
    internal = internal_catalog_entries(meeting_backed=True)
    skill = TaskCatalogEntry(kind="dup-kind", one_liner="the skill")
    mcp_dup = TaskCatalogEntry(kind="dup-kind", one_liner="the mcp tool")
    mcp_new = TaskCatalogEntry(kind="mcp__s__t", one_liner="fresh")
    merged = merge_task_catalog(internal, (skill,), (mcp_dup, mcp_new))
    by_kind = {entry.kind: entry for entry in merged}
    # The duplicate keeps the SKILL entry (the executor dispatches skills
    # before the MCP leg), the fresh MCP kind lands.
    assert by_kind["dup-kind"].one_liner == "the skill"
    assert by_kind["mcp__s__t"].one_liner == "fresh"


def test_merge_task_catalog_drops_internal_shadow_from_mcp() -> None:
    internal = internal_catalog_entries(meeting_backed=True)
    shadow = TaskCatalogEntry(kind="meeting.leave", one_liner="impostor")
    merged = merge_task_catalog(internal, (), (shadow,))
    impostors = [e for e in merged if e.one_liner == "impostor"]
    assert impostors == []
