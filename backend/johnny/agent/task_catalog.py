"""Delegatable-task catalog for the Phase-3 triage router (Johnny-trt.19).

The triage router can only emit a useful ``delegate`` verdict if it knows
*what* can be delegated. This module is the catalog interface: a tuple of
:class:`TaskCatalogEntry` rows (kind + one-liner + optional keywords) that

* :meth:`johnny.agent.router_gate.RouterGate._router_messages` renders into
  the router system prompt (kind + one-liner only — the model picks
  ``task.kind`` from this list), and
* Johnny-trt.50's heuristic complexity scorer reads as its dynamic
  *delegate-prior* keyword dimension (``keywords`` — deliberately NOT
  rendered into the prompt; no heuristic content enters the prompt in
  Phase 3).

Phase 3 ships :data:`STUB_TASK_CATALOG` — hand-written entries for the kinds
the epic builds first. Phase 4 (Johnny-trt.23) replaces the *source* with the
skill-frontmatter loader (name + description of eligible SKILL.md packages)
plus MCP-derived tools; the ``(kind, one_liner, keywords)`` shape here is the
contract that loader fills, so the gate and the scorer plug in unchanged.

Stdlib-only and import-cheap, like :mod:`johnny.agent.gate` /
:mod:`johnny.agent.tasks` — the scorer (a pure-stdlib module) must be able to
import the entry type without pulling livekit or the provider stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TaskCatalogEntry:
    """One delegatable task kind the router may target with ``action='delegate'``.

    ``kind`` is the identifier the router puts in ``task.kind`` (and the
    ``agent_tasks.kind`` column carries; the executor dispatches on it).
    ``one_liner`` is the single-line capability description rendered next to
    the kind in the router prompt — keep it short and imperative-neutral
    ("Look up …", "Search …"), it is model guidance, not marketing.
    ``keywords`` are trigger words for Johnny-trt.50's heuristic
    catalog-keyword dimension (the delegate prior); they never reach the
    prompt. English here — the scorer owns its own multilingual sets.

    ``available`` / ``unavailable_reason`` (Johnny-trt.55): a kind whose
    capability THIS session lacks (no credentials, missing configuration, the
    wrong surface) is carried ``available=False`` with a short, spoken-form,
    actionable reason — rendered so the router can decline honestly and name
    the fix, and read by the gate's delegate backstop. Catalog ASSEMBLY must
    give unavailable entries ``keywords=()`` so the trt.50 delegate prior
    never fires for work the session cannot do (the scorer reads entries
    verbatim and deliberately does not re-check this flag).
    """

    kind: str
    one_liner: str
    keywords: tuple[str, ...] = field(default=())
    available: bool = True
    unavailable_reason: str = ""


STUB_TASK_CATALOG: tuple[TaskCatalogEntry, ...] = (
    TaskCatalogEntry(
        kind="calendar.upcoming_events",
        one_liner="Look up upcoming events on the connected calendar.",
        keywords=(
            "calendar",
            "schedule",
            "meeting",
            "meetings",
            "event",
            "events",
            "appointment",
            "agenda",
            "free slot",
            "availability",
        ),
    ),
    TaskCatalogEntry(
        kind="gmail.search",
        one_liner="Search the connected mailbox for messages.",
        keywords=(
            "email",
            "emails",
            "mail",
            "inbox",
            "gmail",
            "message from",
            "unread",
        ),
    ),
)
"""The Phase-3 stub entries (Johnny-trt.19) — replaced as a *source* by the
Phase-4 skill loader (Johnny-trt.23), kept as the interface's reference shape.

Kinds are the epic's first real executors (``calendar.upcoming_events`` is
Phase 4's first tool; Gmail follows), so the router learns the real
vocabulary now. Until those executors land, a delegated task is settled
``failed`` *fast* by :func:`johnny.agent.tasks.stub_executor` with
speech-ready text — the ack is honest ("queued, then reported as not
workable"), never a dead promise.
"""


_UNAVAILABLE_RENDER_CAP = 5
"""Most unavailable entries rendered with their reason (Johnny-trt.55). The
rest collapse into one summary count line so a long tail of broken
integrations cannot bloat the router prompt — the openclaw 150-skill
precedent applied to the unavailable block."""

_UNAVAILABLE_REASON_CAP = 160
"""Per-entry cap on the rendered reason — the loader's one-liner discipline
applied to the spoken-form gap copy."""


def render_task_catalog(entries: tuple[TaskCatalogEntry, ...]) -> str:
    """Render the catalog block for the router system prompt.

    One header paragraph framing *when* to delegate, then ``- kind: one-liner``
    rows. Returns ``""`` for an empty catalog so callers can append the
    result unconditionally — an empty catalog leaves the prompt
    byte-identical to the pre-trt.19 build (the replay-parity stance: the
    catalog is additive prompt context, off when nothing is delegatable).
    Keywords are deliberately not rendered (they feed the trt.50 scorer
    only).

    The header carries the Johnny-trt.53 restraint + ack contract (the live
    over-delegation fix): prefer ``speak`` whenever the request is answerable
    from context, delegate only the listed kinds, and author the ``task.ack``
    fresh per turn in the user's language — no canned filler example anywhere
    near the model. It also teaches ``status`` for asks about already-started
    work or its outcome (Johnny-trt.29): the gate answers those from the real
    task registry, which beats letting the answer model improvise a result it
    never saw (the live session-4 hallucination).

    Unavailable entries (Johnny-trt.55) render in a *second* block teaching
    the honest decline: never delegate these, answer with the reason and the
    fix instead. The block is bounded (:data:`_UNAVAILABLE_RENDER_CAP`
    entries with reasons, the rest summarized as a count;
    :data:`_UNAVAILABLE_REASON_CAP` chars per reason) so capability gaps can
    never bloat the prompt. A catalog whose entries are ALL available renders
    byte-identical to the pre-trt.55 build — replay parity by construction.
    """
    if not entries:
        return ""
    available = tuple(entry for entry in entries if entry.available)
    unavailable = tuple(entry for entry in entries if not entry.available)
    blocks: list[str] = []
    if available:
        lines = [
            (
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
            )
        ]
        lines.extend(f"- {entry.kind}: {entry.one_liner}" for entry in available)
        blocks.append("\n".join(lines))
    if unavailable:
        blocks.append(_render_unavailable(unavailable, none_available=not available))
    return "\n\n".join(blocks)


def _unavailable_rows(unavailable: tuple[TaskCatalogEntry, ...]) -> list[str]:
    """The capped ``- kind: reason`` rows shared by both unavailable renderers."""
    lines: list[str] = []
    for entry in unavailable[:_UNAVAILABLE_RENDER_CAP]:
        reason = entry.unavailable_reason.strip() or "not available in this session right now"
        if len(reason) > _UNAVAILABLE_REASON_CAP:
            reason = reason[: _UNAVAILABLE_REASON_CAP - 1].rstrip() + "…"
        lines.append(f"- {entry.kind}: {reason}")
    overflow = len(unavailable) - _UNAVAILABLE_RENDER_CAP
    if overflow > 0:
        lines.append(
            f"- …and {overflow} more unavailable kinds — decline those the same way."
        )
    return lines


def _render_unavailable(
    unavailable: tuple[TaskCatalogEntry, ...], *, none_available: bool
) -> str:
    """The capability-gap block (Johnny-trt.55): decline honestly, name the fix.

    ``none_available`` prepends the explicit no-delegation sentence for a
    session whose every catalog kind is unavailable — without it the model
    would see reasons but no rule about ``action='delegate'`` being off the
    table entirely.
    """
    header = (
        "Capabilities NOT available in this session — delegating these is "
        "impossible, so never choose them for action='delegate'. If the user "
        "asks for one, choose action='speak' and decline honestly in the "
        "user's language: say plainly that you cannot do it right now, give "
        "the reason listed below, and tell them the fix it names. Never "
        "pretend to check, never promise to try later:"
    )
    if none_available:
        header = (
            "There are NO delegatable task kinds in this session — never "
            "choose action='delegate'. " + header
        )
    return "\n".join([header, *_unavailable_rows(unavailable)])


def render_capability_notes(entries: tuple[TaskCatalogEntry, ...]) -> str:
    """The ANSWER-prompt honesty block for unavailable capabilities (Johnny-trt.55).

    The router's unavailable block (:func:`render_task_catalog`) keeps the
    triage model from delegating an impossible kind — but an unavailable ask
    the router (correctly) routes to ``speak`` is then answered by the
    *answer* model, which never sees the catalog. Without this note the
    answer model improvises a pretend-check ("let me look — give me a sec"),
    the exact failure the bead removes. Rendered into the agent's persistent
    system prompt by :func:`johnny.agent.session.build_agent_instructions`;
    returns ``""`` when every entry is available so the no-gaps prompt stays
    byte-identical (the replay-parity stance). Same rows and caps as the
    router block — one source of truth for the spoken reasons.
    """
    unavailable = tuple(entry for entry in entries if not entry.available)
    if not unavailable:
        return ""
    header = (
        "Things you CANNOT do in this session right now. If asked for one of "
        "these, say so plainly in the user's language — give the reason below "
        "and tell them the fix it names. Never pretend to check, never "
        "promise to do it later, never invent results:"
    )
    return "\n".join([header, *_unavailable_rows(unavailable)])


__all__ = [
    "STUB_TASK_CATALOG",
    "TaskCatalogEntry",
    "render_capability_notes",
    "render_task_catalog",
]
