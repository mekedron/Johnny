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
    render_capability_notes,
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
        "Delegatable task kinds — the ONLY kinds you may delegate. "
        "Choose action='delegate' only when the request needs real work "
        "in an external system (looking something up, taking an action) "
        "that matches one of the kinds below. If the request can be "
        "answered from the conversation, your own knowledge, or the "
        "context you were given, choose action='speak' instead — even "
        "when these topics come up. When unsure between speak and "
        "delegate, choose speak. If they ask about work already "
        "underway or what it found ('are you still working on it?', "
        "'what did the check turn up?'), choose action='status' — the "
        "real task registry is read out; never invent the result. "
        "With action='delegate', task.ack is "
        "required: write the acknowledgment yourself in the language the "
        "user spoke, naming the specific work you are starting and why "
        "it needs a moment — never a generic filler phrase. The kinds:"
        "\n- calendar.upcoming_events: Look up events."
        "\n- gmail.search: Search the mailbox."
    )
    assert rendered == expected


def test_render_header_carries_restraint_and_ack_contract() -> None:
    """Johnny-trt.53: the header must steer the model away from over-delegating
    (answerable-from-context ⇒ speak, unsure ⇒ speak, catalog kinds only) and
    demand a per-request, user-language ack — with no canned example to copy."""
    entries = (TaskCatalogEntry(kind="calendar.upcoming_events", one_liner="Look up events."),)

    rendered = render_task_catalog(entries)

    assert "ONLY kinds you may delegate" in rendered
    assert "choose action='speak' instead" in rendered
    assert "When unsure between speak and delegate, choose speak." in rendered
    assert "task.ack is required" in rendered
    assert "language the user spoke" in rendered
    # The trt.29 status rule: asks about started work / its outcome route to
    # the registry render, never to an improvised answer.
    assert "choose action='status'" in rendered
    assert "never invent the result" in rendered
    # No filler phrase anywhere near the model (the trt.53 copy-priming bug).
    assert "Let me check" not in rendered


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


# --- availability rendering (Johnny-trt.55) ------------------------------------


def test_all_available_renders_byte_identical_to_pre_trt55() -> None:
    """Replay parity by construction: entries defaulting available=True render
    exactly the pre-trt.55 block (the snapshot test above pins the bytes; this
    pins that explicit available=True changes nothing either)."""
    legacy = (TaskCatalogEntry(kind="a.b", one_liner="Do a thing."),)
    explicit = (
        TaskCatalogEntry(kind="a.b", one_liner="Do a thing.", available=True),
    )
    assert render_task_catalog(legacy) == render_task_catalog(explicit)
    assert "NOT available" not in render_task_catalog(legacy)


def test_mixed_availability_renders_two_blocks() -> None:
    entries = (
        TaskCatalogEntry(kind="session.end", one_liner="End the session."),
        TaskCatalogEntry(
            kind="google-calendar",
            one_liner="Look up events.",
            available=False,
            unavailable_reason="no Google account is connected — link one in settings.",
        ),
    )

    rendered = render_task_catalog(entries)

    # Available block first, unchanged shape.
    assert "- session.end: End the session." in rendered
    # Unavailable block teaches the honest decline with the reason verbatim.
    assert "Capabilities NOT available in this session" in rendered
    assert "- google-calendar: no Google account is connected — link one in settings." in rendered
    assert "Never pretend to check" in rendered
    # The unavailable one-liner is NOT rendered (the reason replaces it).
    assert "Look up events." not in rendered
    # Two blocks, joined like the gate joins prompt sections.
    assert "\n\n" in rendered


def test_only_unavailable_entries_render_explicit_no_delegation_rule() -> None:
    entries = (
        TaskCatalogEntry(
            kind="google-calendar",
            one_liner="Look up events.",
            available=False,
            unavailable_reason="no Google account is connected.",
        ),
    )
    rendered = render_task_catalog(entries)
    assert rendered.startswith("There are NO delegatable task kinds in this session")
    assert "never choose action='delegate'" in rendered
    assert "- google-calendar: no Google account is connected." in rendered
    # The available-kinds header must be absent (nothing is delegatable).
    assert "The kinds:" not in rendered


def test_unavailable_block_is_capped_against_prompt_bloat() -> None:
    """Render cap (trt.55): at most 5 reason-carrying rows; the rest collapse
    into one count line; reasons are clipped to 160 chars."""
    entries = tuple(
        TaskCatalogEntry(
            kind=f"kind.{i:02d}",
            one_liner="x",
            available=False,
            unavailable_reason=f"reason {i:02d} " + "verbose " * 40,
        )
        for i in range(12)
    )

    rendered = render_task_catalog(entries)
    lines = rendered.splitlines()

    reason_rows = [line for line in lines if line.startswith("- kind.")]
    assert len(reason_rows) == 5  # capped
    assert "…and 7 more unavailable kinds" in rendered
    for row in reason_rows:
        _, _, reason = row.partition(": ")
        assert len(reason) <= 160
        assert reason.endswith("…")
    # The whole block stays bounded well under the ~2K budget.
    assert len(rendered) < 2000


def test_unavailable_blank_reason_gets_generic_copy() -> None:
    entries = (
        TaskCatalogEntry(kind="x.y", one_liner="x", available=False),
    )
    rendered = render_task_catalog(entries)
    assert "- x.y: not available in this session right now" in rendered


def test_capability_notes_empty_only_without_user_facing_caps_or_gaps() -> None:
    """Byte-identical empty when the session has neither a user-facing
    available capability nor a gap (Johnny-etu.7)."""
    # No catalog at all (non-delegation mode).
    assert render_capability_notes(()) == ""
    # Only internal session-control kinds, all available: not user-facing, no
    # gap → still empty (advertising "you can end the session" is noise).
    internal_only = (
        TaskCatalogEntry(kind="session.end", one_liner="End the session.", internal=True),
        TaskCatalogEntry(kind="meeting.leave", one_liner="Leave the meeting.", internal=True),
    )
    assert render_capability_notes(internal_only) == ""


def test_capability_notes_positive_block_grounds_available_capability() -> None:
    """The Johnny-etu.7 fix: an available, user-facing capability is grounded
    as a background-tool request so the answer model never denies it ('wrong
    sandbox') AND never fabricates a result for it on a speak turn."""
    notes = render_capability_notes(STUB_TASK_CATALOG)  # all available, non-internal
    assert "handled for you by background tools" in notes
    assert "- calendar.upcoming_events: Look up upcoming events on the connected calendar." in notes
    assert "- gmail.search: Search the connected mailbox for messages." in notes
    # The two guardrails: never deny an available capability, never invent its result.
    assert "never tell them you can't do" in notes
    assert "never state or guess any specifics" in notes
    # No gap → no CANNOT block, and never router vocabulary.
    assert "CANNOT do" not in notes
    assert "delegate" not in notes


def test_capability_notes_excludes_internal_kinds_from_positive_block() -> None:
    """Internal session-control kinds are not user-facing capabilities — the
    answer model is never told 'you can end the session' (Johnny-etu.7)."""
    entries = (
        TaskCatalogEntry(kind="session.end", one_liner="End the session.", internal=True),
        TaskCatalogEntry(kind="google-calendar", one_liner="Look up events."),
    )
    notes = render_capability_notes(entries)
    assert "handled for you by background tools" in notes
    assert "- google-calendar: Look up events." in notes
    assert "session.end" not in notes
    assert "End the session." not in notes


def test_capability_notes_render_both_blocks_positive_before_negative() -> None:
    """A session with both a working and a broken capability grounds the
    answer model on each: the background-tool block first, then the decline."""
    entries = (
        TaskCatalogEntry(kind="google-calendar", one_liner="Look up events."),
        TaskCatalogEntry(
            kind="gmail.search",
            one_liner="Search the mailbox.",
            available=False,
            unavailable_reason="no Google account is connected — link one in settings.",
        ),
    )
    notes = render_capability_notes(entries)
    assert "- google-calendar: Look up events." in notes
    assert "- gmail.search: no Google account is connected — link one in settings." in notes
    # Positive grounding leads, the gap honesty follows.
    assert notes.index("background tools") < notes.index("CANNOT do in this session")


def test_capability_notes_positive_block_is_capped_against_prompt_bloat() -> None:
    """A skill-rich workspace collapses the tail into a count line (the
    openclaw 150-skill precedent applied to the CAN-do block)."""
    entries = tuple(
        TaskCatalogEntry(kind=f"skill-{i}", one_liner=f"Do thing {i}.")
        for i in range(20)
    )
    notes = render_capability_notes(entries)
    assert "- skill-0: Do thing 0." in notes
    assert "- skill-11: Do thing 11." in notes
    assert "- skill-12: Do thing 12." not in notes  # past the 12-row cap
    assert "…and 8 more you can do" in notes


def test_capability_notes_carry_reasons_without_router_vocabulary() -> None:
    """The answer-side block speaks the same reasons but knows nothing about
    router actions — 'delegate' is triage vocabulary, not answer vocabulary."""
    entries = (
        TaskCatalogEntry(kind="session.end", one_liner="End the session.", internal=True),
        TaskCatalogEntry(
            kind="google-calendar",
            one_liner="Look up events.",
            available=False,
            unavailable_reason="no Google account is connected — link one in settings.",
        ),
    )
    notes = render_capability_notes(entries)
    assert "CANNOT do in this session" in notes
    assert "- google-calendar: no Google account is connected — link one in settings." in notes
    assert "Never pretend to check" in notes
    assert "delegate" not in notes
    assert "session.end" not in notes  # internal kinds are not the answer model's business


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
