"""Bot session scheduler (US-029).

Picks meetings that are about to start, spawns a meet-worker for each,
and tears them down once the event ends. The "spawning" is delegated to
a pluggable :class:`ContainerLauncher` — the default is a no-op that
just records the call, so the scheduler itself can be exercised end-to-end
without Docker. US-030 will land a Docker-backed launcher.

Shape:

* :func:`select_due_meetings` — find meeting_configs whose event starts
  within the next ``join_window_seconds`` and has no active bot_session.
* :func:`start_sessions_for_meeting` — create one ``scheduled`` bot_session
  row PER enabled agent assignment (Johnny-trt.45; no assignments → one
  session on the default agent), call ``launcher.start`` for each, then
  transition to ``joining``. Errors during launch are translated into
  ``failed``.
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
from collections.abc import Mapping
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
    MeetingAgent,
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

    ``agent_snapshot`` and ``provider_config`` are passed as environment
    variables to the meet-worker by the Docker launcher (US-030) so the
    pipeline knows how to behave and which providers are active. Defaults
    keep the scheduler's no-op path unchanged.
    """

    bot_session_id: int
    meeting_config_id: int
    calendar_event_id: int
    identity_account_id: int
    meet_link: str
    container_name: str
    agent_id: int | None = None
    """The agent serving this session (Johnny-trt.45). ``None`` only when
    no agent exists at all — the session then runs the contract defaults."""
    agent_snapshot: Mapping[str, Any] = field(default_factory=dict)
    """The agent's frozen behavior + provider-pin blob (Johnny-trt.41/45).

    The exact dict persisted on ``bot_sessions.agent_snapshot`` at dispatch
    — including the per-assignment ``assignment_context`` brief. Forwarded
    to the meet-worker as one ``JOHNNY_AGENT_SNAPSHOT`` JSON env var (and to
    the dispatched agent worker as job metadata); it replaced the per-field
    mode / character / context / allowed-replies / threshold env overrides.
    Empty when no agent resolved (contract defaults).
    """
    calendar_context: str = ""
    """Calendar event description — pre-meeting context from the event itself.

    Per-meeting (not per-agent), so it rides next to the snapshot rather
    than inside it. Kept distinct from the assignment brief so an audit can
    tell them apart and the user can edit one without disturbing the other.
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
    there must be no bot_session row in scheduled/joining/joined
    status for it, and the bot must not be dismissed for the current
    occurrence (Johnny-trt.56): a dismissal is in force while the event's
    current ``start_time`` still falls inside the window captured at
    dismissal time (``start_time <= bot_dismissed_until``) — see
    :mod:`app.services.meeting_lifecycle` for the occurrence-scoping rule.
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
        # Dismissed-for-this-occurrence rows don't dispatch. An event moved
        # entirely past the dismissed window (start > dismissed_until) is a
        # new occurrence, so the dismissal lapses by design.
        .where(
            or_(
                MeetingConfig.bot_dismissed_until.is_(None),
                CalendarEvent.start_time > MeetingConfig.bot_dismissed_until,
            )
        )
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


def _enabled_assignments(meeting: MeetingConfig) -> list[MeetingAgent]:
    """The meeting's enabled agent assignments, ordered by (position, id)."""
    candidates = [a for a in meeting.agent_assignments if a.enabled]
    candidates.sort(key=lambda a: (a.position, a.id))
    return candidates


def _build_base_provider_payload(session: Session) -> dict[str, Any]:
    """Materialise the global-active provider rows for the meet-worker.

    Built once per meeting launch and shared across its per-assignment
    sessions (each assignment then layers its agent's pins on a copy).
    ``crypto`` is the production CredentialCrypto; a startup
    misconfiguration (no FERNET_KEY) degrades gracefully to an empty
    payload so the bot still joins — it just runs in listen-only mode.
    """
    from app.security.crypto import CryptoError

    try:
        from app.security.crypto import get_crypto
        from app.services.provider_payload import build_provider_payload

        return build_provider_payload(session, get_crypto())
    except CryptoError as exc:
        logger.warning(
            "provider payload skipped — FERNET_KEY not configured (%s); "
            "meet-worker will run without providers",
            exc,
        )
        return {}
    except Exception:  # noqa: BLE001 — never block a launch on payload errors
        logger.exception("provider payload build failed; sending empty payload")
        return {}


def _find_prior_session_context(
    session: Session,
    *,
    meeting: MeetingConfig,
    exclude_bot_session_id: int,
) -> str:
    """Prior-occurrence summary for recurring meetings (Johnny-dsy).

    ``exclude`` guards the corner case where this row already has a summary
    written (re-launch after a crash) so we don't echo our own state back
    at ourselves. Never blocks a launch.
    """
    event = meeting.calendar_event
    recurring_event_id = event.recurring_event_id if event is not None else None
    try:
        from app.services.history import find_prior_session_summary

        prior = find_prior_session_summary(
            session,
            recurring_event_id=recurring_event_id,
            exclude_bot_session_id=exclude_bot_session_id,
        )
        return prior.summary if prior is not None else ""
    except Exception:  # noqa: BLE001 — never block a launch on history lookup
        logger.exception(
            "prior_session_summary lookup failed for meeting_config=%s; "
            "continuing without cross-session context",
            meeting.id,
        )
        return ""


async def _start_one_session(
    session: Session,
    *,
    meeting: MeetingConfig,
    launcher: ContainerLauncher,
    meet_link: str,
    agent: Any,
    assignment: MeetingAgent | None,
    base_provider_payload: Mapping[str, Any],
) -> BotSession:
    """Create + launch ONE bot_session for one agent assignment.

    ``agent`` is the resolved :class:`~app.db.models.Agent` row (or ``None``
    for the contract-default degrade); ``assignment`` the
    :class:`MeetingAgent` row that selected it (``None`` for the
    no-assignments default-agent fallback). The flow per session:

    1. Insert the ``bot_sessions`` row in ``scheduled`` so its id exists
       before we tell the launcher (the id is part of the container name).
    2. Freeze the agent's behavior + the per-assignment ``context`` brief
       onto the row as ``agent_snapshot`` (Johnny-trt.41/45) — everything
       downstream reads the snapshot, never the live agents/meeting_agents
       rows.
    3. Resolve the agent's provider pins onto a copy of the shared base
       payload (Johnny-trt.42).
    4. ``await launcher.start(...)``; failures are recorded on the row via
       :func:`mark_session_failed` and re-raised.
    5. Persist the returned ``container_name``, transition to ``joining``,
       and dispatch the agent worker into the session's room.
    """
    row = BotSession(
        meeting_config_id=meeting.id,
        # Tag with the calendar owner so History can filter by account
        # (Johnny-8th). MeetingConfig.calendar_event is non-null by schema.
        account_id=meeting.calendar_event.account_id,
        status=BotSessionStatus.SCHEDULED,
    )
    session.add(row)
    session.flush()

    # Freeze the agent's behavior (Johnny-trt.41) + the assignment brief
    # (Johnny-trt.45) onto the row before dispatch. Guarded so a snapshot
    # glitch degrades to the contract defaults rather than blocking launch.
    agent_snapshot: dict[str, Any] | None = None
    if agent is not None:
        try:
            from app.services.agents import build_agent_snapshot

            agent_snapshot = build_agent_snapshot(
                agent,
                assignment_context=(assignment.context if assignment is not None else None),
            )
            row.agent_id = agent.id
            row.agent_snapshot = agent_snapshot
            row.bot_name = agent.name
        except Exception:  # noqa: BLE001 — never block a launch on agent errors
            agent_snapshot = None
            logger.exception(
                "agent snapshot failed for meeting_config=%s agent=%s; "
                "launching with contract defaults",
                meeting.id,
                getattr(agent, "id", None),
            )

    # Johnny-trt.42: apply the snapshot's provider pins to a copy of the
    # global payload so the dispatched session runs the agent's providers
    # (answer/router LLM roles, TTS + voice, the reasoning stamp). Unusable
    # pins degrade to the global-active entry with a visible provider_switch
    # row in the activity log; any resolver error degrades to the unresolved
    # payload — provider resolution must never block a launch.
    provider_payload: dict[str, Any] = dict(base_provider_payload)
    if agent_snapshot is not None and provider_payload:
        try:
            from app.security.crypto import get_crypto
            from app.services.agent_providers import (
                persist_provider_fallback_warnings,
                resolve_agent_provider_payload,
            )

            resolved = resolve_agent_provider_payload(
                session,
                get_crypto(),
                base_payload=provider_payload,
                snapshot=agent_snapshot,
                context_label=f"bot_session={row.id}",
            )
            provider_payload = resolved.payload
            persist_provider_fallback_warnings(
                session, bot_session_id=row.id, warnings=resolved.warnings
            )
        except Exception:  # noqa: BLE001 — never block a launch on pin resolution
            logger.exception(
                "agent provider resolution failed for bot_session=%s; "
                "launching with the global-active payload",
                row.id,
            )

    event = meeting.calendar_event
    calendar_description = (event.description or "") if event is not None else ""
    calendar_attachments = (event.attachments_text or "") if event is not None else ""

    # Per-assignment identity (Johnny-trt.45): each agent joins the Meet as
    # its own Google account so two agents appear as two participants — a
    # single account cannot join one Meet twice. The meeting-level identity
    # account remains the fallback for assignments that pin none.
    identity_account_id = meeting.identity_account_id
    if assignment is not None and assignment.identity_account_id is not None:
        identity_account_id = assignment.identity_account_id

    ctx = LaunchContext(
        bot_session_id=row.id,
        meeting_config_id=meeting.id,
        calendar_event_id=meeting.calendar_event_id,
        identity_account_id=identity_account_id,
        meet_link=meet_link,
        container_name=container_name_for_session(row.id),
        agent_id=row.agent_id,
        agent_snapshot=agent_snapshot or {},
        calendar_context=calendar_description,
        calendar_attachments_text=calendar_attachments,
        prior_session_context=_find_prior_session_context(
            session, meeting=meeting, exclude_bot_session_id=row.id
        ),
        provider_config=provider_payload,
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
        "started bot_session id=%s for meeting_config id=%s agent=%s as container %s",
        row.id,
        meeting.id,
        row.agent_id,
        result.container_name,
    )

    # Agent-worker lifecycle (Johnny-9eh): when JOHNNY_ORCHESTRATOR=agentsession
    # (the default), dispatch the LiveKit agent into this session's room (one
    # room per session). Defensive — a dispatch failure never breaks the
    # meet-worker that is already running. The full per-session engine
    # selection (and the meet-worker→bridge switch) is Johnny-wz5.
    await maybe_dispatch_session_agent(ctx)
    return row


async def start_sessions_for_meeting(
    session: Session,
    *,
    meeting: MeetingConfig,
    launcher: ContainerLauncher,
) -> list[BotSession]:
    """Launch one bot_session PER enabled agent assignment (Johnny-trt.45).

    Meetings are configured by assigning agents (:class:`MeetingAgent`
    rows): each enabled assignment gets its own session, its own frozen
    agent snapshot (carrying the per-assignment ``context`` brief), its own
    provider resolution, and its own join identity (the assignment's
    ``identity_account_id``, falling back to the meeting-level account). A
    meeting with **no** enabled assignments falls back to one session on
    the default agent — the pre-trt.45 behavior.

    Per-assignment launcher failures are recorded on their own rows and do
    NOT stop the remaining assignments; the error re-raises only when *no*
    session could be launched at all, so the scheduler pass still counts a
    fully-failed meeting as an error. Returns the successfully-launched
    rows. The session is left uncommitted; the caller's outer transaction
    commits.

    The shared speech floor / peer awareness for the launched co-agents is
    sibling work (Johnny-trt.46) — this function owns the fan-out only.
    """
    meet_link = _validate_meeting_for_launch(meeting)
    base_provider_payload = _build_base_provider_payload(session)

    # Build the (agent, assignment) launch list. Assignment-less meetings
    # degrade to the default agent; agent resolution errors degrade to the
    # contract defaults (one agent-less session) — a launch is never blocked.
    launches: list[tuple[Any, MeetingAgent | None]] = []
    try:
        assignments = _enabled_assignments(meeting)
        if assignments:
            launches = [(a.agent, a) for a in assignments if a.agent is not None]
        if not launches:
            from app.services.agents import select_default_agent

            default_agent = select_default_agent(session)
            if default_agent is None:
                logger.warning(
                    "no agent resolved for meeting_config=%s (empty agents "
                    "table?); launching with contract defaults",
                    meeting.id,
                )
            launches = [(default_agent, None)]
    except Exception:  # noqa: BLE001 — never block a launch on agent errors
        logger.exception(
            "agent resolution failed for meeting_config=%s; "
            "launching with contract defaults",
            meeting.id,
        )
        launches = [(None, None)]

    rows: list[BotSession] = []
    last_error: Exception | None = None
    for agent, assignment in launches:
        try:
            rows.append(
                await _start_one_session(
                    session,
                    meeting=meeting,
                    launcher=launcher,
                    meet_link=meet_link,
                    agent=agent,
                    assignment=assignment,
                    base_provider_payload=base_provider_payload,
                )
            )
        except Exception as exc:  # noqa: BLE001 — keep launching the co-agents
            last_error = exc
            logger.warning(
                "assignment launch failed for meeting_config=%s agent=%s: %s",
                meeting.id,
                getattr(agent, "id", None),
                exc,
            )
    if not rows and last_error is not None:
        raise last_error
    return rows


async def start_session_for_meeting(
    session: Session,
    *,
    meeting: MeetingConfig,
    launcher: ContainerLauncher,
) -> BotSession:
    """Single-session compatibility wrapper over :func:`start_sessions_for_meeting`.

    Kept for callers that address "the" session of a meeting (the manual
    Join-now endpoint's response shape). Launches every enabled assignment
    exactly like the scheduler does and returns the FIRST launched row.
    """
    rows = await start_sessions_for_meeting(
        session, meeting=meeting, launcher=launcher
    )
    return rows[0]


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

    # Start sweep. One session launches per enabled agent assignment
    # (Johnny-trt.45); ``started`` counts sessions, not meetings.
    due = select_due_meetings(
        session,
        now=moment,
        join_window_seconds=join_window_seconds,
    )
    for meeting in due:
        try:
            rows = await start_sessions_for_meeting(
                session, meeting=meeting, launcher=launcher
            )
            started += len(rows)
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
    "start_sessions_for_meeting",
    "stop_session_by_id",
]
