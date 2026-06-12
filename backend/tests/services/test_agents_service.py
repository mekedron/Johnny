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
    snapshot = build_agent_snapshot(agent, assignment_context="weekly sync")
    assert snapshot == {
        "agent_id": agent.id,
        "name": "Mika",
        "avatar": "🦊",
        "character_prompt": "Be foxy.",
        "mode": "limited_auto_speak",
        "allowed_replies": ["Yes.", "No."],
        "confidence_threshold": 0.85,
        "providers": {
            "router_llm_provider_id": None,
            "answer_llm_provider_id": None,
            "reasoning_llm_provider_id": None,
            "tts_provider_id": None,
            "tts_voice_id": "voice-7",
            "tts_options": {"rate": 1.1},
        },
        "assignment_context": "weekly sync",
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


def test_snapshot_behavior_rides_the_dispatch_contract_into_the_gate(
    db_session: Session,
) -> None:
    """Drift guard: the gate reads behavior FROM the snapshot (Johnny-trt.41).

    The path under pin: agent_snapshot → LaunchContext → SessionJobConfig
    (the dispatch metadata the worker rebuilds) — the same four behavior
    fields, value-for-value. ``job_session`` then passes
    ``config.allowed_replies`` / ``config.confidence_threshold`` /
    ``config.mode`` / ``config.character_prompt`` verbatim into
    ``RouterGateConfig``, so equality across this round trip proves no
    layer re-reads behavior from config tables.
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
        mode=str(snapshot["mode"]),
        character_prompt=str(snapshot["character_prompt"]),
        allowed_replies=tuple(snapshot["allowed_replies"]),
        confidence_threshold=float(snapshot["confidence_threshold"]),
        context=str(snapshot["assignment_context"]),
    )
    config = session_job_config_from_launch_context(ctx)

    assert config.mode == snapshot["mode"]
    assert config.character_prompt == snapshot["character_prompt"]
    assert list(config.allowed_replies) == snapshot["allowed_replies"]
    assert config.confidence_threshold == snapshot["confidence_threshold"]

    # The dispatch metadata round-trips the behavior unchanged — what the
    # worker's gate assembly receives is byte-for-byte the snapshot values.
    rebuilt = SessionJobConfig.from_metadata(config.to_metadata())
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
