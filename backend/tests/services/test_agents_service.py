"""Tests for agent selection / snapshot / seeding (Johnny-trt.41).

Pins the three contracts the session-start surfaces share:

* :func:`select_agent` precedence — explicit request id → the meeting's
  first enabled assignment by position → the ``is_default`` agent → none;
  a stale requested id falls through instead of failing.
* :func:`build_agent_snapshot` — the frozen behavior blob persisted on
  ``bot_sessions.agent_snapshot``, and the drift guard proving the
  router/gate consume behavior *from that snapshot*: the snapshot's
  behavior fields ride LaunchContext → SessionJobConfig and arrive as the
  exact ``allowed_replies`` / ``confidence_threshold`` / ``mode`` /
  ``character_prompt`` the gate config is built from (job_session passes
  them verbatim) — no config-table re-read anywhere on that path.
* :func:`seed_default_agent` — inserts canonical Johnny only into an empty
  table; never touches existing rows.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.db.models import (
    Agent,
    BotMode,
    CalendarEvent,
    GoogleAccount,
    MeetingAgent,
    MeetingConfig,
)
from app.services.agents import (
    JOHNNY_DEFAULT_CHARACTER_PROMPT,
    build_agent_snapshot,
    seed_default_agent,
    select_agent,
    select_default_agent,
)


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine)
    session = maker()
    try:
        yield session
    finally:
        session.close()


def _seed_meeting(db_session: Session) -> MeetingConfig:
    from datetime import UTC, datetime, timedelta

    account = GoogleAccount(email="owner@example.com")
    db_session.add(account)
    db_session.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id="evt-1",
        start_time=datetime.now(UTC),
        end_time=datetime.now(UTC) + timedelta(hours=1),
        meet_link="https://meet.google.com/abc-defg-hij",
    )
    db_session.add(event)
    db_session.flush()
    meeting = MeetingConfig(
        calendar_event_id=event.id,
        identity_account_id=account.id,
        enabled=True,
    )
    db_session.add(meeting)
    db_session.flush()
    return meeting


def _agent(db_session: Session, name: str, **kwargs: object) -> Agent:
    row = Agent(name=name, **kwargs)
    db_session.add(row)
    db_session.flush()
    return row


# --- select_agent -------------------------------------------------------------


def test_select_returns_none_when_no_agents(db_session: Session) -> None:
    resolution = select_agent(db_session)
    assert resolution.agent is None
    assert resolution.assignment_context is None


def test_select_falls_back_to_default(db_session: Session) -> None:
    _agent(db_session, "Other")
    default = _agent(db_session, "Johnny", is_default=True)
    resolution = select_agent(db_session)
    assert resolution.agent is not None
    assert resolution.agent.id == default.id
    assert select_default_agent(db_session) is not None


def test_select_explicit_request_wins_over_meeting_and_default(
    db_session: Session,
) -> None:
    default = _agent(db_session, "Johnny", is_default=True)
    assigned = _agent(db_session, "Assigned")
    requested = _agent(db_session, "Requested")
    meeting = _seed_meeting(db_session)
    db_session.add(
        MeetingAgent(meeting_config_id=meeting.id, agent_id=assigned.id)
    )
    db_session.flush()

    resolution = select_agent(
        db_session, requested_id=requested.id, meeting=meeting
    )
    assert resolution.agent is not None
    assert resolution.agent.id == requested.id
    assert resolution.agent.id != default.id
    # An explicit request carries no per-meeting assignment context.
    assert resolution.assignment_context is None


def test_select_meeting_first_enabled_assignment_by_position(
    db_session: Session,
) -> None:
    _agent(db_session, "Johnny", is_default=True)
    second = _agent(db_session, "Second")
    first = _agent(db_session, "First")
    disabled = _agent(db_session, "Disabled")
    meeting = _seed_meeting(db_session)
    db_session.add_all(
        [
            MeetingAgent(
                meeting_config_id=meeting.id,
                agent_id=disabled.id,
                position=0,
                enabled=False,
            ),
            MeetingAgent(
                meeting_config_id=meeting.id,
                agent_id=second.id,
                position=5,
                context="second brief",
            ),
            MeetingAgent(
                meeting_config_id=meeting.id,
                agent_id=first.id,
                position=1,
                context="first brief",
            ),
        ]
    )
    db_session.flush()
    db_session.refresh(meeting)

    resolution = select_agent(db_session, meeting=meeting)
    assert resolution.agent is not None
    assert resolution.agent.id == first.id
    assert resolution.assignment_context == "first brief"


def test_select_stale_requested_id_falls_through(db_session: Session) -> None:
    default = _agent(db_session, "Johnny", is_default=True)
    resolution = select_agent(db_session, requested_id=99_999)
    assert resolution.agent is not None
    assert resolution.agent.id == default.id


# --- build_agent_snapshot -------------------------------------------------------


def test_snapshot_shape_and_values(db_session: Session) -> None:
    agent = _agent(
        db_session,
        "Mika",
        avatar="🦊",
        character_prompt="Be foxy.",
        mode=BotMode.LIMITED_AUTO_SPEAK,
        allowed_replies=["Yes.", "No."],
        confidence_threshold=0.85,
        tts_voice_id="voice-7",
        tts_options={"rate": 1.1},
    )
    snapshot = build_agent_snapshot(
        agent, assignment_context="weekly sync", peer_names=["Echo", "  ", "Nova"]
    )
    assert snapshot == {
        "agent_id": agent.id,
        "name": "Mika",
        "avatar": "🦊",
        "character_prompt": "Be foxy.",
        "mode": "limited_auto_speak",
        "allowed_replies": ["Yes.", "No."],
        "confidence_threshold": 0.85,
        # Router-triage timeout + on-timeout fallback (Johnny-xql); _agent left
        # these unset so they ride the model defaults.
        "router_llm_timeout_s": 8.0,
        "router_timeout_retries": 0,
        "router_timeout_fallback_mode": "static",
        "router_timeout_fallback_text": (
            "Sorry, I didn't catch that in time — could you say that again?"
        ),
        "providers": {
            "router_llm_provider_id": None,
            "answer_llm_provider_id": None,
            "reasoning_llm_provider_id": None,
            "tts_provider_id": None,
            "tts_voice_id": "voice-7",
            "tts_options": {"rate": 1.1},
        },
        "assignment_context": "weekly sync",
        # Johnny-trt.47: the co-agent roster, blanks dropped; defaults to []
        # for every single-agent launch.
        "peer_names": ["Echo", "Nova"],
    }
    # Plain JSON-able — what bot_sessions.agent_snapshot stores verbatim.
    import json

    assert json.loads(json.dumps(snapshot)) == snapshot


def test_snapshot_is_detached_from_the_live_row(db_session: Session) -> None:
    agent = _agent(db_session, "Mika", allowed_replies=["Yes."])
    snapshot = build_agent_snapshot(agent)
    agent.allowed_replies = ["Changed."]
    agent.name = "Renamed"
    assert snapshot["allowed_replies"] == ["Yes."]
    assert snapshot["name"] == "Mika"


def test_snapshot_carries_the_resolved_capability_policy(db_session: Session) -> None:
    """Johnny-trt.38: the RESOLVED policy payload rides the snapshot to
    turn-time enforcement; absent (None) keeps the legacy key-less shape and
    the job-config read degrades to unrestricted."""
    from johnny.agent.job_config import SessionJobConfig
    from johnny.skills.capability_policy import (
        CapabilityPolicyLayer,
        resolve_policy,
    )

    agent = _agent(db_session, "Mika")
    bare = build_agent_snapshot(agent)
    assert "capability_policy" not in bare
    assert SessionJobConfig(
        bot_session_id=1, room_name="r", agent_snapshot=bare
    ).capability_policy().tools_unrestricted

    policy = resolve_policy(
        [
            CapabilityPolicyLayer.from_document(
                "agent", {"tools_allow": ["google-calendar"]}, scope_detail="Mika"
            )
        ]
    )
    stamped = build_agent_snapshot(agent, capability_policy=policy.to_payload())
    assert stamped["capability_policy"]["layers"][0]["scope"] == "agent"

    rebuilt = SessionJobConfig(
        bot_session_id=1, room_name="r", agent_snapshot=stamped
    ).capability_policy()
    assert rebuilt.check_tool("google-calendar").allowed
    denied = rebuilt.check_tool("financial-reports")
    assert not denied.allowed and denied.layer == "agent" and denied.detail == "Mika"

    # Plain JSON-able — what bot_sessions.agent_snapshot stores verbatim.
    import json

    assert json.loads(json.dumps(stamped)) == stamped


def test_snapshot_carries_the_resolved_workspace(db_session: Session) -> None:
    """Johnny-wks.1: the effective workspace rides the snapshot to the
    resolver seams; absent (None) keeps the legacy key-less shape so
    default-workspace agents stay byte-identical."""
    from app.db.models import Workspace
    from app.services.workspaces import (
        resolve_agent_workspace,
        seed_default_workspace,
        workspace_snapshot_payload,
    )

    agent = _agent(db_session, "Mika")
    bare = build_agent_snapshot(agent)
    assert "workspace_id" not in bare
    assert "workspace" not in bare

    seed_default_workspace(db_session)
    default = resolve_agent_workspace(db_session, agent)
    assert default is not None and default.is_default is True

    stamped = build_agent_snapshot(
        agent, workspace=workspace_snapshot_payload(default)
    )
    assert stamped["workspace_id"] == default.id
    assert stamped["workspace"] == {
        "id": default.id,
        "name": "Default",
        "slug": "default",
        "is_default": True,
    }

    # An explicit non-default attachment resolves to ITS row.
    finance = Workspace(name="Finance", slug="finance", is_default=False)
    db_session.add(finance)
    db_session.flush()
    agent.workspace_id = finance.id
    resolved = resolve_agent_workspace(db_session, agent)
    assert resolved is not None and resolved.id == finance.id
    stamped = build_agent_snapshot(
        agent, workspace=workspace_snapshot_payload(resolved)
    )
    assert stamped["workspace_id"] == finance.id
    assert stamped["workspace"]["is_default"] is False

    # Plain JSON-able — what bot_sessions.agent_snapshot stores verbatim.
    import json

    assert json.loads(json.dumps(stamped)) == stamped


def test_resolve_agent_workspace_degrades(db_session: Session) -> None:
    """NULL attachment → the default workspace; unseeded schema → None;
    a dangling id (RESTRICT should prevent it) falls back to the default."""
    from app.services.workspaces import (
        resolve_agent_workspace,
        seed_default_workspace,
    )

    agent = _agent(db_session, "Mika")
    assert resolve_agent_workspace(db_session, agent) is None  # unseeded
    assert resolve_agent_workspace(db_session, None) is None

    default = seed_default_workspace(db_session)
    assert default is not None
    assert resolve_agent_workspace(db_session, agent).id == default.id
    assert resolve_agent_workspace(db_session, None).id == default.id

    agent.workspace_id = 999  # dangling — fall back, never fail a dispatch
    assert resolve_agent_workspace(db_session, agent).id == default.id


def test_snapshot_behavior_rides_the_dispatch_contract_into_the_gate(
    db_session: Session,
) -> None:
    """Drift guard: the gate reads behavior FROM the snapshot (Johnny-trt.41).

    Since Johnny-trt.45 the snapshot rides the dispatch contract WHOLE:
    agent_snapshot → LaunchContext.agent_snapshot →
    SessionJobConfig.agent_snapshot (the dispatch metadata the worker
    rebuilds), and the contract derives mode / character_prompt /
    allowed_replies / confidence_threshold / context from it.
    ``job_session`` then passes those properties verbatim into
    ``RouterGateConfig``, so equality across this round trip proves no
    layer re-reads behavior from config tables — and no layer can drift
    from the persisted ``bot_sessions.agent_snapshot``.
    """
    from app.services.agent_dispatch import session_job_config_from_launch_context
    from app.services.session_scheduler import LaunchContext
    from johnny.agent.job_config import SessionJobConfig

    agent = _agent(
        db_session,
        "Mika",
        character_prompt="Be foxy.",
        mode=BotMode.LIMITED_AUTO_SPEAK,
        allowed_replies=["Yes.", "No."],
        confidence_threshold=0.85,
    )
    snapshot = build_agent_snapshot(agent, assignment_context="brief")

    ctx = LaunchContext(
        bot_session_id=1,
        meeting_config_id=2,
        calendar_event_id=3,
        identity_account_id=4,
        meet_link="https://meet.google.com/abc-defg-hij",
        container_name="meet-worker-session-1",
        agent_id=agent.id,
        agent_snapshot=snapshot,
    )
    config = session_job_config_from_launch_context(ctx)

    assert config.agent_snapshot == snapshot
    assert config.mode == snapshot["mode"]
    assert config.character_prompt == snapshot["character_prompt"]
    assert list(config.allowed_replies) == snapshot["allowed_replies"]
    assert config.confidence_threshold == snapshot["confidence_threshold"]
    assert config.context == snapshot["assignment_context"]

    # The dispatch metadata round-trips the behavior unchanged — what the
    # worker's gate assembly receives is byte-for-byte the snapshot values.
    rebuilt = SessionJobConfig.from_metadata(config.to_metadata())
    assert rebuilt.agent_snapshot == snapshot
    assert rebuilt.mode == snapshot["mode"]
    assert rebuilt.character_prompt == snapshot["character_prompt"]
    assert list(rebuilt.allowed_replies) == snapshot["allowed_replies"]
    assert rebuilt.confidence_threshold == snapshot["confidence_threshold"]


def test_gate_assembly_consumes_job_config_behavior_fields() -> None:
    """The RouterGateConfig fields the snapshot feeds exist and accept them.

    ``job_session.py`` passes config.allowed_replies/confidence_threshold/
    mode/character_prompt into ``RouterGateConfig`` (asserted by source
    inspection here to stay import-light — RouterGateConfig itself imports
    cleanly, but assembling a full job session needs livekit). This pins the
    wiring textually so a refactor that silently drops a field fails loud.
    """
    import inspect

    import johnny.agent.job_session as job_session
    from johnny.agent.router_gate import RouterGateConfig

    source = inspect.getsource(job_session)
    for needle in (
        "allowed_replies=tuple(config.allowed_replies)",
        "confidence_threshold=config.confidence_threshold",
        "character_prompt=config.character_prompt",
        "mode=config.mode",
    ):
        assert needle in source, f"job_session no longer wires {needle!r}"

    fields = {f.name for f in RouterGateConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert {
        "allowed_replies",
        "confidence_threshold",
        "mode",
        "character_prompt",
    } <= fields


# --- seed_default_agent ---------------------------------------------------------


def test_seed_inserts_canonical_johnny_into_empty_table(
    db_session: Session,
) -> None:
    created = seed_default_agent(db_session)
    assert created is not None
    assert created.name == "Johnny"
    assert created.is_default is True
    assert created.mode is BotMode.AUTONOMOUS
    assert created.character_prompt == JOHNNY_DEFAULT_CHARACTER_PROMPT

    # Idempotent: a second call is a no-op.
    assert seed_default_agent(db_session) is None
    assert db_session.scalar(sa.select(sa.func.count(Agent.id))) == 1


def test_seed_never_touches_existing_rows(db_session: Session) -> None:
    existing = Agent(name="Custom", is_default=True, character_prompt="mine")
    db_session.add(existing)
    db_session.commit()
    assert seed_default_agent(db_session) is None
    db_session.refresh(existing)
    assert existing.character_prompt == "mine"
    assert db_session.scalar(sa.select(sa.func.count(Agent.id))) == 1
