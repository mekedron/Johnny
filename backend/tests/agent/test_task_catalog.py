"""Unit tests for the Phase-3 task catalog (Johnny-trt.19).

The catalog is the delegate-vocabulary interface: the router prompt renders
``(kind, one_liner)`` and the trt.50 heuristic scorer reads ``keywords`` —
so the tests pin the rendered block's shape (a snapshot the gate prompt test
builds on) and the interface guarantees the scorer relies on.

Stdlib-only module — no ``importorskip`` needed (mirrors test_gate / test_tasks).
"""

from __future__ import annotations

from johnny.agent.task_catalog import (
    STUB_TASK_CATALOG,
    TaskCatalogEntry,
    render_task_catalog,
)


def test_render_empty_catalog_is_empty_string() -> None:
    """Empty catalog ⇒ '' so the prompt stays byte-identical to pre-trt.19."""
    assert render_task_catalog(()) == ""


def test_render_snapshot_two_entries() -> None:
    entries = (
        TaskCatalogEntry(kind="calendar.upcoming_events", one_liner="Look up events."),
        TaskCatalogEntry(kind="gmail.search", one_liner="Search the mailbox."),
    )

    rendered = render_task_catalog(entries)

    expected = (
        "Delegatable task kinds — when the request needs real work done "
        "(looking something up in an external system, taking an action) "
        "rather than a conversational answer from context, choose "
        "action='delegate' and set task.kind to one of these, with a "
        "short spoken acknowledgment in task.ack:"
        "\n- calendar.upcoming_events: Look up events."
        "\n- gmail.search: Search the mailbox."
    )
    assert rendered == expected


def test_render_never_leaks_keywords() -> None:
    """Keywords feed the trt.50 scorer only — no heuristic content in the prompt."""
    entries = (
        TaskCatalogEntry(
            kind="calendar.upcoming_events",
            one_liner="Look up events.",
            keywords=("calendar", "agenda"),
        ),
    )
    rendered = render_task_catalog(entries)
    assert "agenda" not in rendered
    assert "keywords" not in rendered


def test_stub_catalog_entries_are_prompt_and_scorer_ready() -> None:
    """The Phase-3 stubs satisfy both consumers' contracts."""
    assert STUB_TASK_CATALOG  # non-empty — the router gets real guidance
    kinds = [entry.kind for entry in STUB_TASK_CATALOG]
    assert len(kinds) == len(set(kinds))  # no duplicate kinds
    for entry in STUB_TASK_CATALOG:
        # Prompt side: single-line, non-empty, no stray whitespace.
        assert entry.kind and entry.kind == entry.kind.strip()
        assert entry.one_liner and "\n" not in entry.one_liner
        # Scorer side (trt.50): keywords is a non-empty tuple of non-empty strs.
        assert isinstance(entry.keywords, tuple)
        assert entry.keywords
        assert all(kw and kw == kw.strip() for kw in entry.keywords)


def test_stub_catalog_carries_the_phase4_first_tool() -> None:
    """calendar.upcoming_events is the epic's first real executor (trt.23)."""
    assert any(e.kind == "calendar.upcoming_events" for e in STUB_TASK_CATALOG)
