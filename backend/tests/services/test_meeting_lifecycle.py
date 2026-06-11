"""Tests for app.services.meeting_lifecycle (Johnny-trt.56).

Covers the occurrence-scoped dismissal predicate, the derived bot_state
precedence, dismiss/un-dismiss (column stamps, session stops, event
publishing), and the scheduler dispatch matrix
(enabled × dismissal × window × active-session).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    BotDismissActor,
    BotMode,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    PipelineSettings,
    ProfileTemplate,
)
from app.services.meeting_lifecycle import (
    BOT_STATE_EVENT_TYPE,
    GLOBAL_CALENDAR_CHANNEL,
    MeetingBotState,
    derive_bot_state,
    dismiss_bot_for_meeting,
    dismissal_in_force,
    undismiss_bot_for_meeting,
)
from app.services.session_scheduler import (
    NoopContainerLauncher,
    select_due_meetings,
)

# --- Fixtures ---------------------------------------------------------------


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
            PipelineSettings.__table__,  # type: ignore[list-item]
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


NOW = datetime.now(UTC).replace(microsecond=0)


def _seed_meeting(
    sess: Session,
    *,
    start: datetime,
    end: datetime,
    enabled: bool = True,
    external_id: str = "evt-1",
) -> MeetingConfig:
    account = GoogleAccount(
        email=f"u-{external_id}@example.com",
        refresh_token_encrypted="x",
    )
    sess.add(account)
    sess.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id=external_id,
        start_time=start,
        end_time=end,
        meet_link="https://meet.google.com/abc-defg-hij",
    )
    sess.add(event)
    sess.flush()
    template = ProfileTemplate(
        name=f"tpl-{external_id}",
        mode=BotMode.LISTEN_ONLY,
        base_instructions="",
        base_context="",
        allowed_replies=[],
        confidence_threshold=0.7,
    )
    sess.add(template)
    sess.flush()
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=template.id,
        identity_account_id=account.id,
        mode=BotMode.LISTEN_ONLY,
        enabled=enabled,
    )
    sess.add(cfg)
    sess.commit()
    sess.refresh(cfg)
    return cfg


def _dismiss_columns(
    cfg: MeetingConfig,
    *,
    at: datetime,
    until: datetime,
    by: BotDismissActor = BotDismissActor.UI,
) -> None:
    cfg.bot_dismissed_at = at
    cfg.bot_dismissed_by = by
    cfg.bot_dismissed_until = until


def _add_session(
    sess: Session, cfg: MeetingConfig, status: BotSessionStatus
) -> BotSession:
    row = BotSession(meeting_config_id=cfg.id, status=status)
    sess.add(row)
    sess.commit()
    sess.refresh(row)
    return row


class RecordingPublisher:
    """Collects (channel, payload) pairs the lifecycle helpers publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, channel: str, payload: dict[str, Any]) -> None:
        self.published.append((channel, payload))


# --- dismissal_in_force ------------------------------------------------------


def test_not_dismissed_when_columns_null(db_session: Session) -> None:
    cfg = _seed_meeting(
        db_session, start=NOW + timedelta(minutes=1), end=NOW + timedelta(hours=1)
    )
    assert dismissal_in_force(cfg) is False


def test_in_force_while_event_start_inside_dismissed_window(
    db_session: Session,
) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    assert dismissal_in_force(cfg) is True


def test_lapses_when_event_moved_past_dismissed_window(db_session: Session) -> None:
    """A reschedule beyond the captured window is a new occurrence."""
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    cfg.calendar_event.start_time = NOW + timedelta(days=1)
    cfg.calendar_event.end_time = NOW + timedelta(days=1, hours=1)
    db_session.commit()
    assert dismissal_in_force(cfg) is False


def test_stays_in_force_when_meeting_extended(db_session: Session) -> None:
    """Pushing end_time later keeps start inside the window → still dismissed."""
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    cfg.calendar_event.end_time = NOW + timedelta(hours=2)
    db_session.commit()
    assert dismissal_in_force(cfg) is True


# --- derive_bot_state ---------------------------------------------------------


def test_state_active_wins_over_dismissed(db_session: Session) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    state = derive_bot_state(cfg, active_session=True, now=NOW)
    assert state is MeetingBotState.ACTIVE


def test_state_dismissed_while_occurrence_open(db_session: Session) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    state = derive_bot_state(cfg, active_session=False, now=NOW + timedelta(minutes=5))
    assert state is MeetingBotState.DISMISSED


def test_state_ended_after_occurrence_even_if_dismissed(db_session: Session) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    state = derive_bot_state(cfg, active_session=False, now=NOW + timedelta(hours=2))
    assert state is MeetingBotState.ENDED


def test_state_scheduled_for_clean_upcoming_meeting(db_session: Session) -> None:
    cfg = _seed_meeting(
        db_session, start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2)
    )
    state = derive_bot_state(cfg, active_session=False, now=NOW)
    assert state is MeetingBotState.SCHEDULED


# --- dismiss_bot_for_meeting ---------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_stamps_columns_and_stops_sessions(
    db_session: Session,
) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    live = _add_session(db_session, cfg, BotSessionStatus.JOINED)
    launcher = NoopContainerLauncher()
    publisher = RecordingPublisher()

    result = await dismiss_bot_for_meeting(
        db_session,
        meeting=cfg,
        actor=BotDismissActor.UI,
        launcher=launcher,
        publisher=publisher,
    )
    db_session.commit()

    assert cfg.bot_dismissed_at is not None
    assert cfg.bot_dismissed_by is BotDismissActor.UI
    until = cfg.bot_dismissed_until
    assert until is not None
    expected_until = cfg.calendar_event.end_time
    if expected_until.tzinfo is None:
        expected_until = expected_until.replace(tzinfo=UTC)
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    assert until == expected_until
    assert result.stopped_session_ids == (live.id,)
    assert result.stop_errors == ()
    assert launcher.stopped == [(live.id, None)]
    db_session.refresh(live)
    assert live.status is BotSessionStatus.ENDED


@pytest.mark.asyncio
async def test_dismiss_publishes_global_and_session_events(
    db_session: Session,
) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    live = _add_session(db_session, cfg, BotSessionStatus.JOINED)
    publisher = RecordingPublisher()

    await dismiss_bot_for_meeting(
        db_session,
        meeting=cfg,
        actor=BotDismissActor.VOICE,
        launcher=NoopContainerLauncher(),
        publisher=publisher,
    )

    channels = [c for c, _ in publisher.published]
    assert channels == [GLOBAL_CALENDAR_CHANNEL, f"johnny.session.{live.id}"]
    for _, payload in publisher.published:
        assert payload["type"] == BOT_STATE_EVENT_TYPE
        assert payload["bot_state"] == "dismissed"
        assert payload["dismissed_by"] == "voice"
        assert payload["meeting_config_id"] == cfg.id
        assert payload["calendar_event_id"] == cfg.calendar_event_id
        assert payload["stopped_session_ids"] == [live.id]
        assert payload["dismissed_at"] is not None
        assert payload["dismissed_until"] is not None


@pytest.mark.asyncio
async def test_dismiss_without_active_session_publishes_global_only(
    db_session: Session,
) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    publisher = RecordingPublisher()

    result = await dismiss_bot_for_meeting(
        db_session,
        meeting=cfg,
        actor=BotDismissActor.UI,
        launcher=NoopContainerLauncher(),
        publisher=publisher,
    )

    assert result.stopped_session_ids == ()
    assert [c for c, _ in publisher.published] == [GLOBAL_CALENDAR_CHANNEL]


@pytest.mark.asyncio
async def test_dismiss_survives_launcher_stop_failure(db_session: Session) -> None:
    """The durable dismissal lands even when the container stop blows up."""

    class ExplodingLauncher(NoopContainerLauncher):
        async def stop(
            self, *, bot_session_id: int, container_name: str | None
        ) -> None:
            raise RuntimeError("docker is gone")

    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    live = _add_session(db_session, cfg, BotSessionStatus.JOINED)
    publisher = RecordingPublisher()

    result = await dismiss_bot_for_meeting(
        db_session,
        meeting=cfg,
        actor=BotDismissActor.UI,
        launcher=ExplodingLauncher(),
        publisher=publisher,
    )

    assert cfg.bot_dismissed_at is not None
    assert result.stopped_session_ids == ()
    assert len(result.stop_errors) == 1
    assert f"session {live.id}" in result.stop_errors[0]
    # stop_session_by_id marked the row failed on the launcher error.
    db_session.refresh(live)
    assert live.status is BotSessionStatus.FAILED
    # Still announced on the global channel (state DID change).
    assert [c for c, _ in publisher.published] == [GLOBAL_CALENDAR_CHANNEL]


@pytest.mark.asyncio
async def test_dismiss_publish_failure_does_not_raise(db_session: Session) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))

    async def exploding_publisher(channel: str, payload: dict[str, Any]) -> None:
        raise ConnectionError("redis down")

    result = await dismiss_bot_for_meeting(
        db_session,
        meeting=cfg,
        actor=BotDismissActor.UI,
        launcher=NoopContainerLauncher(),
        publisher=exploding_publisher,
    )
    assert result.meeting.bot_dismissed_at is not None


# --- undismiss_bot_for_meeting --------------------------------------------------


@pytest.mark.asyncio
async def test_undismiss_clears_columns_and_publishes(db_session: Session) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    publisher = RecordingPublisher()

    await undismiss_bot_for_meeting(db_session, meeting=cfg, publisher=publisher)
    db_session.commit()

    assert cfg.bot_dismissed_at is None
    assert cfg.bot_dismissed_by is None
    assert cfg.bot_dismissed_until is None
    assert [c for c, _ in publisher.published] == [GLOBAL_CALENDAR_CHANNEL]
    payload = publisher.published[0][1]
    assert payload["bot_state"] in ("scheduled", "ended")
    assert payload["dismissed_at"] is None


@pytest.mark.asyncio
async def test_undismiss_is_noop_without_dismissal(db_session: Session) -> None:
    cfg = _seed_meeting(db_session, start=NOW, end=NOW + timedelta(hours=1))
    publisher = RecordingPublisher()

    await undismiss_bot_for_meeting(db_session, meeting=cfg, publisher=publisher)

    # No phantom transition in the activity log.
    assert publisher.published == []


# --- Scheduler dispatch matrix --------------------------------------------------


def _matrix_meeting(
    db_session: Session,
    *,
    enabled: bool,
    dismissed: bool,
    in_window: bool,
    active_session: bool,
    external_id: str,
) -> MeetingConfig:
    start = NOW + (timedelta(minutes=1) if in_window else timedelta(hours=6))
    end = start + timedelta(hours=1)
    cfg = _seed_meeting(
        db_session, start=start, end=end, enabled=enabled, external_id=external_id
    )
    if dismissed:
        _dismiss_columns(cfg, at=NOW, until=end)
    if active_session:
        _add_session(db_session, cfg, BotSessionStatus.JOINED)
    db_session.commit()
    return cfg


@pytest.mark.parametrize(
    ("enabled", "dismissed", "in_window", "active_session", "expect_due"),
    [
        # The one dispatchable combination.
        (True, False, True, False, True),
        # Dismissal blocks dispatch even with everything else green.
        (True, True, True, False, False),
        # Disabled blocks regardless of dismissal.
        (False, False, True, False, False),
        (False, True, True, False, False),
        # Out of window never dispatches.
        (True, False, False, False, False),
        (True, True, False, False, False),
        # An active session blocks (with and without dismissal).
        (True, False, True, True, False),
        (True, True, True, True, False),
    ],
)
def test_dispatch_matrix(
    db_session: Session,
    enabled: bool,
    dismissed: bool,
    in_window: bool,
    active_session: bool,
    expect_due: bool,
) -> None:
    cfg = _matrix_meeting(
        db_session,
        enabled=enabled,
        dismissed=dismissed,
        in_window=in_window,
        active_session=active_session,
        external_id="evt-matrix",
    )
    due = select_due_meetings(db_session, now=NOW)
    assert ([m.id for m in due] == [cfg.id]) is expect_due


def test_dispatch_resumes_when_event_moved_past_dismissed_window(
    db_session: Session,
) -> None:
    """Same-row reschedule beyond the window = new occurrence → dispatch again."""
    cfg = _seed_meeting(
        db_session, start=NOW + timedelta(minutes=1), end=NOW + timedelta(hours=1)
    )
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    assert select_due_meetings(db_session, now=NOW) == []

    # Organizer moves the meeting to tomorrow; poll near the new start.
    new_start = NOW + timedelta(days=1)
    cfg.calendar_event.start_time = new_start
    cfg.calendar_event.end_time = new_start + timedelta(hours=1)
    db_session.commit()
    due = select_due_meetings(db_session, now=new_start - timedelta(minutes=1))
    assert [m.id for m in due] == [cfg.id]


def test_dispatch_next_occurrence_of_recurring_series_unaffected(
    db_session: Session,
) -> None:
    """Dismissing today's occurrence leaves next week's row dispatchable."""
    today = _seed_meeting(
        db_session,
        start=NOW + timedelta(minutes=1),
        end=NOW + timedelta(hours=1),
        external_id="evt-recur-1",
    )
    next_week = _seed_meeting(
        db_session,
        start=NOW + timedelta(days=7),
        end=NOW + timedelta(days=7, hours=1),
        external_id="evt-recur-2",
    )
    today.calendar_event.recurring_event_id = "series-1"
    next_week.calendar_event.recurring_event_id = "series-1"
    _dismiss_columns(today, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()

    assert select_due_meetings(db_session, now=NOW) == []
    due_next = select_due_meetings(
        db_session, now=NOW + timedelta(days=7, minutes=-1)
    )
    assert [m.id for m in due_next] == [next_week.id]


def test_undismissed_meeting_is_due_again(db_session: Session) -> None:
    cfg = _seed_meeting(
        db_session, start=NOW + timedelta(minutes=1), end=NOW + timedelta(hours=1)
    )
    _dismiss_columns(cfg, at=NOW, until=NOW + timedelta(hours=1))
    db_session.commit()
    assert select_due_meetings(db_session, now=NOW) == []

    cfg.bot_dismissed_at = None
    cfg.bot_dismissed_by = None
    cfg.bot_dismissed_until = None
    db_session.commit()
    assert [m.id for m in select_due_meetings(db_session, now=NOW)] == [cfg.id]
