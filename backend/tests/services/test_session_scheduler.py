"""Tests for app.services.session_scheduler (US-029)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base
from app.db.models import (
    Agent,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingAgent,
    MeetingConfig,
)
from app.services.session_scheduler import (
    DEFAULT_RELOGIN_TTL_SECONDS,
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    MAX_AGENTS_PER_MEETING,
    ContainerLauncher,
    LaunchContext,
    LauncherError,
    LaunchResult,
    NoopContainerLauncher,
    container_name_for_session,
    get_scheduler_interval_seconds,
    list_active_sessions,
    pending_assignments,
    run_scheduler_pass_with_session,
    select_due_meetings,
    select_due_stops,
    select_relogin_to_settle,
    start_session_for_meeting,
    start_sessions_for_meeting,
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


def _seed_full_meeting(
    sess: Session,
    *,
    start_offset: timedelta,
    end_offset: timedelta,
    meet_link: str | None = "https://meet.google.com/abc-defg-hij",
    enabled: bool = True,
    external_id: str = "evt-1",
) -> MeetingConfig:
    """Insert account + event + meeting_config in one go."""
    now = datetime.now(UTC).replace(microsecond=0)
    account = GoogleAccount(
        email=f"u-{external_id}@example.com",
        refresh_token_encrypted="x",
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
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        identity_account_id=account.id,
        enabled=enabled,
    )
    sess.add(cfg)
    sess.flush()
    return cfg


def _seed_agent(
    sess: Session,
    *,
    name: str,
    mode: BotMode = BotMode.AUTONOMOUS,
    character_prompt: str = "",
    allowed_replies: list[str] | None = None,
    confidence_threshold: float = 0.7,
    is_default: bool = False,
    meeting_bot_account_id: int | None = None,
) -> Agent:
    row = Agent(
        name=name,
        character_prompt=character_prompt,
        mode=mode,
        allowed_replies=allowed_replies or [],
        confidence_threshold=confidence_threshold,
        is_default=is_default,
        meeting_bot_account_id=meeting_bot_account_id,
    )
    sess.add(row)
    sess.flush()
    return row


def _assign_agent(
    sess: Session,
    *,
    meeting: MeetingConfig,
    agent: Agent,
    context: str | None = None,
    enabled: bool = True,
    position: int = 0,
    identity_account_id: int | None = None,
) -> MeetingAgent:
    row = MeetingAgent(
        meeting_config_id=meeting.id,
        agent_id=agent.id,
        identity_account_id=identity_account_id,
        context=context,
        enabled=enabled,
        position=position,
    )
    sess.add(row)
    sess.flush()
    sess.refresh(meeting)
    return row


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


def test_select_due_stops_skips_waiting_for_relogin(db_session: Session) -> None:
    """A signed-out session past its meeting end is settled by the relogin
    sweep (→ failed), NOT the stop sweep (→ ended) — so it must be excluded
    from select_due_stops (Johnny-ebf)."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-30),
        end_offset=timedelta(minutes=-5),
    )
    db_session.add(
        BotSession(
            meeting_config_id=cfg.id,
            status=BotSessionStatus.WAITING_FOR_RELOGIN,
        )
    )
    db_session.flush()
    assert select_due_stops(db_session) == []


# --- select_relogin_to_settle (Johnny-ebf) ---------------------------------


def test_select_relogin_to_settle_picks_waiting_after_meeting_end(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-30),
        end_offset=timedelta(minutes=-2),
    )
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.WAITING_FOR_RELOGIN
    )
    db_session.add(row)
    db_session.flush()
    settle = select_relogin_to_settle(db_session)
    assert [r.id for r in settle] == [row.id]


def test_select_relogin_to_settle_skips_live_meeting_within_ttl(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-5),
        end_offset=timedelta(hours=2),
    )
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.WAITING_FOR_RELOGIN
    )
    db_session.add(row)
    db_session.flush()
    # Meeting still live and the wait just started → leave it waiting.
    assert select_relogin_to_settle(db_session) == []


def test_select_relogin_to_settle_picks_waiting_past_ttl_while_live(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-5),
        end_offset=timedelta(hours=2),
    )
    # Entered waiting longer ago than the TTL — operator never re-logged in.
    old = datetime.now(UTC).replace(microsecond=0) - timedelta(
        seconds=DEFAULT_RELOGIN_TTL_SECONDS + 60
    )
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.WAITING_FOR_RELOGIN
    )
    row.updated_at = old  # explicit value overrides server_default/onupdate
    db_session.add(row)
    db_session.flush()
    settle = select_relogin_to_settle(db_session)
    assert [r.id for r in settle] == [row.id]


def test_run_scheduler_pass_settles_waiting_to_failed(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=-30),
        end_offset=timedelta(minutes=-2),
    )
    row = BotSession(
        meeting_config_id=cfg.id, status=BotSessionStatus.WAITING_FOR_RELOGIN
    )
    db_session.add(row)
    db_session.flush()

    import asyncio

    result = asyncio.run(
        run_scheduler_pass_with_session(
            db_session, launcher=NoopContainerLauncher()
        )
    )
    assert result.settled_count == 1
    db_session.refresh(row)
    assert row.status == BotSessionStatus.FAILED
    assert row.error_reason is not None
    assert "signed out" in row.error_reason.lower()
    assert "meeting ended" in row.error_reason.lower()


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


def test_list_active_sessions_includes_waiting_for_relogin(
    db_session: Session,
) -> None:
    """waiting_for_relogin must stay in the active panel so the frontend keeps
    its WS subscription open and shows the status (Johnny-ebf)."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(minutes=1),
        end_offset=timedelta(minutes=30),
    )
    db_session.add(
        BotSession(
            meeting_config_id=cfg.id,
            status=BotSessionStatus.WAITING_FOR_RELOGIN,
        )
    )
    db_session.flush()
    rows = list_active_sessions(db_session)
    assert [r.status for r in rows] == [BotSessionStatus.WAITING_FOR_RELOGIN]


def test_select_due_meetings_skips_meeting_with_waiting_session(
    db_session: Session,
) -> None:
    """A meeting whose session is waiting for re-login must not be re-queued
    (no duplicate worker) — the existing row owns the recovery (Johnny-ebf)."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    db_session.add(
        BotSession(
            meeting_config_id=cfg.id,
            status=BotSessionStatus.WAITING_FOR_RELOGIN,
        )
    )
    db_session.flush()
    assert select_due_meetings(db_session) == []


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


# --- Johnny-trt.41: agent resolution at launch ------------------------------


@pytest.mark.asyncio
async def test_start_session_default_agent_stamps_row_and_ctx(
    db_session: Session,
) -> None:
    """(a) With no assignments, the ``is_default`` agent serves the session:
    its behavior is frozen onto row.agent_snapshot / row.bot_name and the
    LaunchContext reads the snapshot, never the live rows."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    agent = _seed_agent(
        db_session,
        name="Johnny",
        mode=BotMode.LIMITED_AUTO_SPEAK,
        character_prompt="You are Johnny, sharp and dry.",
        allowed_replies=["Yes.", "No."],
        confidence_threshold=0.62,
        is_default=True,
    )

    launcher = NoopContainerLauncher()
    row = await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    db_session.refresh(row)
    # The row freezes the serving agent.
    assert row.agent_id == agent.id
    assert row.bot_name == "Johnny"
    snapshot = row.agent_snapshot
    assert snapshot is not None
    assert snapshot["agent_id"] == agent.id
    assert snapshot["name"] == "Johnny"
    assert snapshot["mode"] == "limited_auto_speak"
    assert snapshot["character_prompt"] == "You are Johnny, sharp and dry."
    assert snapshot["allowed_replies"] == ["Yes.", "No."]
    assert snapshot["confidence_threshold"] == pytest.approx(0.62)
    assert snapshot["assignment_context"] is None
    assert set(snapshot["providers"]) == {
        "router_llm_provider_id",
        "answer_llm_provider_id",
        "reasoning_llm_provider_id",
        "tts_provider_id",
        "tts_voice_id",
        "tts_options",
    }
    # The launch context carries the SAME snapshot dict (Johnny-trt.45);
    # behavior derives from it, never from per-field overrides.
    ctx = launcher.started[0]
    assert ctx.agent_id == agent.id
    assert ctx.agent_snapshot == snapshot


@pytest.mark.asyncio
async def test_start_sessions_launches_one_session_per_enabled_assignment(
    db_session: Session,
) -> None:
    """(b) Johnny-trt.45 acceptance: one bot session PER enabled assignment,
    each with its own agent snapshot (incl. the per-assignment context),
    ordered by position; disabled assignments are skipped and the default
    agent does NOT additionally launch."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    _seed_agent(db_session, name="Default", is_default=True)
    disabled = _seed_agent(db_session, name="Disabled", mode=BotMode.AUTONOMOUS)
    aria = _seed_agent(
        db_session,
        name="Aria",
        mode=BotMode.SUGGEST_ONLY,
        character_prompt="You are Aria.",
    )
    finn = _seed_agent(
        db_session,
        name="Finn",
        mode=BotMode.AUTONOMOUS,
        character_prompt="You are Finn.",
    )
    _assign_agent(
        db_session, meeting=cfg, agent=disabled, enabled=False, position=0
    )
    _assign_agent(
        db_session,
        meeting=cfg,
        agent=aria,
        context="Aria runs the demo today.",
        position=1,
    )
    _assign_agent(
        db_session,
        meeting=cfg,
        agent=finn,
        context="Finn takes notes.",
        position=2,
    )

    launcher = NoopContainerLauncher()
    rows = await start_sessions_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )

    assert len(rows) == 2
    assert len(launcher.started) == 2
    by_agent = {row.agent_id: row for row in rows}
    assert set(by_agent) == {aria.id, finn.id}
    aria_row, finn_row = by_agent[aria.id], by_agent[finn.id]
    db_session.refresh(aria_row)
    db_session.refresh(finn_row)
    assert aria_row.bot_name == "Aria"
    assert aria_row.agent_snapshot is not None
    assert aria_row.agent_snapshot["mode"] == "suggest_only"
    assert aria_row.agent_snapshot["assignment_context"] == "Aria runs the demo today."
    assert finn_row.bot_name == "Finn"
    assert finn_row.agent_snapshot is not None
    assert finn_row.agent_snapshot["assignment_context"] == "Finn takes notes."
    # Peer roster (Johnny-trt.47): each snapshot names the OTHER enabled
    # assignments' agents — never itself, never the disabled assignment.
    assert aria_row.agent_snapshot["peer_names"] == ["Finn"]
    assert finn_row.agent_snapshot["peer_names"] == ["Aria"]
    # Each launch context carries its own session id + frozen snapshot;
    # position order decides launch order.
    assert [c.agent_id for c in launcher.started] == [aria.id, finn.id]
    for started_ctx in launcher.started:
        matching_row = by_agent[started_ctx.agent_id]
        assert started_ctx.bot_session_id == matching_row.id
        assert started_ctx.agent_snapshot == matching_row.agent_snapshot
        assert started_ctx.container_name == container_name_for_session(
            matching_row.id
        )


@pytest.mark.asyncio
async def test_per_assignment_identity_account_with_meeting_fallback(
    db_session: Session,
) -> None:
    """Johnny-trt.45 multi-agent identity: an assignment pinning its own
    Google account joins with it; an assignment pinning none falls back to
    the meeting-level identity account."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    second_account = GoogleAccount(
        email="second-identity@example.com", refresh_token_encrypted="x"
    )
    db_session.add(second_account)
    db_session.flush()
    aria = _seed_agent(db_session, name="Aria")
    finn = _seed_agent(db_session, name="Finn")
    _assign_agent(
        db_session,
        meeting=cfg,
        agent=aria,
        position=0,
        identity_account_id=second_account.id,
    )
    _assign_agent(db_session, meeting=cfg, agent=finn, position=1)

    launcher = NoopContainerLauncher()
    await start_sessions_for_meeting(db_session, meeting=cfg, launcher=launcher)

    identities = {c.agent_id: c.identity_account_id for c in launcher.started}
    assert identities[aria.id] == second_account.id
    assert identities[finn.id] == cfg.identity_account_id


# --- Johnny-wks.7: per-AGENT meeting-bot identity ---------------------------


def _seed_extra_account(sess: Session, *, email: str) -> GoogleAccount:
    account = GoogleAccount(email=email, refresh_token_encrypted="x")
    sess.add(account)
    sess.flush()
    return account


@pytest.mark.asyncio
async def test_agent_meeting_bot_account_overrides_meeting_default(
    db_session: Session,
) -> None:
    """Johnny-wks.7: an agent with its own meeting-bot account joins as that
    account, overriding the meeting-level identity (no per-assignment pin)."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    bot_account = _seed_extra_account(db_session, email="agent-bot@example.com")
    agent = _seed_agent(
        db_session, name="Aria", meeting_bot_account_id=bot_account.id
    )
    _assign_agent(db_session, meeting=cfg, agent=agent, position=0)

    launcher = NoopContainerLauncher()
    await start_sessions_for_meeting(db_session, meeting=cfg, launcher=launcher)

    assert launcher.started[0].identity_account_id == bot_account.id
    # The meeting-level account was NOT used — the agent carries its own.
    assert bot_account.id != cfg.identity_account_id


@pytest.mark.asyncio
async def test_null_agent_account_preserves_meeting_resolution(
    db_session: Session,
) -> None:
    """Behavior-preserving: a NULL agent-level account resolves exactly as
    before wks.7 — the meeting-level identity account."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    agent = _seed_agent(db_session, name="Aria", meeting_bot_account_id=None)
    _assign_agent(db_session, meeting=cfg, agent=agent, position=0)

    launcher = NoopContainerLauncher()
    await start_sessions_for_meeting(db_session, meeting=cfg, launcher=launcher)

    assert launcher.started[0].identity_account_id == cfg.identity_account_id


@pytest.mark.asyncio
async def test_per_assignment_pin_still_beats_agent_level_account(
    db_session: Session,
) -> None:
    """The per-assignment pin (trt.45) is the most specific — the agent-level
    account is only the default it sources from, so an explicit pin wins."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    agent_account = _seed_extra_account(db_session, email="agent-bot@example.com")
    pinned_account = _seed_extra_account(db_session, email="pinned@example.com")
    agent = _seed_agent(
        db_session, name="Aria", meeting_bot_account_id=agent_account.id
    )
    _assign_agent(
        db_session,
        meeting=cfg,
        agent=agent,
        position=0,
        identity_account_id=pinned_account.id,
    )

    launcher = NoopContainerLauncher()
    await start_sessions_for_meeting(db_session, meeting=cfg, launcher=launcher)

    assert launcher.started[0].identity_account_id == pinned_account.id


@pytest.mark.asyncio
async def test_two_agents_distinct_meeting_bot_accounts_are_distinct(
    db_session: Session,
) -> None:
    """Acceptance: two agents in ONE meeting, each with its own meeting-bot
    account, join as two distinct identities — no single-account collision."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria_account = _seed_extra_account(db_session, email="aria-bot@example.com")
    finn_account = _seed_extra_account(db_session, email="finn-bot@example.com")
    aria = _seed_agent(
        db_session, name="Aria", meeting_bot_account_id=aria_account.id
    )
    finn = _seed_agent(
        db_session, name="Finn", meeting_bot_account_id=finn_account.id
    )
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _assign_agent(db_session, meeting=cfg, agent=finn, position=1)

    launcher = NoopContainerLauncher()
    await start_sessions_for_meeting(db_session, meeting=cfg, launcher=launcher)

    identities = {c.agent_id: c.identity_account_id for c in launcher.started}
    assert identities[aria.id] == aria_account.id
    assert identities[finn.id] == finn_account.id
    assert identities[aria.id] != identities[finn.id]


@pytest.mark.asyncio
async def test_two_agents_may_share_one_meeting_bot_account(
    db_session: Session,
) -> None:
    """Acceptance: opt-in shared identity — two agents MAY point at the SAME
    meeting-bot account (the operator's choice, never workspace-driven)."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    shared = _seed_extra_account(db_session, email="shared-bot@example.com")
    aria = _seed_agent(db_session, name="Aria", meeting_bot_account_id=shared.id)
    finn = _seed_agent(db_session, name="Finn", meeting_bot_account_id=shared.id)
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _assign_agent(db_session, meeting=cfg, agent=finn, position=1)

    launcher = NoopContainerLauncher()
    await start_sessions_for_meeting(db_session, meeting=cfg, launcher=launcher)

    identities = {c.agent_id: c.identity_account_id for c in launcher.started}
    assert identities[aria.id] == shared.id
    assert identities[finn.id] == shared.id


@pytest.mark.asyncio
async def test_assignment_launch_failure_does_not_stop_co_agents(
    db_session: Session,
) -> None:
    """A launcher failure on one assignment records that row failed and
    keeps launching the rest; the call returns the successful rows."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    finn = _seed_agent(db_session, name="Finn")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _assign_agent(db_session, meeting=cfg, agent=finn, position=1)

    class _FirstBoomLauncher(NoopContainerLauncher):
        async def start(self, ctx: LaunchContext) -> LaunchResult:
            if not self.started:
                self.started.append(ctx)
                raise LauncherError("docker exploded")
            return await super().start(ctx)

    launcher = _FirstBoomLauncher()
    rows = await start_sessions_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )

    assert len(rows) == 1
    assert rows[0].agent_id == finn.id
    failed = db_session.scalars(
        select(BotSession).where(BotSession.status == BotSessionStatus.FAILED)
    ).all()
    assert len(failed) == 1
    assert failed[0].agent_id == aria.id
    assert "launcher.start failed" in (failed[0].error_reason or "")


@pytest.mark.asyncio
async def test_all_assignments_failing_raises(db_session: Session) -> None:
    """When NO session could launch at all, the error propagates so the
    scheduler pass still counts the meeting as errored."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)

    class _BoomLauncher(NoopContainerLauncher):
        async def start(self, ctx: LaunchContext) -> LaunchResult:
            raise LauncherError("docker exploded")

    with pytest.raises(LauncherError):
        await start_sessions_for_meeting(
            db_session, meeting=cfg, launcher=_BoomLauncher()
        )


@pytest.mark.asyncio
async def test_start_session_without_any_agent_degrades_to_contract_defaults(
    db_session: Session,
) -> None:
    """(c) No agents in the DB at all → contract defaults: empty snapshot, no
    bot_name/agent stamp. The launch still happens."""
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
    assert row.agent_id is None
    assert row.agent_snapshot is None
    assert row.bot_name is None
    ctx = launcher.started[0]
    assert ctx.agent_id is None
    assert ctx.agent_snapshot == {}
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
async def test_start_session_passes_resolved_attachments_text(
    db_session: Session,
) -> None:
    """Johnny-4da: ``CalendarEvent.attachments_text`` → LaunchContext."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    cfg.calendar_event.description = (
        "See: https://docs.google.com/document/d/docABC/edit"
    )
    cfg.calendar_event.attachments_text = (
        "--- Quarterly Plan ---\nObjective: ship Johnny-4da."
    )
    db_session.flush()

    launcher = NoopContainerLauncher()
    await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    ctx = launcher.started[0]
    assert ctx.calendar_attachments_text == (
        "--- Quarterly Plan ---\nObjective: ship Johnny-4da."
    )


@pytest.mark.asyncio
async def test_start_session_attachments_default_empty(
    db_session: Session,
) -> None:
    """No resolved attachments → empty string env var, not None."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    db_session.flush()
    launcher = NoopContainerLauncher()
    await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    assert launcher.started[0].calendar_attachments_text == ""


@pytest.mark.asyncio
async def test_start_session_injects_prior_session_summary(
    db_session: Session,
) -> None:
    """Johnny-dsy: prior occurrence's summary rides into LaunchContext."""
    # First occurrence: a prior bot_session with a written summary,
    # already ended.
    prior_cfg = _seed_full_meeting(
        db_session,
        start_offset=-timedelta(days=7, hours=1),
        end_offset=-timedelta(days=7),
        external_id="evt-week-old",
    )
    prior_cfg.calendar_event.recurring_event_id = "series-standup"
    db_session.flush()
    prior_row = BotSession(
        meeting_config_id=prior_cfg.id,
        status=BotSessionStatus.ENDED,
        ended_at=datetime.now(UTC) - timedelta(days=7),
        session_summary="Last week we agreed to ship Johnny-dsy by Friday.",
    )
    db_session.add(prior_row)
    db_session.flush()

    # Second occurrence: about to start, same series id.
    next_cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
        external_id="evt-this-week",
    )
    next_cfg.calendar_event.recurring_event_id = "series-standup"
    db_session.flush()

    launcher = NoopContainerLauncher()
    await start_session_for_meeting(
        db_session, meeting=next_cfg, launcher=launcher
    )
    ctx = launcher.started[0]
    assert ctx.prior_session_context == (
        "Last week we agreed to ship Johnny-dsy by Friday."
    )


@pytest.mark.asyncio
async def test_start_session_no_recurring_id_empty_prior_context(
    db_session: Session,
) -> None:
    """One-off event (no recurringEventId) leaves prior_session_context empty."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    db_session.flush()
    launcher = NoopContainerLauncher()
    await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    assert launcher.started[0].prior_session_context == ""


@pytest.mark.asyncio
async def test_start_session_recurring_but_no_prior_session(
    db_session: Session,
) -> None:
    """First occurrence of a new series → no prior summary → empty context."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    cfg.calendar_event.recurring_event_id = "series-first-run"
    db_session.flush()
    launcher = NoopContainerLauncher()
    await start_session_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )
    assert launcher.started[0].prior_session_context == ""


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
async def test_stop_session_browser_live_signals_runner_not_launcher(
    db_session: Session,
) -> None:
    """Stopping a LIVE browser session signals the in-process runner and
    skips the docker launcher entirely (Johnny-8zv). The row is left for
    the runner's own cleanup to mark ended + publish."""
    row = BotSession(
        source=BotSessionSource.BROWSER, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.flush()
    launcher = NoopContainerLauncher()
    with patch(
        "app.api.browser_sessions.request_browser_session_stop",
        return_value=True,
    ) as req:
        result = await stop_session_by_id(
            db_session, bot_session_id=row.id, launcher=launcher
        )
    req.assert_called_once_with(row.id)
    assert launcher.stopped == []
    # Runner cleanup ends the row asynchronously — not here.
    assert result.status == BotSessionStatus.JOINED


@pytest.mark.asyncio
async def test_stop_session_browser_stale_ends_and_publishes(
    db_session: Session,
) -> None:
    """A stale browser row (no live runner) is ended directly + a status
    event is published, without ever touching the docker launcher."""
    row = BotSession(
        source=BotSessionSource.BROWSER, status=BotSessionStatus.JOINED
    )
    db_session.add(row)
    db_session.flush()
    launcher = NoopContainerLauncher()
    with (
        patch(
            "app.api.browser_sessions.request_browser_session_stop",
            return_value=False,
        ),
        patch(
            "app.api.browser_sessions.publish_session_status_oneoff",
            new=AsyncMock(),
        ) as pub,
    ):
        result = await stop_session_by_id(
            db_session, bot_session_id=row.id, launcher=launcher
        )
    assert result.status == BotSessionStatus.ENDED
    assert launcher.stopped == []
    pub.assert_awaited_once()


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


# --- Johnny-trt.41: active provider payload still rides the launch ----------


@pytest.mark.asyncio
async def test_start_session_serialises_active_provider_payload(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The globally-active provider rows are materialised into
    ``ctx.provider_config`` so the DB-free meet-worker can instantiate its
    stack. (Per-agent provider-pin resolution is Johnny-trt.42.)"""
    from cryptography.fernet import Fernet

    from app.db.models import ProviderCredential
    from app.providers.base import ProviderKind
    from app.security.crypto import CredentialCrypto, encrypt_json

    crypto = CredentialCrypto(Fernet.generate_key())
    monkeypatch.setattr("app.security.crypto.get_crypto", lambda: crypto)

    engine = db_session.get_bind()
    ProviderCredential.__table__.create(bind=engine, checkfirst=True)

    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    ga = ProviderCredential(
        kind=ProviderKind.LLM,
        provider_name="ga",
        display_name="GA",
        credentials_encrypted=encrypt_json(crypto, {"api_key": "k"}),
        config={},
        is_active=True,
    )
    dormant = ProviderCredential(
        kind=ProviderKind.LLM,
        provider_name="dormant",
        display_name="Dormant",
        credentials_encrypted=encrypt_json(crypto, {"api_key": "k"}),
        config={},
        is_active=False,
    )
    db_session.add_all([ga, dormant])
    db_session.flush()

    launcher = NoopContainerLauncher()
    await start_session_for_meeting(db_session, meeting=cfg, launcher=launcher)

    ctx = launcher.started[0]
    assert ctx.provider_config["llm"]["provider_name"] == "ga"  # active row only
    assert ctx.provider_config["llm"]["credentials"] == {"api_key": "k"}


# --- Per-assignment gate + cap (Johnny-trt.46) ------------------------------


def _active_session_for(
    sess: Session,
    cfg: MeetingConfig,
    *,
    agent_id: int | None = None,
    status: BotSessionStatus = BotSessionStatus.JOINED,
) -> BotSession:
    row = BotSession(meeting_config_id=cfg.id, status=status, agent_id=agent_id)
    sess.add(row)
    sess.flush()
    return row


def test_select_due_includes_meeting_with_one_dead_co_agent(
    db_session: Session,
) -> None:
    """The per-assignment gate: agent A joined, agent B's session crashed —
    the meeting is due again so B (alone) can redispatch."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    finn = _seed_agent(db_session, name="Finn")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _assign_agent(db_session, meeting=cfg, agent=finn, position=1)
    _active_session_for(db_session, cfg, agent_id=aria.id)
    _active_session_for(
        db_session, cfg, agent_id=finn.id, status=BotSessionStatus.FAILED
    )

    assert [m.id for m in select_due_meetings(db_session)] == [cfg.id]


def test_select_due_skips_fully_covered_multi_agent_meeting(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    finn = _seed_agent(db_session, name="Finn")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _assign_agent(db_session, meeting=cfg, agent=finn, position=1)
    _active_session_for(db_session, cfg, agent_id=aria.id)
    _active_session_for(db_session, cfg, agent_id=finn.id)

    assert select_due_meetings(db_session) == []


def test_pending_assignments_shapes(db_session: Session) -> None:
    """[] = covered; None = default-agent launch owed; list = the uncovered."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    # No assignments, no sessions → the default-agent launch is owed.
    assert pending_assignments(db_session, cfg) is None
    # An agent-less active session (contract degrade) covers the default case.
    row = _active_session_for(db_session, cfg, agent_id=None)
    assert pending_assignments(db_session, cfg) == []
    row.status = BotSessionStatus.ENDED
    db_session.flush()
    # Assignments: only the uncovered ones come back, in position order.
    aria = _seed_agent(db_session, name="Aria")
    finn = _seed_agent(db_session, name="Finn")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    finn_assignment = _assign_agent(db_session, meeting=cfg, agent=finn, position=1)
    _active_session_for(db_session, cfg, agent_id=aria.id)
    pending = pending_assignments(db_session, cfg)
    assert pending is not None
    assert [a.id for a in pending] == [finn_assignment.id]


def test_pending_assignments_relogin_covers_for_scheduler_not_manual_join(
    db_session: Session,
) -> None:
    """waiting_for_relogin counts as covered for the scheduler default, but
    the manual Join-now statuses leave it joinable (the recovery path)."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _active_session_for(
        db_session, cfg, agent_id=aria.id, status=BotSessionStatus.WAITING_FOR_RELOGIN
    )

    assert pending_assignments(db_session, cfg) == []
    manual = (
        BotSessionStatus.SCHEDULED,
        BotSessionStatus.JOINING,
        BotSessionStatus.JOINED,
    )
    pending = pending_assignments(db_session, cfg, statuses=manual)
    assert pending is not None and len(pending) == 1


@pytest.mark.asyncio
async def test_start_sessions_launches_only_uncovered_assignments(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    finn = _seed_agent(db_session, name="Finn")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _assign_agent(db_session, meeting=cfg, agent=finn, position=1)
    _active_session_for(db_session, cfg, agent_id=aria.id)

    launcher = NoopContainerLauncher()
    rows = await start_sessions_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )

    assert [row.agent_id for row in rows] == [finn.id]
    assert [c.agent_id for c in launcher.started] == [finn.id]


@pytest.mark.asyncio
async def test_start_sessions_fully_covered_is_a_noop(db_session: Session) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _active_session_for(db_session, cfg, agent_id=aria.id)

    launcher = NoopContainerLauncher()
    rows = await start_sessions_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )

    assert rows == []
    assert launcher.started == []


@pytest.mark.asyncio
async def test_start_session_for_meeting_raises_when_fully_covered(
    db_session: Session,
) -> None:
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)
    _active_session_for(db_session, cfg, agent_id=aria.id)

    with pytest.raises(ValueError, match="already has an active session"):
        await start_session_for_meeting(
            db_session, meeting=cfg, launcher=NoopContainerLauncher()
        )


@pytest.mark.asyncio
async def test_launch_caps_enabled_assignments_at_max(db_session: Session) -> None:
    """Defensive cap re-application at launch: rows written around the API
    can't fan out past MAX_AGENTS_PER_MEETING."""
    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    agents = [
        _seed_agent(db_session, name=f"Agent {i}")
        for i in range(MAX_AGENTS_PER_MEETING + 2)
    ]
    for position, agent in enumerate(agents):
        _assign_agent(db_session, meeting=cfg, agent=agent, position=position)

    launcher = NoopContainerLauncher()
    rows = await start_sessions_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )

    assert len(rows) == MAX_AGENTS_PER_MEETING
    expected = [agent.id for agent in agents[:MAX_AGENTS_PER_MEETING]]
    assert [row.agent_id for row in rows] == expected


@pytest.mark.asyncio
async def test_agent_id_stamped_even_when_snapshot_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-assignment coverage key survives a snapshot glitch: the row
    still records WHICH agent it is for, so the next pass cannot
    double-launch the assignment (Johnny-trt.46)."""
    import app.services.agents as agents_service

    cfg = _seed_full_meeting(
        db_session,
        start_offset=timedelta(seconds=30),
        end_offset=timedelta(minutes=30),
    )
    aria = _seed_agent(db_session, name="Aria")
    _assign_agent(db_session, meeting=cfg, agent=aria, position=0)

    def _boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("snapshot glitch")

    monkeypatch.setattr(agents_service, "build_agent_snapshot", _boom)

    launcher = NoopContainerLauncher()
    rows = await start_sessions_for_meeting(
        db_session, meeting=cfg, launcher=launcher
    )

    assert len(rows) == 1
    assert rows[0].agent_id == aria.id  # stamped despite the failed snapshot
    assert rows[0].agent_snapshot is None  # contract-default degrade preserved
    # And the meeting now reads as covered — no redispatch storm.
    assert pending_assignments(db_session, cfg) == []


# --- US-401 (Johnny-d6w.19): human meet roster extraction -------------------


def _event_with_attendees(attendees: object) -> CalendarEvent:
    now = datetime.now(UTC)
    return CalendarEvent(
        account_id=1,
        external_id="evt-roster",
        start_time=now,
        end_time=now + timedelta(minutes=30),
        meet_link="https://meet.example/x",
        attendees=attendees,
    )


def test_human_attendee_names_filters_self_and_resolves_label() -> None:
    """Drops the bot's own account + blanks; display_name then email fallback."""
    from app.services.session_scheduler import _human_attendee_names

    event = _event_with_attendees(
        [
            {"email": "alice@example.com", "display_name": "Alice Smith", "self": False},
            {"email": "bob@example.com", "display_name": None, "self": False},
            {"email": "bot@example.com", "display_name": "Johnny Bot", "self": True},
            {"email": "", "display_name": "", "self": False},
            "not-a-dict",
        ]
    )
    cfg = MeetingConfig(calendar_event_id=1, identity_account_id=1, enabled=True)
    cfg.calendar_event = event
    assert _human_attendee_names(cfg) == ["Alice Smith", "bob@example.com"]


def test_human_attendee_names_empty_without_attendees() -> None:
    """Ad-hoc rooms (no roster) → empty list → live attribution degrades."""
    from app.services.session_scheduler import _human_attendee_names

    cfg = MeetingConfig(calendar_event_id=1, identity_account_id=1, enabled=True)
    cfg.calendar_event = _event_with_attendees(None)
    assert _human_attendee_names(cfg) == []
