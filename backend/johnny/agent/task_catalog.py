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

    ``hidden`` (Johnny-trt.38): a POLICY-denied kind is carried
    ``hidden=True, available=False`` — both renderers skip it entirely (the
    canonical least-privilege scenario: a denied capability is never even
    *mentioned* in any prompt), but the entry stays in the catalog tuple so
    the gate's unavailable backstop still degrades a forced delegate to the
    spoken decline. ``policy_layer`` / ``policy_rule`` carry the deciding
    layer for the ``policy_denied`` observability event — machine fields,
    never rendered.

    ``internal`` (Johnny-etu.7): an internal session-control kind
    (``meeting.leave`` / ``session.end``) rather than a user-facing capability.
    Set by :func:`johnny.agent.internal_tools.InternalToolSpec.catalog_entry`;
    skill- and MCP-backed entries leave it ``False``. The answer-prompt
    positive block (:func:`render_capability_notes`) renders only NON-internal
    available kinds — "you can check the calendar" grounds the answer model,
    but advertising "you can end the session" as a capability is noise (its
    one-liner is router-facing guidance, and the user never asks the answer
    model to run it). The router catalog and the gate backstop still see
    internal entries unchanged.
    """

    kind: str
    one_liner: str
    keywords: tuple[str, ...] = field(default=())
    available: bool = True
    unavailable_reason: str = ""
    hidden: bool = False
    internal: bool = False
    policy_layer: str = ""
    policy_rule: str = ""


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

_AVAILABLE_RENDER_CAP = 12
"""Most available capabilities rendered in the answer-prompt positive block
(Johnny-etu.7). A long tail collapses into one summary count line so a
skill-rich workspace cannot bloat the answer prompt — the openclaw 150-skill
precedent applied to the available block, mirroring :data:`_UNAVAILABLE_RENDER_CAP`."""


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
    # Policy-hidden entries (Johnny-trt.38) render NOWHERE — not in the
    # delegatable block, not in the unavailable block: the canonical
    # least-privilege scenario forbids the prompt from even naming a
    # policy-denied kind. The gate's backstop still sees them in the tuple.
    visible = tuple(entry for entry in entries if not entry.hidden)
    if not visible:
        return ""
    available = tuple(entry for entry in visible if entry.available)
    unavailable = tuple(entry for entry in visible if not entry.available)
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


def _available_rows(available: tuple[TaskCatalogEntry, ...]) -> list[str]:
    """The capped ``- kind: one-liner`` rows for the positive capability block."""
    lines = [
        f"- {entry.kind}: {entry.one_liner}"
        for entry in available[:_AVAILABLE_RENDER_CAP]
    ]
    overflow = len(available) - _AVAILABLE_RENDER_CAP
    if overflow > 0:
        lines.append(f"- …and {overflow} more you can do — treat those the same way.")
    return lines


def render_capability_notes(entries: tuple[TaskCatalogEntry, ...]) -> str:
    """The ANSWER-prompt capability-grounding block (Johnny-trt.55 + Johnny-etu.7).

    The answer model never sees the router catalog, so on a capability ask the
    router (correctly) routes to ``speak`` it answers with no idea what this
    session can or cannot do. Two failures follow:

    * an UNAVAILABLE ask → the model improvises a pretend-check ("let me
      look — give me a sec"), the Johnny-trt.55 failure; and
    * an AVAILABLE ask → with no POSITIVE signal that the capability exists,
      the weak answer model overgeneralizes nearby gap copy into a blanket
      denial ("we're in the wrong sandbox, I can't check the calendar") —
      the Johnny-etu.7 fabrication, even though the real result was e.g. "No
      events found for the upcoming week."

    So this renders TWO blocks. First the available, NON-internal kinds, framed
    as BACKGROUND-TOOL requests the model does not answer itself — two rules:
    never deny one (the anti-"wrong sandbox" grounding), and, because no result
    exists yet on a speak turn, never state or invent specifics either. The
    framing leads with the no-invention rule on purpose: a bare "you CAN check
    the calendar" line invites a weak model to role-play the lookup and
    fabricate events (seen live on llama3.2:3b), so the block frames the work as
    the tool's, not the model's — the trt.28/0qw deliverer and the per-turn
    answer-context injection carry the REAL result once delegation fires
    (Johnny-etu.6). Then what the session CANNOT do (the trt.55 honesty block —
    decline with the reason and the fix). Internal session-control kinds
    (``session.end``/``meeting.leave``) are excluded from the first block
    (``entry.internal`` — not user-facing capabilities, and their one-liners are
    router-facing). Hidden (policy-denied) entries render NOWHERE either way
    (the trt.38 least-privilege guarantee).

    Rendered into the agent's persistent system prompt by
    :func:`johnny.agent.session.build_agent_instructions`. Returns ``""`` only
    when the session has neither a user-facing available capability nor a gap
    (e.g. the empty catalog of a non-delegation mode), so that prompt stays
    byte-identical. Same rows/caps as the router block — one source of truth.
    """
    blocks: list[str] = []
    available = tuple(
        entry
        for entry in entries
        if entry.available and not entry.hidden and not entry.internal
    )
    if available:
        intro = (
            "Some requests are handled for you by background tools — not "
            "answered by you directly. These include:"
        )
        rules = (
            "When the user asks for one of these: never tell them you can't do "
            "it, that you lack access, or that you're in the wrong place — the "
            "tool can. But you do NOT have its result yet, so never state or "
            "guess any specifics (events, times, names, counts, outcomes) and "
            "never pretend to look it up live — just briefly say you're on it; "
            "the real result is delivered the moment the tool finishes."
        )
        blocks.append("\n".join([intro, *_available_rows(available), rules]))
    unavailable = tuple(
        entry for entry in entries if not entry.available and not entry.hidden
    )
    if unavailable:
        header = (
            "Things you CANNOT do in this session right now. If asked for one of "
            "these, say so plainly in the user's language — give the reason below "
            "and tell them the fix it names. Never pretend to check, never "
            "promise to do it later, never invent results:"
        )
        blocks.append("\n".join([header, *_unavailable_rows(unavailable)]))
    return "\n\n".join(blocks)


__all__ = [
    "STUB_TASK_CATALOG",
    "TaskCatalogEntry",
    "render_capability_notes",
    "render_task_catalog",
]
