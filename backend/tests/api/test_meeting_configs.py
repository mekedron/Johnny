"""Tests for the per-meeting bot configuration HTTP API (US-009, reshaped Johnny-trt.41).

The agents rebuild removed the per-meeting override soup (template /
personality FKs, mode, instructions, context, allowed_replies,
confidence_threshold). A meeting config is now: identity account + enabled +
the list of agent assignments (each binding an Agent with an optional
per-assignment context brief), plus the occurrence-scoped bot-dismissal
state (Johnny-trt.56).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db import Base
from app.db.models import (
    Agent,
    BotMode,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingAgent,
    MeetingConfig,
)
from app.main import app


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[
            GoogleAccount.__table__,  # type: ignore[list-item]
            CalendarEvent.__table__,  # type: ignore[list-item]
            Agent.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
            MeetingAgent.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
        ],
    )
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _override_session() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- fixtures: seeded rows -------------------------------------------------


@pytest.fixture
def seed_account(db_session: Session) -> GoogleAccount:
    row = GoogleAccount(
        email="user@example.com",
        refresh_token_encrypted="x",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def seed_bot_account(db_session: Session) -> GoogleAccount:
    row = GoogleAccount(
        email="johnny-bot@example.com",
        refresh_token_encrypted=None,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def seed_event(db_session: Session, seed_account: GoogleAccount) -> CalendarEvent:
    row = CalendarEvent(
        account_id=seed_account.id,
        external_id="evt-1",
        summary="Weekly sync",
        organizer="alice@example.com",
        start_time=datetime.now(UTC) + timedelta(hours=1),
        end_time=datetime.now(UTC) + timedelta(hours=2),
        meet_link="https://meet.google.com/abc-defg-hij",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _make_agent(
    db_session: Session,
    *,
    name: str,
    mode: BotMode = BotMode.LISTEN_ONLY,
    is_default: bool = False,
) -> Agent:
    row = Agent(
        name=name,
        character_prompt=f"You are {name}.",
        mode=mode,
        allowed_replies=[],
        confidence_threshold=0.7,
        is_default=is_default,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def seed_agent(db_session: Session) -> Agent:
    return _make_agent(db_session, name="Johnny", is_default=True)


@pytest.fixture
def seed_agent_b(db_session: Session) -> Agent:
    return _make_agent(db_session, name="Aria", mode=BotMode.AUTONOMOUS)


def _upsert_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "identity_account_id": 0,  # caller must override
        "enabled": True,
    }
    base.update(overrides)
    return base


# --- GET -------------------------------------------------------------------


def test_get_missing_event_returns_404(client: TestClient) -> None:
    resp = client.get("/calendar/events/9999/meeting-config")
    assert resp.status_code == 404


def test_get_no_config_returns_404(
    client: TestClient, seed_event: CalendarEvent
) -> None:
    resp = client.get(f"/calendar/events/{seed_event.id}/meeting-config")
    assert resp.status_code == 404
    assert "meeting config" in resp.json()["detail"]


# --- PUT (create) ----------------------------------------------------------


def test_put_creates_config(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(identity_account_id=seed_account.id)
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["calendar_event_id"] == seed_event.id
    assert body["identity_account_id"] == seed_account.id
    assert body["enabled"] is True
    assert body["agents"] == []
    assert body["bot_state"] == "scheduled"


def test_put_creates_then_get_returns_same(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(identity_account_id=seed_account.id)
    created = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    ).json()
    fetched = client.get(f"/calendar/events/{seed_event.id}/meeting-config").json()
    assert fetched == created


def test_put_with_bot_identity(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_bot_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(identity_account_id=seed_bot_account.id)
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200
    assert resp.json()["identity_account_id"] == seed_bot_account.id


def test_put_rejects_unknown_event(
    client: TestClient,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(identity_account_id=seed_account.id)
    resp = client.put("/calendar/events/9999/meeting-config", json=payload)
    assert resp.status_code == 404


def test_put_rejects_unknown_account(
    client: TestClient,
    seed_event: CalendarEvent,
) -> None:
    payload = _upsert_payload(identity_account_id=999)
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422
    assert "identity_account_id" in resp.json()["detail"]


def test_put_rejects_retired_override_fields(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    """The pre-trt.41 override fields are gone — extra=forbid 422s them."""
    payload = _upsert_payload(
        identity_account_id=seed_account.id,
        mode="listen_only",
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422


# --- PUT: agent assignments (Johnny-trt.41) ---------------------------------


def test_put_with_agents_round_trips_ordered_with_names(
    client: TestClient,
    db_session: Session,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
    seed_agent_b: Agent,
) -> None:
    """An explicit agents list persists and reads back ordered by position,
    with ``agent_name`` resolved from the agents table."""
    payload = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[
            # Deliberately submitted out of order; position drives the read.
            {
                "agent_id": seed_agent_b.id,
                "context": "Aria covers the demo.",
                "enabled": True,
                "position": 1,
            },
            {
                "agent_id": seed_agent.id,
                "context": None,
                "enabled": True,
                "position": 0,
            },
        ],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    agents = resp.json()["agents"]
    assert [a["agent_id"] for a in agents] == [seed_agent.id, seed_agent_b.id]
    assert [a["agent_name"] for a in agents] == ["Johnny", "Aria"]
    assert [a["position"] for a in agents] == [0, 1]
    assert agents[0]["context"] is None
    assert agents[1]["context"] == "Aria covers the demo."
    assert all(a["enabled"] is True for a in agents)
    # And the same shape comes back on GET.
    fetched = client.get(f"/calendar/events/{seed_event.id}/meeting-config").json()
    assert fetched["agents"] == agents
    # Rows actually persisted.
    count = db_session.scalar(sa.select(sa.func.count()).select_from(MeetingAgent))
    assert count == 2


def test_put_agents_omitted_leaves_assignments_untouched(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
) -> None:
    base = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[{"agent_id": seed_agent.id, "context": "brief", "position": 0}],
    )
    first = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=base
    ).json()
    assert len(first["agents"]) == 1

    # Second upsert omits ``agents`` entirely → assignments survive.
    update = _upsert_payload(identity_account_id=seed_account.id, enabled=False)
    assert "agents" not in update
    second = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=update
    ).json()
    assert second["enabled"] is False
    assert [a["agent_id"] for a in second["agents"]] == [seed_agent.id]
    assert second["agents"][0]["context"] == "brief"


def test_put_agents_empty_list_clears_assignments(
    client: TestClient,
    db_session: Session,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
) -> None:
    base = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[{"agent_id": seed_agent.id}],
    )
    client.put(f"/calendar/events/{seed_event.id}/meeting-config", json=base)

    cleared = _upsert_payload(identity_account_id=seed_account.id, agents=[])
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=cleared
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["agents"] == []
    count = db_session.scalar(sa.select(sa.func.count()).select_from(MeetingAgent))
    assert count == 0


def test_put_replaces_assignment_list(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
    seed_agent_b: Agent,
) -> None:
    """An explicit list REPLACES the previous one (not a merge)."""
    client.put(
        f"/calendar/events/{seed_event.id}/meeting-config",
        json=_upsert_payload(
            identity_account_id=seed_account.id,
            agents=[{"agent_id": seed_agent.id, "context": "old"}],
        ),
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config",
        json=_upsert_payload(
            identity_account_id=seed_account.id,
            agents=[{"agent_id": seed_agent_b.id, "context": "new"}],
        ),
    )
    assert resp.status_code == 200, resp.json()
    agents = resp.json()["agents"]
    assert [a["agent_id"] for a in agents] == [seed_agent_b.id]
    assert agents[0]["context"] == "new"


def test_put_resaving_same_agent_updates_in_place(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
) -> None:
    """Re-saving a list that KEEPS an agent (the UI always sends the full
    desired list, Johnny-trt.45) must not trip the unique constraint — the
    replacement deletes flush before the re-inserts."""
    client.put(
        f"/calendar/events/{seed_event.id}/meeting-config",
        json=_upsert_payload(
            identity_account_id=seed_account.id,
            agents=[{"agent_id": seed_agent.id, "context": "v1"}],
        ),
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config",
        json=_upsert_payload(
            identity_account_id=seed_account.id,
            agents=[{"agent_id": seed_agent.id, "context": "v2"}],
        ),
    )
    assert resp.status_code == 200, resp.json()
    agents = resp.json()["agents"]
    assert [a["agent_id"] for a in agents] == [seed_agent.id]
    assert agents[0]["context"] == "v2"


def test_put_rejects_unknown_agent_id(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[{"agent_id": 4242}],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422
    assert "agent_id=4242" in resp.json()["detail"]


def test_put_assignment_identity_account_round_trips(
    client: TestClient,
    db_session: Session,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
    seed_agent_b: Agent,
) -> None:
    """Johnny-trt.45: the per-assignment join identity persists and reads
    back; an assignment without one reads ``None`` (meeting-level fallback
    applies at dispatch)."""
    second = GoogleAccount(
        email="second-identity@example.com", refresh_token_encrypted="x"
    )
    db_session.add(second)
    db_session.commit()
    payload = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[
            {
                "agent_id": seed_agent.id,
                "identity_account_id": second.id,
                "position": 0,
            },
            {"agent_id": seed_agent_b.id, "position": 1},
        ],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    agents = resp.json()["agents"]
    assert agents[0]["identity_account_id"] == second.id
    assert agents[1]["identity_account_id"] is None
    fetched = client.get(f"/calendar/events/{seed_event.id}/meeting-config").json()
    assert fetched["agents"] == agents
    row = db_session.scalar(
        sa.select(MeetingAgent).where(MeetingAgent.agent_id == seed_agent.id)
    )
    assert row is not None and row.identity_account_id == second.id


def test_put_rejects_unknown_assignment_identity_account(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
) -> None:
    payload = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[{"agent_id": seed_agent.id, "identity_account_id": 31337}],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422
    assert "identity_account_id=31337" in resp.json()["detail"]


def test_put_rejects_duplicate_agent_id(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
) -> None:
    payload = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[
            {"agent_id": seed_agent.id, "position": 0},
            {"agent_id": seed_agent.id, "position": 1},
        ],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422
    assert "more than once" in resp.json()["detail"]


def test_put_assignment_enabled_and_disabled_round_trip(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
    seed_agent_b: Agent,
) -> None:
    payload = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[
            {"agent_id": seed_agent.id, "enabled": False, "position": 0},
            {"agent_id": seed_agent_b.id, "enabled": True, "position": 1},
        ],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    agents = resp.json()["agents"]
    assert [(a["agent_id"], a["enabled"]) for a in agents] == [
        (seed_agent.id, False),
        (seed_agent_b.id, True),
    ]


# --- PUT (update) ----------------------------------------------------------


def test_put_updates_existing(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_bot_account: GoogleAccount,
    db_session: Session,
) -> None:
    base = _upsert_payload(identity_account_id=seed_account.id)
    first = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=base
    ).json()

    update = _upsert_payload(
        identity_account_id=seed_bot_account.id,
        enabled=False,
    )
    second = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=update
    ).json()
    assert second["id"] == first["id"]
    assert second["identity_account_id"] == seed_bot_account.id
    assert second["enabled"] is False
    # Only one row in DB.
    count = db_session.scalar(
        sa.select(sa.func.count()).select_from(MeetingConfig)
    )
    assert count == 1


# --- DELETE ----------------------------------------------------------------


def test_delete_existing(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    seed_agent: Agent,
    db_session: Session,
) -> None:
    payload = _upsert_payload(
        identity_account_id=seed_account.id,
        agents=[{"agent_id": seed_agent.id}],
    )
    client.put(f"/calendar/events/{seed_event.id}/meeting-config", json=payload)
    resp = client.delete(f"/calendar/events/{seed_event.id}/meeting-config")
    assert resp.status_code == 204
    count = db_session.scalar(
        sa.select(sa.func.count()).select_from(MeetingConfig)
    )
    assert count == 0
    # Assignments cascade with the config; the agent itself survives.
    assert (
        db_session.scalar(sa.select(sa.func.count()).select_from(MeetingAgent)) == 0
    )
    assert db_session.get(Agent, seed_agent.id) is not None


def test_delete_idempotent(
    client: TestClient, seed_event: CalendarEvent
) -> None:
    """Delete when no row exists succeeds with 204 (idempotent)."""
    resp = client.delete(f"/calendar/events/{seed_event.id}/meeting-config")
    assert resp.status_code == 204


def test_delete_missing_event_returns_404(client: TestClient) -> None:
    resp = client.delete("/calendar/events/9999/meeting-config")
    assert resp.status_code == 404


# --- Bot dismissal endpoints (Johnny-trt.56) --------------------------------


@pytest.fixture(autouse=True)
def _quiet_state_publisher(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Replace the one-off Redis publish with a recorder for every test here."""
    published: list[tuple[str, dict]] = []

    async def _record(channel: str, payload: dict) -> None:
        published.append((channel, payload))

    monkeypatch.setattr(
        "app.services.meeting_lifecycle._publish_via_redis", _record
    )
    return published


def _create_config(
    client: TestClient,
    event: CalendarEvent,
    account: GoogleAccount,
) -> dict:
    payload = _upsert_payload(identity_account_id=account.id)
    resp = client.put(f"/calendar/events/{event.id}/meeting-config", json=payload)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def test_fresh_config_reads_scheduled_state(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    body = _create_config(client, seed_event, seed_account)
    assert body["bot_state"] == "scheduled"
    assert body["bot_dismissed_at"] is None
    assert body["bot_dismissed_by"] is None
    assert body["bot_dismissed_until"] is None


def test_dismiss_sets_state_and_stops_active_session(
    client: TestClient,
    db_session: Session,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    body = _create_config(client, seed_event, seed_account)
    live = BotSession(
        meeting_config_id=body["id"],
        status=BotSessionStatus.JOINED,
    )
    db_session.add(live)
    db_session.commit()

    resp = client.post(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal",
        json={"dismissed_by": "ui"},
    )
    assert resp.status_code == 200, resp.json()
    out = resp.json()
    assert out["bot_state"] == "dismissed"
    assert out["bot_dismissed_by"] == "ui"
    assert out["bot_dismissed_at"] is not None
    assert out["bot_dismissed_until"] is not None

    db_session.refresh(live)
    assert live.status is BotSessionStatus.ENDED


def test_dismiss_defaults_actor_to_ui_with_empty_body(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    _create_config(client, seed_event, seed_account)
    resp = client.post(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal"
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["bot_dismissed_by"] == "ui"


def test_dismiss_accepts_voice_actor(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    _create_config(client, seed_event, seed_account)
    resp = client.post(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal",
        json={"dismissed_by": "voice"},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["bot_dismissed_by"] == "voice"


def test_dismiss_publishes_state_change_event(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    _quiet_state_publisher: list[tuple[str, dict]],
) -> None:
    _create_config(client, seed_event, seed_account)
    resp = client.post(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal"
    )
    assert resp.status_code == 200
    channels = [c for c, _ in _quiet_state_publisher]
    assert channels == ["johnny.global.calendar"]
    assert _quiet_state_publisher[0][1]["type"] == "meeting_bot_state_changed"
    assert _quiet_state_publisher[0][1]["bot_state"] == "dismissed"


def test_dismiss_404_without_config(
    client: TestClient, seed_event: CalendarEvent
) -> None:
    resp = client.post(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal"
    )
    assert resp.status_code == 404


def test_dismiss_404_unknown_event(client: TestClient) -> None:
    resp = client.post("/calendar/events/9999/meeting-config/bot-dismissal")
    assert resp.status_code == 404


def test_get_reflects_dismissed_state(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    _create_config(client, seed_event, seed_account)
    client.post(f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal")
    fetched = client.get(f"/calendar/events/{seed_event.id}/meeting-config").json()
    assert fetched["bot_state"] == "dismissed"
    assert fetched["bot_dismissed_by"] == "ui"


def test_undismiss_clears_state(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    _quiet_state_publisher: list[tuple[str, dict]],
) -> None:
    _create_config(client, seed_event, seed_account)
    client.post(f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal")

    resp = client.delete(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal"
    )
    assert resp.status_code == 200, resp.json()
    out = resp.json()
    assert out["bot_state"] == "scheduled"
    assert out["bot_dismissed_at"] is None
    assert out["bot_dismissed_by"] is None
    assert out["bot_dismissed_until"] is None
    # dismiss + undismiss both announced.
    assert [p["bot_state"] for _, p in _quiet_state_publisher] == [
        "dismissed",
        "scheduled",
    ]


def test_undismiss_idempotent_when_not_dismissed(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
    _quiet_state_publisher: list[tuple[str, dict]],
) -> None:
    _create_config(client, seed_event, seed_account)
    resp = client.delete(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal"
    )
    assert resp.status_code == 200
    assert resp.json()["bot_state"] == "scheduled"
    assert _quiet_state_publisher == []
