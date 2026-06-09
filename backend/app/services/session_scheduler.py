"""Bot session scheduler (US-029).

Picks meetings that are about to start, spawns a meet-worker for each,
and tears them down once the event ends. The "spawning" is delegated to
a pluggable :class:`ContainerLauncher` — the default is a no-op that
just records the call, so the scheduler itself can be exercised end-to-end
without Docker. US-030 will land a Docker-backed launcher.

Shape:

* :func:`select_due_meetings` — find meeting_configs whose event starts
  within the next ``join_window_seconds`` and has no active bot_session.
* :func:`start_session_for_meeting` — create a ``scheduled`` bot_session
  row, call ``launcher.start``, then transition to ``joining``. Errors
  during launch are translated into ``failed``.
* :func:`stop_session_by_id` — call ``launcher.stop`` and transition the
  row to ``ended`` (or ``failed`` on launcher error). Idempotent for
  rows already in a terminal state.
* :func:`run_scheduler_pass` — opens a fresh session, runs both due-start
  and due-stop sweeps in one go.

The scheduler is intentionally synchronous from the ORM's point of view
— ``session.flush`` is enough; the outer ``session_scope`` commits.
The launcher's ``start`` / ``stop`` are async to leave room for future
HTTP / Docker SDK calls without forcing every test to set up an event
loop just for the no-op default.

Manual UI actions (``POST /sessions/start``, ``POST /sessions/{id}/stop``)
invoke the same start/stop helpers — see :mod:`app.api.sessions`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    MeetingConfig,
)
from app.services.agent_dispatch import maybe_dispatch_session_agent
from app.services.bot_sessions import (
    BotSessionNotFoundError,
    mark_session_ended,
    mark_session_failed,
    mark_session_joining,
)

logger = logging.getLogger(__name__)


# Default look-ahead window: the AC says "starts within the next 2 minutes".
DEFAULT_JOIN_WINDOW_SECONDS = 120
# How long after end_time before we ask the launcher to stop the worker.
DEFAULT_STOP_GRACE_SECONDS = 60
# How long a session may sit in ``waiting_for_relogin`` before we give up and
# settle it to ``failed`` even if the meeting is still live (the operator
# never re-logged in). Matches the bot-signin link TTL (Johnny-ebf).
DEFAULT_RELOGIN_TTL_SECONDS = 600
# How often the worker's periodic loop ticks the scheduler.
DEFAULT_SCHEDULER_INTERVAL_SECONDS = 60
SCHEDULER_INTERVAL_ENV = "JOHNNY_SCHEDULER_INTERVAL_SECONDS"

# Statuses the stop sweep acts on (a live/scheduled worker to wind down).
_STOP_SWEEP_STATUSES = (
    BotSessionStatus.SCHEDULED,
    BotSessionStatus.JOINING,
    BotSessionStatus.JOINED,
)
# Statuses that count as "this meeting already has a worker (or had one
# scheduled), or is waiting on the operator — don't queue another, and keep
# it in the active-sessions panel". ``waiting_for_relogin`` is active-but-not-
# stoppable: the stop sweep would mark it ``ended``, but the relogin settle
# sweep marks it ``failed`` with the signed-out reason instead (Johnny-ebf).
_ACTIVE_STATUSES = (
    *_STOP_SWEEP_STATUSES,
    BotSessionStatus.WAITING_FOR_RELOGIN,
)
_TERMINAL_STATUSES = (BotSessionStatus.ENDED, BotSessionStatus.FAILED)


def get_scheduler_interval_seconds() -> int:
    """Read ``JOHNNY_SCHEDULER_INTERVAL_SECONDS`` from the environment.

    Defaults to 60 seconds when unset or malformed; clamps to at least
    1 second so a misconfiguration can't spin the worker loop.
    """
    raw = os.environ.get(SCHEDULER_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_SCHEDULER_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r; using default %d",
            SCHEDULER_INTERVAL_ENV,
            raw,
            DEFAULT_SCHEDULER_INTERVAL_SECONDS,
        )
        return DEFAULT_SCHEDULER_INTERVAL_SECONDS
    return max(1, value)


# --- Launcher protocol -----------------------------------------------------


@dataclass(frozen=True)
class LaunchContext:
    """Inputs the launcher needs to spawn a meet-worker.

    ``container_name`` is suggested by the scheduler so the launcher's
    naming stays predictable across implementations; the launcher may
    ignore it and pick its own name as long as it returns the actual
    name in :class:`LaunchResult`.

    ``instructions`` and ``provider_config`` are passed as environment
    variables to the meet-worker by the Docker launcher (US-030) so the
    pipeline knows what to say and which providers are active. Defaults
    keep the scheduler's no-op path unchanged.
    """

    bot_session_id: int
    meeting_config_id: int
    calendar_event_id: int
    identity_account_id: int
    meet_link: str
    container_name: str
    mode: str = ""
    instructions: str = ""
    personality_prompt: str = ""
    """Personality IDENTITY-layer system prompt (Johnny-oly.8).

    The resolved personality's ``description`` wrapped as
    ``[personality: <name>]\\n<description>``. Forwarded to the meet-worker
    via the ``JOHNNY_PERSONALITY_PROMPT`` env var so a scheduled bot adopts
    the same persona a playground session would. Empty when no personality
    applied.
    """
    context: str = ""
    calendar_context: str = ""
    """Calendar event description — pre-meeting context from the event itself.

    Passed alongside ``context`` (the user-typed brief) so the bot sees
    both. Kept distinct so an audit can tell them apart and so the user
    can edit one without disturbing the other.
    """
    calendar_attachments_text: str = ""
    """Resolved text body of Google Docs / Sheets / Drive files linked
    from the calendar event description (Johnny-4da).

    Populated by the polling worker's resolver pass and cached on
    :attr:`~app.db.models.CalendarEvent.attachments_text`. The scheduler
    reads it once per launch and forwards via the meet-worker env var
    ``JOHNNY_CALENDAR_ATTACHMENTS``. Empty string when the event has no
    Drive URLs or the polling cycle hasn't yet resolved them — the bot
    still joins, it just doesn't see document bodies for that meeting.
    """
    prior_session_context: str = ""
    """Prior-occurrence summary for recurring meetings (Johnny-dsy).

    Populated by the scheduler via
    :func:`app.services.history.find_prior_session_summary` when the
    upcoming :class:`~app.db.models.CalendarEvent` shares a
    ``recurring_event_id`` with a previously-ended bot_session. The
    docker launcher forwards via ``JOHNNY_PRIOR_SESSION_CONTEXT``. Empty
    string when there's no prior occurrence (one-off events, first run
    of a new series) — the pipeline simply skips the "Last session
    summary" prompt line.
    """
    provider_config: dict[str, Any] = field(default_factory=dict)
    pipeline_mode: str = "split"
    """Pipeline shape — ``split`` (STT→LLM→TTS) or ``unified`` (S2S) (Johnny-ckz.17).

    Defaults to ``split`` so the existing scheduler path is unchanged.
    The Docker launcher forwards this to the meet-worker via the
    ``JOHNNY_PIPELINE_MODE`` env var.
    """


@dataclass(frozen=True)
class LaunchResult:
    """What the launcher actually did.

    ``container_name`` is persisted back to ``bot_sessions.container_name``
    so an operator can correlate Docker objects with rows.
    """

    container_name: str


class ContainerLauncher:
    """Interface for "spawn / kill the meet-worker for this session".

    The default implementation (:class:`NoopContainerLauncher`) does
    nothing — useful for tests and for landing the scheduler before
    US-030 wires the Docker SDK.
    """

    async def start(self, ctx: LaunchContext) -> LaunchResult:
        raise NotImplementedError

    async def stop(self, *, bot_session_id: int, container_name: str | None) -> None:
        raise NotImplementedError


class NoopContainerLauncher(ContainerLauncher):
    """Recording no-op launcher. Useful for tests / scheduler-only deploys."""

    def __init__(self) -> None:
        self.started: list[LaunchContext] = []
        self.stopped: list[tuple[int, str | None]] = []

    async def start(self, ctx: LaunchContext) -> LaunchResult:
        self.started.append(ctx)
        return LaunchResult(container_name=ctx.container_name)

    async def stop(self, *, bot_session_id: int, container_name: str | None) -> None:
        self.stopped.append((bot_session_id, container_name))


class LauncherError(RuntimeError):
    """Raised by launcher implementations on irrecoverable failure."""


def container_name_for_session(bot_session_id: int) -> str:
    """Stable container name per session — matches US-030's convention."""
    return f"meet-worker-session-{bot_session_id}"


# --- Active-session detection ---------------------------------------------


def _now() -> datetime:
    """Indirection so tests can pin the clock without monkey-patching datetime."""
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Coerce a DB datetime to aware-UTC for Python-side comparison.

    ``DateTime(timezone=True)`` round-trips as naive on SQLite (the test
    engine) but aware on Postgres; assume stored times are UTC so a naive
    value can be compared against :func:`_now`-derived thresholds without a
    "can't compare offset-naive and offset-aware" error.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def select_due_meetings(
    session: Session,
    *,
    now: datetime | None = None,
    join_window_seconds: int = DEFAULT_JOIN_WINDOW_SECONDS,
) -> list[MeetingConfig]:
    """Meeting configs whose event starts soon AND have no active session.

    "Soon" = ``start_time <= now + join_window_seconds`` and
    ``end_time > now`` (we don't try to join a meeting that already ended).
    The meeting must be ``enabled``, its event must have a Meet link,
    and there must be no bot_session row in scheduled/joining/joined
    status for it.
    """
    moment = now or _now()
    horizon = moment + timedelta(seconds=join_window_seconds)

    # Distinct meeting configs whose event is due, that don't already
    # have an active bot_session. We use a left-join + WHERE NULL pattern
    # rather than EXISTS so the test fixture sees an executable plan on
    # SQLite without coercing the dialect.
    active_subq = (
        select(BotSession.meeting_config_id)
        .where(BotSession.status.in_(_ACTIVE_STATUSES))
        .subquery()
    )
    stmt = (
        select(MeetingConfig)
        .join(CalendarEvent, CalendarEvent.id == MeetingConfig.calendar_event_id)
        .where(MeetingConfig.enabled.is_(True))
        .where(CalendarEvent.meet_link.is_not(None))
        .where(CalendarEvent.start_time <= horizon)
        .where(CalendarEvent.end_time > moment)
        .where(MeetingConfig.id.not_in(select(active_subq)))
        .order_by(CalendarEvent.start_time, MeetingConfig.id)
    )
    return list(session.scalars(stmt).all())


def select_due_stops(
    session: Session,
    *,
    now: datetime | None = None,
    stop_grace_seconds: int = DEFAULT_STOP_GRACE_SECONDS,
) -> list[BotSession]:
    """Bot sessions whose event ended ``stop_grace_seconds`` ago.

    Returns rows in ``scheduled`` / ``joining`` / ``joined`` whose
    underlying event's ``end_time`` is in the past by at least the
    grace window. Used by the periodic stop sweep.
    """
    moment = now or _now()
    threshold = moment - timedelta(seconds=stop_grace_seconds)
    stmt = (
        select(BotSession)
        .join(
            MeetingConfig, MeetingConfig.id == BotSession.meeting_config_id
        )
        .join(
            CalendarEvent, CalendarEvent.id == MeetingConfig.calendar_event_id
        )
        .where(BotSession.status.in_(_STOP_SWEEP_STATUSES))
        .where(CalendarEvent.end_time <= threshold)
        .order_by(BotSession.id)
    )
    return list(session.scalars(stmt).all())


def select_relogin_to_settle(
    session: Session,
    *,
    now: datetime | None = None,
    stop_grace_seconds: int = DEFAULT_STOP_GRACE_SECONDS,
    ttl_seconds: int = DEFAULT_RELOGIN_TTL_SECONDS,
) -> list[BotSession]:
    """``waiting_for_relogin`` rows that should now settle to ``failed``.

    A signed-out session waits for the operator to re-login, but it must not
    wait forever (Johnny-ebf). It settles when EITHER the meeting has ended
    (``end_time`` past by the grace window — the same trigger the stop sweep
    uses for live sessions) OR it has been waiting longer than ``ttl_seconds``
    (the operator never acted while the meeting is still live). Returns rows
    oldest-first; the caller flips each to ``failed`` with a clear reason.
    """
    moment = now or _now()
    end_threshold = moment - timedelta(seconds=stop_grace_seconds)
    ttl_threshold = moment - timedelta(seconds=ttl_seconds)
    stmt = (
        select(BotSession)
        .join(
            MeetingConfig, MeetingConfig.id == BotSession.meeting_config_id
        )
        .join(
            CalendarEvent, CalendarEvent.id == MeetingConfig.calendar_event_id
        )
        .where(BotSession.status == BotSessionStatus.WAITING_FOR_RELOGIN)
        .where(
            or_(
                CalendarEvent.end_time <= end_threshold,
                BotSession.updated_at <= ttl_threshold,
            )
        )
        .order_by(BotSession.id)
    )
    return list(session.scalars(stmt).all())


def list_active_sessions(session: Session) -> list[BotSession]:
    """Every non-terminal bot_session, ordered oldest-first.

    Powers the "scheduler state" UI panel (US-029 AC #5).
    """
    stmt = (
        select(BotSession)
        .where(BotSession.status.in_(_ACTIVE_STATUSES))
        .order_by(BotSession.id)
    )
    return list(session.scalars(stmt).all())


# --- Start / stop --------------------------------------------------------


def _combine_text(base: str | None, override: str | None) -> str:
    """Concatenate template base text with the meeting-level override.

    Used to build the effective instructions / context the meet-worker
    receives via environment variables. Empty strings are skipped so the
    result has no leading or trailing separator.
    """
    parts = [part for part in (base, override) if part]
    return "\n\n".join(parts)


def _validate_meeting_for_launch(meeting: MeetingConfig) -> str:
    """Return the meet_link or raise ``ValueError`` if the meeting cannot launch."""
    if not meeting.enabled:
        raise ValueError(f"meeting_config id={meeting.id} is disabled")
    event = meeting.calendar_event
    if event is None:
        raise ValueError(
            f"meeting_config id={meeting.id} has no linked calendar_event"
        )
    if not event.meet_link:
        raise ValueError(
            f"calendar_event id={event.id} has no meet_link"
        )
    return event.meet_link


async def start_session_for_meeting(
    session: Session,
    *,
    meeting: MeetingConfig,
    launcher: ContainerLauncher,
) -> BotSession:
    """Create the bot_session row and ask the launcher to start the worker.

    Flow:

    1. ``validate`` — checks enabled + has meet link.
    2. Insert ``bot_sessions`` row in ``scheduled`` so its id exists
       before we tell the launcher (the id is part of the container name).
    3. ``await launcher.start(...)``. Any exception is recorded against
       the row via :func:`mark_session_failed` and re-raised.
    4. Persist the returned ``container_name`` and transition to
       ``joining``.

    Returns the persisted row. The session is left uncommitted; the
    caller's outer transaction commits.
    """
    meet_link = _validate_meeting_for_launch(meeting)

    row = BotSession(
        meeting_config_id=meeting.id,
        # Tag with the calendar owner so History can filter by account
        # (Johnny-8th). MeetingConfig.calendar_event is non-null by schema.
        account_id=meeting.calendar_event.account_id,
        status=BotSessionStatus.SCHEDULED,
    )
    session.add(row)
    session.flush()

    template = meeting.profile_template
    base_instructions = template.base_instructions if template is not None else ""
    base_context = template.base_context if template is not None else ""
    effective_instructions = _combine_text(base_instructions, meeting.instructions)
    effective_context = _combine_text(base_context, meeting.context)

    # Materialise the active provider rows so the meet-worker bootstrap
    # can instantiate STT / LLM / TTS without DB access. ``crypto`` is
    # the production CredentialCrypto by default; tests inject a
    # NoopCrypto via the ``crypto`` kwarg. A startup misconfiguration
    # (no FERNET_KEY) degrades gracefully to an empty payload so the
    # bot still joins — it just runs in listen-only mode for that
    # session.
    provider_payload: dict[str, Any] = {}
    pipeline_mode_value = "split"
    personality_prompt = ""
    try:
        from app.security.crypto import CryptoError, get_crypto
        from app.services.provider_payload import (
            build_provider_payload,
            resolve_pipeline_mode,
        )

        provider_payload = build_provider_payload(session, get_crypto())
        pipeline_mode_value = resolve_pipeline_mode(session).value
    except CryptoError as exc:
        logger.warning(
            "provider payload skipped — FERNET_KEY not configured (%s); "
            "meet-worker will run without providers",
            exc,
        )
    except Exception:  # noqa: BLE001 — never block a launch on payload errors
        logger.exception("provider payload build failed; sending empty payload")

    # Johnny-oly.3: layer the meeting's personality (or the global default) over
    # the global-active payload before it is serialised into
    # ``JOHNNY_PROVIDER_CONFIG`` for the DB-free meet-worker. A scheduled launch
    # has no per-start request, so selection is meeting.personality_id → the
    # ``is_default`` personality. Wrapped in its own guard AFTER the block above
    # so a personality glitch degrades to the global-active payload (not empty),
    # and mode stays the meeting's non-null ``mode`` (personalities never
    # override an existing meeting's mode at launch — PRD §4c).
    try:
        from app.security.crypto import get_crypto
        from app.services.personality_resolver import (
            apply_personality,
            select_personality,
        )

        personality = select_personality(session, requested_id=None, meeting=meeting)
        resolution = apply_personality(
            session, provider_payload, personality, crypto=get_crypto()
        )
        provider_payload = resolution.payload
        # Johnny-oly.8: the personality's description rides to the DB-free
        # meet-worker as JOHNNY_PERSONALITY_PROMPT so the scheduled bot adopts
        # the same persona a playground session would.
        personality_prompt = resolution.personality_prompt
        # Johnny-oly.6: snapshot the resolved personality so history renders this
        # session's bot name and the active-session card can show the character +
        # any provider fallback. ``playground_overrides`` doubles as the generic
        # per-session decoration bag here (the meet-worker itself never reads it).
        row.bot_name = resolution.personality_name
        if resolution.personality_id is not None:
            snapshot: dict[str, Any] = {
                "personality_id": resolution.personality_id,
                "personality_name": resolution.personality_name,
            }
            if resolution.fallbacks:
                snapshot["personality_fallbacks"] = [
                    {"kind": fb.kind, "reason": fb.reason}
                    for fb in resolution.fallbacks
                ]
            row.playground_overrides = snapshot
    except Exception:  # noqa: BLE001 — never block a launch on personality errors
        logger.exception(
            "personality resolution failed for meeting_config=%s; "
            "launching with global-active providers",
            meeting.id,
        )

    calendar_description = ""
    calendar_attachments = ""
    recurring_event_id: str | None = None
    event = meeting.calendar_event
    if event is not None:
        if event.description:
            calendar_description = event.description
        if event.attachments_text:
            calendar_attachments = event.attachments_text
        recurring_event_id = event.recurring_event_id

    # Johnny-dsy: cross-session continuity. When this calendar event is
    # an occurrence of a recurring series, pull the previous terminal
    # bot_session's summary so the bot can pick up open questions /
    # decisions from last week without re-asking. ``exclude`` guards the
    # corner case where this row already has a summary written (re-launch
    # after a crash) so we don't echo our own state back at ourselves.
    prior_session_context = ""
    try:
        from app.services.history import find_prior_session_summary

        prior = find_prior_session_summary(
            session,
            recurring_event_id=recurring_event_id,
            exclude_bot_session_id=row.id,
        )
        if prior is not None:
            prior_session_context = prior.summary
    except Exception:  # noqa: BLE001 — never block a launch on history lookup
        logger.exception(
            "prior_session_summary lookup failed for meeting_config=%s; "
            "continuing without cross-session context",
            meeting.id,
        )

    ctx = LaunchContext(
        bot_session_id=row.id,
        meeting_config_id=meeting.id,
        calendar_event_id=meeting.calendar_event_id,
        identity_account_id=meeting.identity_account_id,
        meet_link=meet_link,
        container_name=container_name_for_session(row.id),
        mode=str(meeting.mode.value if hasattr(meeting.mode, "value") else meeting.mode),
        instructions=effective_instructions,
        personality_prompt=personality_prompt,
        context=effective_context,
        calendar_context=calendar_description,
        calendar_attachments_text=calendar_attachments,
        prior_session_context=prior_session_context,
        provider_config=provider_payload,
        pipeline_mode=pipeline_mode_value,
    )
    try:
        result = await launcher.start(ctx)
    except Exception as exc:
        # Record the failure on the row before re-raising so the operator
        # has a visible audit trail of the failed attempt.
        try:
            mark_session_failed(session, row.id, f"launcher.start failed: {exc}")
        except BotSessionNotFoundError:  # pragma: no cover — flushed above
            logger.exception("bot_session %s vanished mid-flow", row.id)
        raise

    row.container_name = result.container_name
    mark_session_joining(session, row.id)
    logger.info(
        "started bot_session id=%s for meeting_config id=%s as container %s",
        row.id,
        meeting.id,
        result.container_name,
    )

    # Agent-worker lifecycle (Johnny-9eh): when JOHNNY_ORCHESTRATOR=agentsession,
    # dispatch the LiveKit agent into this session's room (one room per session).
    # No-op + cheap in the default `legacy` mode, and defensive — a dispatch
    # failure never breaks the meet-worker that is already running. The full
    # per-session engine selection (and the meet-worker→bridge switch) is Johnny-wz5.
    await maybe_dispatch_session_agent(ctx)
    return row


async def stop_session_by_id(
    session: Session,
    *,
    bot_session_id: int,
    launcher: ContainerLauncher,
) -> BotSession:
    """Ask the launcher to stop the worker and transition the row to ``ended``.

    Idempotent: rows already in ``ended`` / ``failed`` are returned
    unchanged. A launcher error is recorded against the row as
    ``failed`` with the exception message; the exception is re-raised so
    the caller (manual stop endpoint or scheduler) can surface it.
    """
    row = session.get(BotSession, bot_session_id)
    if row is None:
        raise BotSessionNotFoundError(
            f"no bot_sessions row with id={bot_session_id}"
        )
    if row.status in _TERMINAL_STATUSES:
        return row

    # Browser sessions run in-process in the API, not in a container.
    # Route them to the in-process runner instead of the docker launcher
    # so "Leave now" from the sidebar actually stops the pipeline and
    # publishes the SessionStatusChanged event the playground listens for
    # (Johnny-8zv). Importing lazily avoids an api↔service import cycle.
    if row.source == BotSessionSource.BROWSER:
        from app.api.browser_sessions import (
            publish_session_status_oneoff,
            request_browser_session_stop,
        )

        if request_browser_session_stop(row.id):
            # A live runner was signalled; its cleanup marks the row ended
            # and publishes the status event. Leave the row as-is here.
            logger.info("stopped browser bot_session id=%s (in-process)", row.id)
            return row
        # No live runner (e.g. API restarted and lost the registry). The
        # row is stale-active — end it directly and publish so the UI
        # still reacts.
        mark_session_ended(session, row.id)
        await publish_session_status_oneoff(str(row.id), "ended", None)
        logger.info("ended stale browser bot_session id=%s (no runner)", row.id)
        return row

    try:
        await launcher.stop(
            bot_session_id=row.id, container_name=row.container_name
        )
    except Exception as exc:
        mark_session_failed(session, row.id, f"launcher.stop failed: {exc}")
        raise

    mark_session_ended(session, row.id)
    logger.info(
        "stopped bot_session id=%s (container=%s)",
        row.id,
        row.container_name,
    )
    return row


# --- Periodic pass --------------------------------------------------------


@dataclass(frozen=True)
class SchedulerPassResult:
    """Aggregate counts across one scheduler pass."""

    started_count: int
    stopped_count: int
    error_count: int
    # Rows moved out of ``waiting_for_relogin`` into ``failed`` because the
    # meeting ended or the re-login wait timed out (Johnny-ebf).
    settled_count: int = 0


async def run_scheduler_pass_with_session(
    session: Session,
    *,
    launcher: ContainerLauncher,
    now: datetime | None = None,
    join_window_seconds: int = DEFAULT_JOIN_WINDOW_SECONDS,
    stop_grace_seconds: int = DEFAULT_STOP_GRACE_SECONDS,
    relogin_ttl_seconds: int = DEFAULT_RELOGIN_TTL_SECONDS,
) -> SchedulerPassResult:
    """Run one scheduler pass against the given session.

    Per-row errors are caught (so one bad meeting / launcher hiccup
    doesn't stall the whole pass) and aggregated into ``error_count``.
    The caller's session must wrap a transaction; rows touched here
    are flushed but not committed.
    """
    moment = now or _now()
    started = 0
    stopped = 0
    settled = 0
    errors = 0

    # Start sweep.
    due = select_due_meetings(
        session,
        now=moment,
        join_window_seconds=join_window_seconds,
    )
    for meeting in due:
        try:
            await start_session_for_meeting(
                session, meeting=meeting, launcher=launcher
            )
            started += 1
        except (ValueError, LauncherError) as exc:
            logger.warning(
                "scheduler start failed meeting_config_id=%s: %s",
                meeting.id,
                exc,
            )
            errors += 1
        except Exception:  # noqa: BLE001 — last-resort safety net
            logger.exception(
                "scheduler start crashed for meeting_config_id=%s",
                meeting.id,
            )
            errors += 1

    # Stop sweep.
    due_stops = select_due_stops(
        session, now=moment, stop_grace_seconds=stop_grace_seconds
    )
    for row in due_stops:
        try:
            await stop_session_by_id(
                session, bot_session_id=row.id, launcher=launcher
            )
            stopped += 1
        except (BotSessionNotFoundError, LauncherError) as exc:
            logger.warning(
                "scheduler stop failed bot_session_id=%s: %s",
                row.id,
                exc,
            )
            errors += 1
        except Exception:  # noqa: BLE001 — last-resort safety net
            logger.exception(
                "scheduler stop crashed for bot_session_id=%s",
                row.id,
            )
            errors += 1

    # Relogin settle sweep. A signed-out session waits for the operator to
    # re-login, but settles to ``failed`` (preserving a clear reason) once the
    # meeting ends or the wait times out — a pure status flip, no container to
    # stop (the worker already exited when it hit the chooser page). Johnny-ebf.
    end_threshold = moment - timedelta(seconds=stop_grace_seconds)
    for row in select_relogin_to_settle(
        session,
        now=moment,
        stop_grace_seconds=stop_grace_seconds,
        ttl_seconds=relogin_ttl_seconds,
    ):
        try:
            event = row.meeting_config.calendar_event if row.meeting_config else None
            meeting_ended = (
                event is not None and _as_utc(event.end_time) <= end_threshold
            )
            reason = (
                "Account was signed out and the meeting ended before re-login."
                if meeting_ended
                else "Account was signed out and re-login was not completed in time."
            )
            mark_session_failed(session, row.id, reason)
            settled += 1
        except BotSessionNotFoundError as exc:  # pragma: no cover — row vanished
            logger.warning(
                "scheduler relogin-settle failed bot_session_id=%s: %s",
                row.id,
                exc,
            )
            errors += 1
        except Exception:  # noqa: BLE001 — last-resort safety net
            logger.exception(
                "scheduler relogin-settle crashed for bot_session_id=%s",
                row.id,
            )
            errors += 1

    return SchedulerPassResult(
        started_count=started,
        stopped_count=stopped,
        error_count=errors,
        settled_count=settled,
    )


async def run_scheduler_pass(
    *,
    launcher: ContainerLauncher | None = None,
) -> SchedulerPassResult:
    """Open a fresh DB session and run one pass.

    Mirrors :func:`app.services.calendar_polling.run_polling_pass`.
    Intended for the worker's periodic scheduler; when US-029 introduces
    a real task queue, this becomes a registered beat task without changes.
    """
    from app.db.session import session_scope

    chosen_launcher = launcher or NoopContainerLauncher()
    with session_scope() as session:
        return await run_scheduler_pass_with_session(
            session, launcher=chosen_launcher
        )


__all__ = [
    "ContainerLauncher",
    "DEFAULT_JOIN_WINDOW_SECONDS",
    "DEFAULT_RELOGIN_TTL_SECONDS",
    "DEFAULT_SCHEDULER_INTERVAL_SECONDS",
    "DEFAULT_STOP_GRACE_SECONDS",
    "LaunchContext",
    "LaunchResult",
    "LauncherError",
    "NoopContainerLauncher",
    "SCHEDULER_INTERVAL_ENV",
    "SchedulerPassResult",
    "container_name_for_session",
    "get_scheduler_interval_seconds",
    "list_active_sessions",
    "run_scheduler_pass",
    "run_scheduler_pass_with_session",
    "select_due_meetings",
    "select_due_stops",
    "select_relogin_to_settle",
    "start_session_for_meeting",
    "stop_session_by_id",
]
