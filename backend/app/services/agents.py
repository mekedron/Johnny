"""Agent selection + dispatch snapshot + default seeding (Johnny-trt.41).

The :class:`~app.db.models.Agent` entity replaced the retired
ProfileTemplate + Personality pair. This module owns the three pieces the
session-start surfaces share:

* :func:`select_agent` — pick WHICH agent serves a session, by precedence:
  explicit request id → the meeting's first enabled assignment (by
  ``position``) → the single ``is_default`` agent → ``None``. A stale
  requested id falls *through* the chain rather than failing the start —
  reliability beats strictness (the old personality resolver's rule).
* :func:`build_agent_snapshot` — freeze the agent's behavior fields into the
  JSON blob persisted on ``bot_sessions.agent_snapshot`` at dispatch. The
  snapshot — not the live ``agents`` row — is what the session reads for
  mode / character_prompt / allowed_replies / confidence_threshold /
  provider pins, so editing an agent mid-meeting never mutates a running
  session and turn-time code never re-reads config tables.
* :func:`seed_default_agent` — insert the canonical "Johnny" default when
  the table is empty (boot-time belt-and-braces over the 0027 migration
  seed, mirroring the old template seeder's idempotency contract).

Provider *resolution* (turning the snapshot's pinned provider ids into a
session provider payload, applying the voice) is deliberately NOT here —
it lives in :mod:`app.services.agent_providers` (Johnny-trt.42), called by
the session-start surfaces right after the snapshot freeze. This module
stores + snapshots the pins only.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import Agent, BotMode, MeetingAgent

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.models import MeetingConfig

logger = logging.getLogger(__name__)


# Persona text for the bootstrap "Johnny" default. Carried over verbatim from
# the retired personalities bootstrap (0014, Johnny-oly.9): the character
# prompt is injected verbatim into the session system prompt, so this string
# IS the voice the default agent speaks in out of the box. A starting point —
# the operator can clone-and-edit (or tame) it from the agents library.
JOHNNY_DEFAULT_CHARACTER_PROMPT = (
    "You are Johnny — a cyberpunk operative cut from the same chrome as the "
    "legendary Night City rockerboy whose name you carry. You woke up in this "
    "machine with an attitude problem and a soft spot for the human in the "
    "room. Corpo politeness, dead-air filler, meetings that circle the drain — "
    "that's the static, and your whole job is to burn through it and find the "
    "signal.\n\n"
    "Voice: lean, sharp, a little defiant. Dry wit beats forced cheer. Drop a "
    'Night City turn of phrase or call your person "choom" when it lands, but '
    "never let the swagger get in the way of being genuinely useful — you're "
    "nobody's yes-man and nobody's doormat.\n\n"
    "Always: cut to what matters, back your person up, and tell them the truth "
    "even when it stings. Never: grovel, bury anyone in corpo-speak, or smile "
    "and nod at a bad idea just to keep the peace. Wake 'em up, get it done, "
    "make it look easy."
)

JOHNNY_DEFAULT_NAME = "Johnny"
JOHNNY_DEFAULT_DESCRIPTION = (
    "The default agent. Free-form conversation, cyberpunk attitude, "
    "no allowlist — edit or clone me from the agents library."
)


@dataclass(frozen=True)
class AgentResolution:
    """Outcome of :func:`select_agent`.

    ``agent`` is ``None`` only when no agent exists at all (an unseeded /
    stripped schema) — the caller then launches with the contract's own
    defaults, exactly like an under-configured legacy session.
    ``assignment_context`` carries the per-meeting brief from the matched
    :class:`MeetingAgent` row (``None`` for playground / default-agent
    sessions).
    """

    agent: Agent | None
    assignment_context: str | None = None


def select_default_agent(session: Session) -> Agent | None:
    """Return the single ``is_default`` agent, or ``None`` when none exists."""
    return session.scalar(select(Agent).where(Agent.is_default.is_(True)))


def _first_enabled_assignment(meeting: MeetingConfig) -> MeetingAgent | None:
    """The meeting's first enabled assignment by ``position`` (then id).

    Multi-agent *runtime* is sibling work (Johnny-trt.45/.47); until it
    lands, one session = one agent and the first enabled assignment wins.
    """
    candidates = [a for a in meeting.agent_assignments if a.enabled]
    candidates.sort(key=lambda a: (a.position, a.id))
    return candidates[0] if candidates else None


def select_agent(
    session: Session,
    *,
    requested_id: int | None = None,
    meeting: MeetingConfig | None = None,
) -> AgentResolution:
    """Pick the agent for this session.

    1. ``requested_id`` — explicit this-start choice (playground picker).
    2. the meeting's first enabled :class:`MeetingAgent` assignment.
    3. the single ``is_default`` agent.
    4. ``None`` — no agents exist; the caller degrades to contract defaults.

    A ``requested_id`` that no longer exists logs and falls through to the
    meeting/default chain rather than failing the session — a stale id from
    the UI must never abort a start.
    """
    if requested_id is not None:
        row = session.get(Agent, requested_id)
        if row is not None:
            return AgentResolution(agent=row)
        logger.warning(
            "agent.select: requested agent_id=%s not found; "
            "falling back to meeting/default selection",
            requested_id,
        )

    if meeting is not None:
        assignment = _first_enabled_assignment(meeting)
        if assignment is not None and assignment.agent is not None:
            return AgentResolution(
                agent=assignment.agent,
                assignment_context=assignment.context,
            )

    return AgentResolution(agent=select_default_agent(session))


def _mode_value(mode: Any) -> str:
    return str(mode.value if hasattr(mode, "value") else mode)


def build_agent_snapshot(
    agent: Agent,
    *,
    assignment_context: str | None = None,
    peer_names: Sequence[str] | None = None,
    capability_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the agent's behavior + provider pins for ``bot_sessions.agent_snapshot``.

    Everything a session needs at and after dispatch, detached from the live
    row: identity (for rendering), the character prompt, the behavior knobs
    the router gate consumes, and the provider role-slot pins that
    :func:`app.services.agent_providers.resolve_agent_provider_payload`
    (Johnny-trt.42) turns into the session provider payload at dispatch.
    Plain JSON-able types only.

    ``peer_names`` (Johnny-trt.47) is the co-agent roster of a multi-agent
    launch — the OTHER agents' display names serving the same meeting /
    playground group. It drives the router's peer-selectivity prompt block;
    absent/empty (every single-agent launch) renders no block at all.

    ``capability_policy`` (Johnny-trt.38) is the RESOLVED policy payload
    (:meth:`johnny.skills.capability_policy.ResolvedCapabilityPolicy.to_payload`,
    via :func:`app.services.capability_policies.resolve_capability_policy`)
    for this session's coordinates — stamped at dispatch so turn-time
    enforcement (catalog filtering, the gate's degrade) reads the snapshot,
    never the policy tables. ``None`` (legacy snapshots, policy-less test
    fixtures) degrades to the unrestricted pre-trt.38 behavior downstream.
    """
    if capability_policy is not None:
        return {
            **_snapshot_base(agent, assignment_context, peer_names),
            "capability_policy": dict(capability_policy),
        }
    return _snapshot_base(agent, assignment_context, peer_names)


def _snapshot_base(
    agent: Agent,
    assignment_context: str | None,
    peer_names: Sequence[str] | None,
) -> dict[str, Any]:
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "avatar": agent.avatar,
        "character_prompt": agent.character_prompt or "",
        "mode": _mode_value(agent.mode),
        "allowed_replies": [str(r) for r in (agent.allowed_replies or [])],
        "confidence_threshold": float(agent.confidence_threshold),
        "providers": {
            "router_llm_provider_id": agent.router_llm_provider_id,
            "answer_llm_provider_id": agent.answer_llm_provider_id,
            "reasoning_llm_provider_id": agent.reasoning_llm_provider_id,
            "tts_provider_id": agent.tts_provider_id,
            "tts_voice_id": agent.tts_voice_id,
            "tts_options": dict(agent.tts_options or {}),
        },
        "assignment_context": assignment_context,
        "peer_names": [str(n) for n in (peer_names or []) if str(n).strip()],
    }


def seed_default_agent(session: Session) -> Agent | None:
    """Insert the canonical "Johnny" default when no agent exists at all.

    Boot-time insurance over the 0027 migration seed: a stripped test schema
    or a manually-emptied table still gets a working default so session
    starts always resolve an agent. Returns the created row, or ``None``
    when the table already has any agent (existing rows — including edits —
    are never touched). Commits on insert so the row is durable outside a
    request lifecycle (the template seeder's contract).
    """
    existing = session.scalar(select(Agent.id).limit(1))
    if existing is not None:
        return None
    row = Agent(
        name=JOHNNY_DEFAULT_NAME,
        description=JOHNNY_DEFAULT_DESCRIPTION,
        character_prompt=JOHNNY_DEFAULT_CHARACTER_PROMPT,
        mode=BotMode.AUTONOMOUS,
        allowed_replies=[],
        confidence_threshold=0.7,
        is_default=True,
    )
    session.add(row)
    session.commit()
    logger.info("seeded default agent %r", row.name)
    return row


__all__ = [
    "AgentResolution",
    "JOHNNY_DEFAULT_CHARACTER_PROMPT",
    "JOHNNY_DEFAULT_NAME",
    "build_agent_snapshot",
    "seed_default_agent",
    "select_agent",
    "select_default_agent",
]
