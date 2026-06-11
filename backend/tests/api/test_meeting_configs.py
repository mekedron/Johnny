"""Tests for the per-meeting bot configuration HTTP API (US-009)."""

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
    BotMode,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    ProfileTemplate,
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
            ProfileTemplate.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
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


@pytest.fixture
def seed_template_listen(db_session: Session) -> ProfileTemplate:
    row = ProfileTemplate(
        name="Listen-only",
        mode=BotMode.LISTEN_ONLY,
        base_instructions="be quiet",
        base_context="",
        allowed_replies=[],
        confidence_threshold=0.7,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def seed_template_limited(db_session: Session) -> ProfileTemplate:
    row = ProfileTemplate(
        name="Limited auto-speak",
        mode=BotMode.LIMITED_AUTO_SPEAK,
        base_instructions="answer only with approved replies",
        base_context="",
        allowed_replies=["Yes", "No", "Could you repeat that?"],
        confidence_threshold=0.8,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _upsert_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile_template_id": 0,  # caller must override
        "identity_account_id": 0,
        "mode": "listen_only",
        "instructions": "be polite",
        "context": "weekly sync",
        "allowed_replies": None,
        "confidence_threshold": 0.75,
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
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["calendar_event_id"] == seed_event.id
    assert body["profile_template_id"] == seed_template_listen.id
    assert body["identity_account_id"] == seed_account.id
    assert body["mode"] == "listen_only"
    assert body["instructions"] == "be polite"
    assert body["context"] == "weekly sync"
    assert body["allowed_replies"] is None
    assert body["confidence_threshold"] == 0.75
    assert body["enabled"] is True


def test_put_creates_then_get_returns_same(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
    )
    created = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    ).json()
    fetched = client.get(f"/calendar/events/{seed_event.id}/meeting-config").json()
    assert fetched == created


def test_put_defaults_mode_to_template(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_limited: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_limited.id,
        identity_account_id=seed_account.id,
        mode=None,
        allowed_replies=None,
    )
    payload.pop("mode")
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["mode"] == "limited_auto_speak"


def test_put_with_bot_identity(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_bot_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_bot_account.id,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200
    assert resp.json()["identity_account_id"] == seed_bot_account.id


def test_put_rejects_unknown_event(
    client: TestClient,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
    )
    resp = client.put("/calendar/events/9999/meeting-config", json=payload)
    assert resp.status_code == 404


def test_put_rejects_unknown_template(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=999, identity_account_id=seed_account.id
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422
    assert "profile_template_id" in resp.json()["detail"]


def test_put_rejects_unknown_account(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id, identity_account_id=999
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422
    assert "identity_account_id" in resp.json()["detail"]


def test_put_strips_blank_allowed_replies(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
        mode="limited_auto_speak",
        allowed_replies=["yes", "", "  ", "no thanks  "],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["allowed_replies"] == ["yes", "no thanks"]


def test_put_limited_auto_speak_without_replies_uses_template(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_limited: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    """When meeting overrides are None but template has replies, accept."""
    payload = _upsert_payload(
        profile_template_id=seed_template_limited.id,
        identity_account_id=seed_account.id,
        mode="limited_auto_speak",
        allowed_replies=None,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()


def test_put_limited_auto_speak_with_empty_overrides_and_no_template_replies(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,  # no allowed_replies
    seed_account: GoogleAccount,
) -> None:
    """Empty override + template without replies → rejected."""
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
        mode="limited_auto_speak",
        allowed_replies=[],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422
    assert "allowed_replies" in str(resp.json()["detail"])


# --- Johnny-ckz.2: autonomous mode validation ------------------------------


@pytest.fixture
def seed_template_autonomous(db_session: Session) -> ProfileTemplate:
    """Template configured for autonomous mode with non-empty instructions."""
    row = ProfileTemplate(
        name="Autonomous",
        mode=BotMode.AUTONOMOUS,
        base_instructions="Be a helpful meeting participant.",
        base_context="",
        allowed_replies=[],
        confidence_threshold=0.8,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def seed_template_listen_blank(db_session: Session) -> ProfileTemplate:
    """Listen-only template with deliberately blank instructions —
    used to verify autonomous-mode validation rejects falling through
    to a template whose instructions are empty."""
    row = ProfileTemplate(
        name="Blank listen",
        mode=BotMode.LISTEN_ONLY,
        base_instructions="",
        base_context="",
        allowed_replies=[],
        confidence_threshold=0.7,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_put_autonomous_with_template_instructions_ok(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_autonomous: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    """Autonomous mode with a template providing instructions and no
    per-meeting override should save successfully."""
    payload = _upsert_payload(
        profile_template_id=seed_template_autonomous.id,
        identity_account_id=seed_account.id,
        mode="autonomous",
        instructions=None,
        allowed_replies=None,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["mode"] == "autonomous"


def test_put_autonomous_with_per_meeting_instructions_ok(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen_blank: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    """Template instructions blank but per-meeting override populated
    → still acceptable; the override is the governance source."""
    payload = _upsert_payload(
        profile_template_id=seed_template_listen_blank.id,
        identity_account_id=seed_account.id,
        mode="autonomous",
        instructions="Drive the meeting for the host.",
        allowed_replies=None,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()


def test_put_autonomous_blank_override_and_blank_template_rejected(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen_blank: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    """Both sources blank → rejected with 422 and a clear message."""
    payload = _upsert_payload(
        profile_template_id=seed_template_listen_blank.id,
        identity_account_id=seed_account.id,
        mode="autonomous",
        instructions=None,
        allowed_replies=None,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422
    assert "instructions" in str(resp.json()["detail"])
    assert "autonomous" in str(resp.json()["detail"])


def test_put_autonomous_whitespace_override_treated_as_blank(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen_blank: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    """Whitespace-only instructions count as blank for validation."""
    payload = _upsert_payload(
        profile_template_id=seed_template_listen_blank.id,
        identity_account_id=seed_account.id,
        mode="autonomous",
        instructions="   \n\t ",
        allowed_replies=None,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422


def test_put_autonomous_does_not_require_allowed_replies(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_autonomous: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    """Autonomous deliberately ignores allowed_replies — empty list ok."""
    payload = _upsert_payload(
        profile_template_id=seed_template_autonomous.id,
        identity_account_id=seed_account.id,
        mode="autonomous",
        instructions="Take notes and answer.",
        allowed_replies=[],
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["allowed_replies"] == []


def test_put_rejects_threshold_out_of_range(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
        confidence_threshold=1.5,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 422


def test_put_accepts_explicit_zero_threshold(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
        confidence_threshold=0.0,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=payload
    )
    assert resp.status_code == 200
    assert resp.json()["confidence_threshold"] == 0.0


# --- PUT (update) ----------------------------------------------------------


def test_put_updates_existing(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_template_limited: ProfileTemplate,
    seed_account: GoogleAccount,
    db_session: Session,
) -> None:
    base = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
        mode="listen_only",
    )
    first = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=base
    ).json()

    update = _upsert_payload(
        profile_template_id=seed_template_limited.id,
        identity_account_id=seed_account.id,
        mode="approval_required",
        instructions="updated",
        context="updated context",
        allowed_replies=["okay"],
        confidence_threshold=0.9,
    )
    second = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=update
    ).json()
    assert second["id"] == first["id"]
    assert second["profile_template_id"] == seed_template_limited.id
    assert second["mode"] == "approval_required"
    assert second["instructions"] == "updated"
    assert second["context"] == "updated context"
    assert second["allowed_replies"] == ["okay"]
    assert second["confidence_threshold"] == 0.9
    # Only one row in DB.
    count = db_session.scalar(
        sa.select(sa.func.count()).select_from(MeetingConfig)
    )
    assert count == 1


def test_put_can_clear_override_fields(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    base = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
        instructions="initial",
        context="initial",
        allowed_replies=["only-with-mode-limited"],
        confidence_threshold=0.6,
    )
    client.put(f"/calendar/events/{seed_event.id}/meeting-config", json=base)

    cleared = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
        instructions=None,
        context=None,
        allowed_replies=None,
        confidence_threshold=None,
    )
    resp = client.put(
        f"/calendar/events/{seed_event.id}/meeting-config", json=cleared
    )
    body = resp.json()
    assert body["instructions"] is None
    assert body["context"] is None
    assert body["allowed_replies"] is None
    assert body["confidence_threshold"] is None


# --- DELETE ----------------------------------------------------------------


def test_delete_existing(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
    db_session: Session,
) -> None:
    payload = _upsert_payload(
        profile_template_id=seed_template_listen.id,
        identity_account_id=seed_account.id,
    )
    client.put(f"/calendar/events/{seed_event.id}/meeting-config", json=payload)
    resp = client.delete(f"/calendar/events/{seed_event.id}/meeting-config")
    assert resp.status_code == 204
    count = db_session.scalar(
        sa.select(sa.func.count()).select_from(MeetingConfig)
    )
    assert count == 0


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
    template: ProfileTemplate,
    account: GoogleAccount,
) -> dict:
    payload = _upsert_payload(
        profile_template_id=template.id,
        identity_account_id=account.id,
    )
    resp = client.put(f"/calendar/events/{event.id}/meeting-config", json=payload)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def test_fresh_config_reads_scheduled_state(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    body = _create_config(client, seed_event, seed_template_listen, seed_account)
    assert body["bot_state"] == "scheduled"
    assert body["bot_dismissed_at"] is None
    assert body["bot_dismissed_by"] is None
    assert body["bot_dismissed_until"] is None


def test_dismiss_sets_state_and_stops_active_session(
    client: TestClient,
    db_session: Session,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    body = _create_config(client, seed_event, seed_template_listen, seed_account)
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
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    _create_config(client, seed_event, seed_template_listen, seed_account)
    resp = client.post(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal"
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["bot_dismissed_by"] == "ui"


def test_dismiss_accepts_voice_actor(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    _create_config(client, seed_event, seed_template_listen, seed_account)
    resp = client.post(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal",
        json={"dismissed_by": "voice"},
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["bot_dismissed_by"] == "voice"


def test_dismiss_publishes_state_change_event(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
    _quiet_state_publisher: list[tuple[str, dict]],
) -> None:
    _create_config(client, seed_event, seed_template_listen, seed_account)
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
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
) -> None:
    _create_config(client, seed_event, seed_template_listen, seed_account)
    client.post(f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal")
    fetched = client.get(f"/calendar/events/{seed_event.id}/meeting-config").json()
    assert fetched["bot_state"] == "dismissed"
    assert fetched["bot_dismissed_by"] == "ui"


def test_undismiss_clears_state(
    client: TestClient,
    seed_event: CalendarEvent,
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
    _quiet_state_publisher: list[tuple[str, dict]],
) -> None:
    _create_config(client, seed_event, seed_template_listen, seed_account)
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
    seed_template_listen: ProfileTemplate,
    seed_account: GoogleAccount,
    _quiet_state_publisher: list[tuple[str, dict]],
) -> None:
    _create_config(client, seed_event, seed_template_listen, seed_account)
    resp = client.delete(
        f"/calendar/events/{seed_event.id}/meeting-config/bot-dismissal"
    )
    assert resp.status_code == 200
    assert resp.json()["bot_state"] == "scheduled"
    assert _quiet_state_publisher == []
