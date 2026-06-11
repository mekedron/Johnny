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
    """

    kind: str
    one_liner: str
    keywords: tuple[str, ...] = field(default=())


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
    near the model.
    """
    if not entries:
        return ""
    lines = [
        (
            "Delegatable task kinds — the ONLY kinds you may delegate. "
            "Choose action='delegate' only when the request needs real work "
            "in an external system (looking something up, taking an action) "
            "that matches one of the kinds below. If the request can be "
            "answered from the conversation, your own knowledge, or the "
            "context you were given, choose action='speak' instead — even "
            "when these topics come up. When unsure between speak and "
            "delegate, choose speak. With action='delegate', task.ack is "
            "required: write the acknowledgment yourself in the language the "
            "user spoke, naming the specific work you are starting and why "
            "it needs a moment — never a generic filler phrase. The kinds:"
        )
    ]
    lines.extend(f"- {entry.kind}: {entry.one_liner}" for entry in entries)
    return "\n".join(lines)


__all__ = [
    "STUB_TASK_CATALOG",
    "TaskCatalogEntry",
    "render_task_catalog",
]
