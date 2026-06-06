"""Tests for app.services.session_scheduler (US-029)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    AccountRole,
    BotMode,
    BotSession,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    ProfileTemplate,
)
from app.services.session_scheduler import (
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    ContainerLauncher,
    LaunchContext,
    LauncherError,
    LaunchResult,
    NoopContainerLauncher,
    container_name_for_session,
    get_scheduler_interval_seconds,
    list_active_sessions,
    run_scheduler_pass_with_session,
    select_due_meetings,
    select_due_stops,
    start_session_for_meeting,
    stop_session_by_id,
)

# --- Fixtures --------------------------------------------------------------


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


def _seed_full_meeting(
    sess: Session,
    *,
    start_offset: timedelta,
    end_offset: timedelta,
    meet_link: str | None = "https://meet.google.com/abc-defg-hij",
    enabled: bool = True,
    external_id: str = "evt-1",
) -> MeetingConfig:
    """Insert account + event + template + meeting_config in one go."""
    now = datetime.now(UTC).replace(microsecond=0)
    account = GoogleAccount(
        email=f"u-{external_id}@example.com",
        role=AccountRole.USER,
        refresh_token_encrypted="x",
        is_default_user=False,
    )
    sess.add(account)
    sess.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id=external_id,
        start_time=now + start_offset,
        end_time=now + end_offset,
        meet_link=meet_link,
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
    sess.flush()
    return cfg


# --- Interval helper -------------------------------------------------------


def test_get_scheduler_interval_seconds_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JOHNNY_SCHEDULER_INTERVAL_SECONDS", None)
        assert get_scheduler_interval_seconds() == DEFAULT_SCHEDULER_INTERVAL_SECONDS


def test_get_scheduler_interval_seconds_override() -> None:
    with patch.dict(os.environ, {"JOHNNY_SCHEDULER_INTERVAL_SECONDS": "30"}):
        assert get_scheduler_interval_seconds() == 30


def test_get_scheduler_interval_seconds_clamps_to_one() -> None:
    with patch.dict(os.environ, {"JOHNNY_SCHEDULER_INTERVAL_SECONDS": "0"}):
        assert get_scheduler_interval_seconds() == 1
    with patch.dict(os.environ, {"JOHNNY_SCHEDULER_INTERVAL_SECONDS": "-12"}):
        assert get_scheduler_interval_seconds() == 1


def test_get_scheduler_interval_seconds_falls_back_on_garbage() -> None:
    with patch.dict(os.environ, {"JOHNNY_SCHEDULER_INTERVAL_SECONDS": "not-a-number"}):
        assert get_scheduler_interval_seconds() == DEFAULT_SCHEDULER_INTERVAL_SECONDS


def test_container_name_for_session() -> None:
    assert container_name_for_session(42) == "meet-worker-session-42"


# --- select_due_meetings ---------------------------------------------------


def test_select_due_meetings_picks_meeting_within_window(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    due = select_due_meetings(db_session)
    assert [m.id for m in due] == [cfg.id]


def test_select_due_meetings_skips_meetings_outside_window(
    db_session: Session,
) -> None:
    _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=10),
        end_offset=timedelta(minutes=40),
    )
    due = select_due_meetings(db_session)
    assert due == []


def test_select_due_meetings_skips_disabled(db_session: Session) -> None:
    _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
        enabled=False,
    )
    due = select_due_meetings(db_session)
    assert due == []


def test_select_due_meetings_skips_missing_meet_link(db_session: Session) -> None:
    _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
        meet_link=None,
    )
    due = select_due_meetings(db_session)
    assert due == []


def test_select_due_meetings_skips_ended_events(db_session: Session) -> None:
    _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-30),
        end_offset=timedelta(minutes=-1),
    )
    due = select_due_meetings(db_session)
    assert due == []


def test_select_due_meetings_skips_meeting_with_active_session(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    # Insert an active row (joined) for the meeting.
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.JOINED)
    )
    db_session.flush()
    assert select_due_meetings(db_session) == []


def test_select_due_meetings_includes_meeting_with_only_ended_sessions(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.ENDED)
    )
    db_session.flush()
    due = select_due_meetings(db_session)
    assert [m.id for m in due] == [cfg.id]


def test_select_due_meetings_respects_custom_window(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=5),
        end_offset=timedelta(minutes=35),
    )
    # 2-minute window: 5 min start is outside.
    assert select_due_meetings(db_session, join_window_seconds=120) == []
    # 10-minute window includes it.
    due = select_due_meetings(db_session, join_window_seconds=600)
    assert [m.id for m in due] == [cfg.id]


# --- select_due_stops ------------------------------------------------------


def test_select_due_stops_returns_active_session_for_ended_event(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-30),
        end_offset=timedelta(minutes=-2),
    )
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.flush()
    due = select_due_stops(db_session)
    assert [r.id for r in due] == [row.id]


def test_select_due_stops_skips_terminal_sessions(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-30),
        end_offset=timedelta(minutes=-5),
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.ENDED)
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.FAILED)
    )
    db_session.flush()
    assert select_due_stops(db_session) == []


def test_select_due_stops_skips_recently_ended_event(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-30),
        end_offset=timedelta(seconds=-10),
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.JOINED)
    )
    db_session.flush()
    # default grace = 60s; event ended 10s ago so it shouldn't trigger.
    assert select_due_stops(db_session) == []


def test_select_due_stops_honors_custom_grace(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-30),
        end_offset=timedelta(seconds=-5),
    )
    row = BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.JOINED)
    db_session.add(row)
    db_session.flush()
    due = select_due_stops(db_session, stop_grace_seconds=0)
    assert [r.id for r in due] == [row.id]


# --- list_active_sessions --------------------------------------------------


def test_list_active_sessions_excludes_terminal(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=1),
        end_offset=timedelta(minutes=30),
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.SCHEDULED)
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.JOINED)
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.ENDED)
    )
    db_session.add(
        BotSession(meeting_config_id=cfg.id, status=BotSessionStatus.FAILED)
    )
    db_session.flush()
    rows = list_active_sessions(db_session)
    statuses = sorted(r.status for r in rows)
    assert statuses == sorted(
        [BotSessionStatus.SCHEDULED, BotSessionStatus.JOINED]
    )


# --- start_session_for_meeting --------------------------------------------


@pytest.mark.asyncio
async def test_start_session_creates_row_and_calls_launcher(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    launcher = NoopContainerLauncher()
    row = await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    db_session.refresh(row)
    assert row.status == BotSessionStatus.JOINING
    assert row.container_name == container_name_for_session(row.id)
    assert len(launcher.started) == 1
    ctx = launcher.started[0]
    assert ctx.meeting_config_id == cfg.id
    assert ctx.calendar_event_id == cfg.calendar_event_id
    assert ctx.identity_account_id == cfg.identity_account_id
    assert ctx.bot_session_id == row.id
    assert ctx.meet_link == "https://meet.google.com/abc-defg-hij"


@pytest.mark.asyncio
async def test_start_session_passes_instructions_and_context_to_launcher(
    db_session: Session,
) -> None:
    """Effective instructions/context = template base + meeting override."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    # Customize template + meeting overrides
    cfg.profile_template.base_instructions = "Be polite."
    cfg.profile_template.base_context = "Engineering team standup."
    cfg.instructions = "Stay quiet unless asked."
    cfg.context = "Today: new hire intros."
    db_session.flush()

    launcher = NoopContainerLauncher()
    await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    assert len(launcher.started) == 1
    ctx = launcher.started[0]
    assert ctx.instructions == "Be polite.\n\nStay quiet unless asked."
    assert ctx.context == "Engineering team standup.\n\nToday: new hire intros."
    assert ctx.mode == BotMode.LISTEN_ONLY.value
    # No calendar description was set → empty calendar_context.
    assert ctx.calendar_context == ""


@pytest.mark.asyncio
async def test_start_session_passes_calendar_description_as_context(
    db_session: Session,
) -> None:
    """Johnny-ckz.3: calendar event description rides into LaunchContext."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    cfg.calendar_event.description = "Q3 launch readiness review.\nAttendees: leads."
    db_session.flush()

    launcher = NoopContainerLauncher()
    await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    ctx = launcher.started[0]
    assert ctx.calendar_context == (
        "Q3 launch readiness review.\nAttendees: leads."
    )


@pytest.mark.asyncio
async def test_start_session_handles_empty_override_text(
    db_session: Session,
) -> None:
    """Missing overrides fall back to template-only text — no extra separator."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    cfg.profile_template.base_instructions = "Only template instructions."
    cfg.instructions = None
    db_session.flush()
    launcher = NoopContainerLauncher()
    await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    assert launcher.started[0].instructions == "Only template instructions."


@pytest.mark.asyncio
async def test_start_session_rejects_disabled_meeting(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
        enabled=False,
    )
    launcher = NoopContainerLauncher()
    with pytest.raises(ValueError, match="disabled"):
        await start_session_for_meeting(
            db_session, meeting=cfg, launcher=launcher
        )
    assert launcher.started == []


@pytest.mark.asyncio
async def test_start_session_rejects_missing_meet_link(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
        meet_link=None,
    )
    launcher = NoopContainerLauncher()
    with pytest.raises(ValueError, match="meet_link"):
        await start_session_for_meeting(
            db_session, meeting=cfg, launcher=launcher
        )


class _FlakyLauncher(ContainerLauncher):
    async def start(self, ctx: LaunchContext) -> LaunchResult:
        raise LauncherError("docker down")

    async def stop(self, *, bot_session_id: int, container_name: str | None) -> None:
        return


@pytest.mark.asyncio
async def test_start_session_records_launcher_failure_on_row(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
    )
    launcher = _FlakyLauncher()
    with pytest.raises(LauncherError):
        await start_session_for_meeting(
            db_session, meeting=cfg, launcher=launcher
        )
    rows = db_session.scalars(
        sa.select(BotSession).where(BotSession.meeting_config_id == cfg.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].status == BotSessionStatus.FAILED
    assert rows[0].error_reason is not None
    assert "docker down" in rows[0].error_reason


# --- stop_session_by_id ---------------------------------------------------


@pytest.mark.asyncio
async def test_stop_session_marks_ended(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
    )
    row = BotSession(
        meeting_config_id=cfg.id,
        status=BotSessionStatus.JOINED,
        container_name="meet-worker-session-1",
    )
    db_session.add(row)
    db_session.flush()
    launcher = NoopContainerLauncher()
    result = await stop_session_by_id(
        db_session, bot_session_id=row.id, launcher=launcher
    )
    db_session.refresh(row)
    assert result.status == BotSessionStatus.ENDED
    assert row.ended_at is not None
    assert launcher.stopped == [(row.id, "meet-worker-session-1")]


@pytest.mark.asyncio
async def test_stop_session_is_idempotent_for_terminal(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
    )
    row = BotSession(
        meeting_config_id=cfg.id,
        status=BotSessionStatus.ENDED,
        ended_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.add(row)
    db_session.flush()
    launcher = NoopContainerLauncher()
    await stop_session_by_id(
        db_session, bot_session_id=row.id, launcher=launcher
    )
    # Launcher was not invoked because the row is already terminal.
    assert launcher.stopped == []


@pytest.mark.asyncio
async def test_stop_session_raises_for_unknown_id(db_session: Session) -> None:
    from app.services.bot_sessions import BotSessionNotFoundError

    launcher = NoopContainerLauncher()
    with pytest.raises(BotSessionNotFoundError):
        await stop_session_by_id(
            db_session, bot_session_id=9999, launcher=launcher
        )


class _StopFailLauncher(ContainerLauncher):
    async def start(self, ctx: LaunchContext) -> LaunchResult:
        return LaunchResult(container_name=ctx.container_name)

    async def stop(self, *, bot_session_id: int, container_name: str | None) -> None:
        raise LauncherError("docker stop failed")


@pytest.mark.asyncio
async def test_stop_session_marks_failed_on_launcher_error(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
    )
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.flush()
    with pytest.raises(LauncherError):
        await stop_session_by_id(
            db_session, bot_session_id=row.id, launcher=_StopFailLauncher()
        )
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason is not None
    assert "docker stop failed" in row.error_reason


# --- run_scheduler_pass ----------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_pass_starts_due_and_stops_old(db_session: Session) -> None:
    # Meeting starting now → should start.
    due = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
        external_id="evt-due",
    )
    # Old meeting ended 5 min ago → should stop.
    old = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-60),
        end_offset=timedelta(minutes=-5),
        external_id="evt-old",
    )
    old_row = BotSession(
        meeting_config_id=old.id, status=BotSessionStatus.JOINED
    )
    db_session.add(old_row)
    db_session.flush()

    launcher = NoopContainerLauncher()
    result = await run_scheduler_pass_with_session(
        db_session, launcher=launcher
    )
    assert result.started_count == 1
    assert result.stopped_count == 1
    assert result.error_count == 0

    # Verify the new session row was created and old one transitioned.
    new_row = db_session.scalar(
        sa.select(BotSession).where(BotSession.meeting_config_id == due.id)
    )
    assert new_row is not None
    assert new_row.status == BotSessionStatus.JOINING

    db_session.refresh(old_row)
    assert old_row.status == BotSessionStatus.ENDED


@pytest.mark.asyncio
async def test_scheduler_pass_counts_launcher_errors(db_session: Session) -> None:
    _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=10),
        end_offset=timedelta(minutes=30),
    )
    result = await run_scheduler_pass_with_session(
        db_session, launcher=_FlakyLauncher()
    )
    assert result.started_count == 0
    assert result.error_count == 1
