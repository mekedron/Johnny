"""MCP-contributed task-catalog entries (Johnny-trt.36, stdlib-only).

Session assembly (:func:`johnny.agent.job_session.build_agent_runtime`)
merges these alongside the internal tools and the skill loader's entries —
the third catalog source. The input is the DB's *cached* view of each
server (the last successful probe's tool list + the latest probe verdict):
assembly never connects to MCP servers — connecting is the worker's lazy,
claim-time job (and the probe endpoint's explicit one). A server that has
never been probed contributes nothing (we cannot advertise tools we have
never seen); the trt.37 management flow is add → probe → use.

Availability (Johnny-trt.55): a server whose *latest* probe failed keeps
contributing its cached tools, but ``available=False`` with a spoken-form
reason — the router declines honestly instead of the tools silently
vanishing (the bead's capability-awareness note). ``keywords`` stay empty
either way: MCP tools carry no trt.50 scorer hints in v1.
"""

from __future__ import annotations

from dataclasses import dataclass

from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.mcp.config import McpServerConfig, qualified_tool_name

ONE_LINER_CAP_CHARS = 160
"""Same prompt-budget cap the skill registry applies to its one-liners."""


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    """One tool as the last successful probe reported it (cached in the DB row).

    ``input_schema`` is the tool's JSON-Schema for its arguments, as the server
    reported it (``tools/list`` ``inputSchema``). It is populated only on the
    LIVE listing path (:func:`johnny.mcp.client._list_all_tools` →
    ``list_mcp_tools``), so the answer model can compose correct ``call_mcp_tool``
    arguments; the cached catalog path leaves it ``None`` (the trt.36 catalog and
    the ``.mcp-state.json`` tool cache never stored schemas — only name +
    description). Kept out of equality is unnecessary: nothing hashes this value.
    """

    name: str
    description: str = ""
    input_schema: dict | None = None


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    """One server's config + cached probe state, as catalog assembly sees it.

    ``tools`` is the *unfiltered* cached list (the row stores everything the
    server reported; filters apply at read time so editing them never needs
    a re-probe). ``probe_ok=None`` means never probed.
    """

    config: McpServerConfig
    tools: tuple[McpToolInfo, ...] = ()
    probe_ok: bool | None = None
    probe_error: str = ""

    def filtered_tools(self) -> tuple[McpToolInfo, ...]:
        kept_names = set(self.config.filtered_tool_names([t.name for t in self.tools]))
        return tuple(t for t in self.tools if t.name in kept_names)


def _one_liner(server: str, tool: McpToolInfo) -> str:
    """First line of the tool's description, prompt-budget capped."""
    described = tool.description.strip()
    first_line = described.splitlines()[0].strip() if described else ""
    text = first_line or f"Use the {tool.name} tool from the {server} connector."
    if len(text) > ONE_LINER_CAP_CHARS:
        text = text[: ONE_LINER_CAP_CHARS - 1].rstrip() + "…"
    return text


def unreachable_reason(server: str) -> str:
    """The spoken-form unavailable reason for a server whose last probe failed."""
    return f"the {server} connector isn't reachable right now"


def mcp_catalog_entries(
    snapshots: tuple[McpServerSnapshot, ...] | list[McpServerSnapshot],
) -> tuple[TaskCatalogEntry, ...]:
    """Catalog entries for every enabled server's cached, filter-surviving tools."""
    entries: list[TaskCatalogEntry] = []
    for snapshot in snapshots:
        config = snapshot.config
        if not config.enabled:
            continue
        reachable = snapshot.probe_ok is not False
        for tool in snapshot.filtered_tools():
            entries.append(
                TaskCatalogEntry(
                    kind=qualified_tool_name(config.name, tool.name),
                    one_liner=_one_liner(config.name, tool),
                    keywords=(),
                    available=reachable,
                    unavailable_reason=(
                        "" if reachable else unreachable_reason(config.name)
                    ),
                )
            )
    return tuple(entries)


def mcp_known_kinds(
    snapshots: tuple[McpServerSnapshot, ...] | list[McpServerSnapshot],
) -> frozenset[str]:
    """The qualified kinds the executor chain can resolve (gate pre-ack set).

    Probe-failed servers stay IN: their catalog entries render unavailable,
    so the gate degrades a delegate verdict to the spoken decline (reason
    included) rather than the unknown-kind leg — and the worker still
    attempts a lazy reconnect if a row does get queued (the server may have
    recovered since the probe).
    """
    kinds: set[str] = set()
    for snapshot in snapshots:
        config = snapshot.config
        if not config.enabled:
            continue
        for tool in snapshot.filtered_tools():
            kinds.add(qualified_tool_name(config.name, tool.name))
    return frozenset(kinds)


__all__ = [
    "McpServerSnapshot",
    "McpToolInfo",
    "ONE_LINER_CAP_CHARS",
    "mcp_catalog_entries",
    "mcp_known_kinds",
    "unreachable_reason",
]
