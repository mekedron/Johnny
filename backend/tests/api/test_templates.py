"""Tests for the profile templates HTTP API (US-010)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db import Base
from app.db.models import (
    BotMode,
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
        ],
    )
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


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


def _create_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Listen-only standup",
        "mode": "listen_only",
        "base_instructions": "Transcribe silently.",
        "base_context": "Daily standup.",
        "allowed_replies": [],
        "confidence_threshold": 0.7,
    }
    base.update(overrides)
    return base


# --- list ------------------------------------------------------------------


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/templates")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_orders_by_name(client: TestClient) -> None:
    client.post("/templates", json=_create_payload(name="Zeta"))
    client.post("/templates", json=_create_payload(name="Alpha"))
    client.post("/templates", json=_create_payload(name="Mid"))
    resp = client.get("/templates")
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert names == ["Alpha", "Mid", "Zeta"]


# --- create ----------------------------------------------------------------


def test_create_returns_201(client: TestClient) -> None:
    resp = client.post("/templates", json=_create_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] >= 1
    assert body["name"] == "Listen-only standup"
    assert body["mode"] == "listen_only"
    assert body["base_instructions"] == "Transcribe silently."
    assert body["allowed_replies"] == []
    assert body["confidence_threshold"] == 0.7
    assert body["meeting_config_count"] == 0


def test_create_strips_blank_allowed_replies(client: TestClient) -> None:
    resp = client.post(
        "/templates",
        json=_create_payload(allowed_replies=["yes", "", "  ", "no thanks  "]),
    )
    assert resp.status_code == 201
    assert resp.json()["allowed_replies"] == ["yes", "no thanks"]


def test_create_rejects_blank_name(client: TestClient) -> None:
    resp = client.post("/templates", json=_create_payload(name=""))
    assert resp.status_code == 422


def test_create_rejects_bad_mode(client: TestClient) -> None:
    resp = client.post("/templates", json=_create_payload(mode="not-a-mode"))
    assert resp.status_code == 422


def test_create_rejects_threshold_out_of_range(client: TestClient) -> None:
    resp = client.post("/templates", json=_create_payload(confidence_threshold=2.0))
    assert resp.status_code == 422
    resp = client.post("/templates", json=_create_payload(confidence_threshold=-0.1))
    assert resp.status_code == 422


def test_create_limited_auto_speak_requires_replies(client: TestClient) -> None:
    resp = client.post(
        "/templates",
        json=_create_payload(mode="limited_auto_speak", allowed_replies=[]),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "allowed_replies" in str(body["detail"])


def test_create_limited_auto_speak_with_replies_ok(client: TestClient) -> None:
    resp = client.post(
        "/templates",
        json=_create_payload(
            mode="limited_auto_speak",
            allowed_replies=["Yes", "No", "Could you repeat that?"],
        ),
    )
    assert resp.status_code == 201
    assert resp.json()["allowed_replies"] == ["Yes", "No", "Could you repeat that?"]


def test_create_autonomous_requires_instructions(client: TestClient) -> None:
    resp = client.post(
        "/templates",
        json=_create_payload(mode="autonomous", base_instructions=""),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "base_instructions" in str(body["detail"])
    assert "autonomous" in str(body["detail"])


def test_create_autonomous_whitespace_only_instructions_rejected(
    client: TestClient,
) -> None:
    resp = client.post(
        "/templates",
        json=_create_payload(mode="autonomous", base_instructions="   \n\t "),
    )
    assert resp.status_code == 422


def test_create_autonomous_with_instructions_ok(client: TestClient) -> None:
    resp = client.post(
        "/templates",
        json=_create_payload(
            mode="autonomous",
            base_instructions="Speak when addressed by name.",
        ),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["mode"] == "autonomous"
    # Autonomous deliberately does NOT require allowed_replies; the empty
    # list shouldn't trigger any validation error.
    assert body["allowed_replies"] == []


def test_update_to_autonomous_without_instructions_fails(client: TestClient) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    # Clear instructions in same patch as the mode flip — server should
    # see the post-patch row with mode=autonomous + empty instructions
    # and reject.
    resp = client.patch(
        f"/templates/{created['id']}",
        json={"mode": "autonomous", "base_instructions": ""},
    )
    assert resp.status_code == 422
    assert "autonomous" in str(resp.json()["detail"])


def test_update_to_autonomous_with_instructions_ok(client: TestClient) -> None:
    created = client.post(
        "/templates",
        json=_create_payload(base_instructions="Run the meeting smoothly."),
    ).json()
    resp = client.patch(
        f"/templates/{created['id']}",
        json={"mode": "autonomous"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "autonomous"
    assert body["base_instructions"] == "Run the meeting smoothly."


def test_update_clearing_instructions_on_autonomous_fails(client: TestClient) -> None:
    created = client.post(
        "/templates",
        json=_create_payload(
            mode="autonomous",
            base_instructions="Help the host.",
        ),
    ).json()
    resp = client.patch(
        f"/templates/{created['id']}",
        json={"base_instructions": ""},
    )
    assert resp.status_code == 422


def test_create_duplicate_name_returns_409(client: TestClient) -> None:
    assert client.post("/templates", json=_create_payload()).status_code == 201
    resp = client.post("/templates", json=_create_payload())
    assert resp.status_code == 409


# --- get ------------------------------------------------------------------


def test_get_template(client: TestClient) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    resp = client.get(f"/templates/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == created["name"]


def test_get_missing_returns_404(client: TestClient) -> None:
    assert client.get("/templates/9999").status_code == 404


# --- update ---------------------------------------------------------------


def test_update_name_only(client: TestClient) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    resp = client.patch(f"/templates/{created['id']}", json={"name": "Renamed"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["mode"] == "listen_only"


def test_update_mode_to_limited_without_replies_fails(client: TestClient) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    resp = client.patch(
        f"/templates/{created['id']}",
        json={"mode": "limited_auto_speak"},
    )
    assert resp.status_code == 422


def test_update_mode_to_limited_with_replies_ok(client: TestClient) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    resp = client.patch(
        f"/templates/{created['id']}",
        json={"mode": "limited_auto_speak", "allowed_replies": ["yes", "no"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "limited_auto_speak"
    assert body["allowed_replies"] == ["yes", "no"]


def test_update_clearing_replies_on_limited_fails(client: TestClient) -> None:
    created = client.post(
        "/templates",
        json=_create_payload(
            mode="limited_auto_speak",
            allowed_replies=["yes", "no"],
        ),
    ).json()
    resp = client.patch(
        f"/templates/{created['id']}",
        json={"allowed_replies": []},
    )
    assert resp.status_code == 422


def test_update_threshold(client: TestClient) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    resp = client.patch(
        f"/templates/{created['id']}",
        json={"confidence_threshold": 0.85},
    )
    assert resp.status_code == 200
    assert resp.json()["confidence_threshold"] == 0.85


def test_update_duplicate_name_returns_409(client: TestClient) -> None:
    a = client.post("/templates", json=_create_payload(name="A")).json()
    client.post("/templates", json=_create_payload(name="B"))
    resp = client.patch(f"/templates/{a['id']}", json={"name": "B"})
    assert resp.status_code == 409


def test_update_missing_returns_404(client: TestClient) -> None:
    resp = client.patch("/templates/9999", json={"name": "x"})
    assert resp.status_code == 404


# --- delete ---------------------------------------------------------------


def test_delete_unused_template(client: TestClient, db_session: Session) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    resp = client.delete(f"/templates/{created['id']}")
    assert resp.status_code == 204
    assert db_session.get(ProfileTemplate, created["id"]) is None


def test_delete_missing_returns_404(client: TestClient) -> None:
    resp = client.delete("/templates/9999")
    assert resp.status_code == 404


def _make_referencing_meeting_config(
    session: Session, template_id: int
) -> MeetingConfig:
    from datetime import UTC, datetime, timedelta

    account = GoogleAccount(
        email="user@example.com",
        refresh_token_encrypted="x",
    )
    session.add(account)
    session.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id="evt-1",
        start_time=datetime.now(UTC),
        end_time=datetime.now(UTC) + timedelta(minutes=30),
    )
    session.add(event)
    session.flush()
    config = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=template_id,
        identity_account_id=account.id,
        mode=BotMode.LISTEN_ONLY,
    )
    session.add(config)
    session.commit()
    return config


def test_delete_referenced_returns_409_by_default(
    client: TestClient, db_session: Session
) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    _make_referencing_meeting_config(db_session, created["id"])
    resp = client.delete(f"/templates/{created['id']}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["meeting_config_count"] == 1
    assert "force" in detail["message"]


def test_delete_referenced_with_force_cascades(
    client: TestClient, db_session: Session
) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    config = _make_referencing_meeting_config(db_session, created["id"])
    config_id = config.id
    resp = client.delete(f"/templates/{created['id']}?force=true")
    assert resp.status_code == 204
    assert db_session.get(ProfileTemplate, created["id"]) is None
    assert db_session.get(MeetingConfig, config_id) is None


def test_list_includes_meeting_config_count(
    client: TestClient, db_session: Session
) -> None:
    created = client.post("/templates", json=_create_payload()).json()
    _make_referencing_meeting_config(db_session, created["id"])
    resp = client.get("/templates")
    assert resp.status_code == 200
    assert resp.json()[0]["meeting_config_count"] == 1


# --- seeding --------------------------------------------------------------


def test_seed_initial_templates_inserts_both(db_session: Session) -> None:
    from app.services.templates import seed_initial_templates

    inserted = seed_initial_templates(db_session)
    assert len(inserted) == 2
    names = sorted(t.name for t in inserted)
    assert names == ["Approval-required client call", "Listen-only standup"]


def test_seed_initial_templates_is_idempotent(db_session: Session) -> None:
    from app.services.templates import seed_initial_templates

    seed_initial_templates(db_session)
    second = seed_initial_templates(db_session)
    assert second == []


def test_seed_initial_templates_skips_existing_by_name(db_session: Session) -> None:
    from app.services.templates import seed_initial_templates

    db_session.add(
        ProfileTemplate(
            name="Listen-only standup",
            mode=BotMode.LISTEN_ONLY,
            base_instructions="custom",
            base_context="custom",
            allowed_replies=[],
            confidence_threshold=0.5,
        )
    )
    db_session.commit()

    inserted = seed_initial_templates(db_session)
    # Only the second template inserted; the first is left untouched.
    assert len(inserted) == 1
    assert inserted[0].name == "Approval-required client call"


def test_seeded_listen_only_template_shape(db_session: Session) -> None:
    from app.services.templates import seed_initial_templates

    seed_initial_templates(db_session)
    row = db_session.scalar(
        sa.select(ProfileTemplate).where(ProfileTemplate.name == "Listen-only standup")
    )
    assert row is not None
    assert row.mode is BotMode.LISTEN_ONLY


def test_seeded_approval_template_shape(db_session: Session) -> None:
    from app.services.templates import seed_initial_templates

    seed_initial_templates(db_session)
    row = db_session.scalar(
        sa.select(ProfileTemplate).where(
            ProfileTemplate.name == "Approval-required client call"
        )
    )
    assert row is not None
    assert row.mode is BotMode.APPROVAL_REQUIRED
