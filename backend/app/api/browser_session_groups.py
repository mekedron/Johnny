"""Multi-agent playground session groups (Johnny-trt.48).

The operator's ensemble test surface: lead a simulated meeting from the
browser with SEVERAL agents and watch them coexist — floor handoffs, peer
suppression, (later, Johnny-trt.47) turn claims — without burning a real
Meet. One *group* start launches N in-process browser sessions (one
``bot_sessions`` row + one :class:`BrowserSessionRunner` per selected agent,
each running the full single-session pipeline unchanged) stitched together
by two pieces:

* a shared **speech floor** — every member's job carries the same
  ``browser-group-{group_id}`` floor scope, so their speak paths contend on
  one meeting-style Redis lock (Johnny-trt.46) and label each other's audio
  exactly like meeting co-agents;
* a :class:`~johnny.voice_pipeline.group_audio.GroupAudioRouter` — ONE
  browser WebSocket fans the user's mic out to every member, merges their
  TTS into one playback stream, and cross-feeds each member's audio to its
  peers at real-time pace so suppression is exercised on genuine audio.

The group id is the first member's ``bot_session_id`` (the *leader*); the
browser attaches to ``/ws/sessions/groups/{group_id}/audio``. Members stay
first-class sessions everywhere else: their rows appear in the session list,
their event feeds (``/ws/sessions/{id}``) drive the playground's per-agent
state strip, per-member stop (``POST /sessions/browser/{id}/stop``) ends one
agent while the group survives, and the one-active-browser-session rule
(Johnny-8zv.2) treats the whole group as "the one active session".

This module deliberately imports the single-session module's private spec
builders — the group surface is an extension of that API, not a parallel
implementation, and reusing ``_build_spec_playground`` / ``_spawn_runner``
verbatim is what keeps a member byte-identical to a classic playground
session (plus the floor scope).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketState

from app.api.browser_sessions import (
    DEFAULT_SAMPLE_RATE,
    DISCONNECT_GRACE_SECONDS,
    BrowserProviderOverride,
    BrowserSessionRead,
    StartBrowserSessionPayload,
    _build_spec_playground,
    _row_to_read,
    _spawn_runner,
    ensure_no_live_browser_session,
    get_session_runner,
)
from app.api.deps import get_session
from app.db.models import (
    Agent,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
)
from app.services.bot_sessions import (
    BotSessionNotFoundError,
    mark_session_joined,
)
from johnny.voice_pipeline import GroupAudioRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions/browser/groups", tags=["browser-session-groups"])
ws_router = APIRouter(tags=["browser-session-groups-ws"])

FLOOR_SCOPE_TEMPLATE = "browser-group-{group_id}"
"""The group's speech-floor scope token (Johnny-trt.46 lock keyspace).

A string namespace, so it can never collide with the integer
``meeting_config_id`` scopes real meetings use.
"""

# In-memory registry of live groups, keyed by group id (= leader member's
# bot_session_id). Process-local like the runner registry — an API restart
# loses it and the stale rows are reaped by the one-active gate.
_session_groups: dict[int, BrowserSessionGroup] = {}


def floor_scope_for_group(group_id: int) -> str:
    return FLOOR_SCOPE_TEMPLATE.format(group_id=group_id)


# --- Pydantic schemas -------------------------------------------------------


class GroupAgentEntry(BaseModel):
    """One agent slot in a group start."""

    model_config = ConfigDict(extra="forbid")

    agent_id: int
    context: str | None = Field(default=None, max_length=8_000)
    """Per-member context brief; ``None`` inherits the group-level one."""


class StartBrowserGroupPayload(BaseModel):
    """Body of ``POST /sessions/browser/groups/start``.

    A group is 2+ explicit agents — a single selected agent goes through the
    classic ``POST /sessions/browser/start`` (whose behavior this task leaves
    byte-identical). Unknown / duplicate agent ids are hard errors rather
    than the single path's silent degrade: an ensemble run is meaningless
    without the exact named agents.
    """

    model_config = ConfigDict(extra="forbid")

    agents: list[GroupAgentEntry] = Field(..., min_length=2)
    account_id: int | None = None
    context: str | None = Field(default=None, max_length=8_000)
    """Group-level context brief, inherited by members without their own."""
    provider_overrides: dict[str, BrowserProviderOverride] | None = None
    """Dev-only escape hatch, applied to every member (same contract as the
    single start)."""


class GroupMemberRead(BaseModel):
    """One member session, with the agent identity the strip renders."""

    session: BrowserSessionRead
    agent_id: int
    agent_name: str


class BrowserGroupRead(BaseModel):
    """Public view of one live (or just-stopped) playground group."""

    group_id: int
    audio_ws_path: str
    sample_rate: int = DEFAULT_SAMPLE_RATE
    members: list[GroupMemberRead]


# --- Registry ----------------------------------------------------------------


@dataclasses.dataclass
class BrowserSessionGroup:
    """One live multi-agent playground group.

    Holds the audio router + group-level WebSocket state. The member
    runners themselves live in the single-session runner registry — every
    per-member surface (text input, stop, sidebar leave-now) works on them
    unchanged.
    """

    group_id: int
    member_ids: list[int]
    member_names: dict[int, str]
    audio: GroupAudioRouter
    started_at: float = dataclasses.field(default_factory=time.monotonic)
    ws_connected: bool = False
    disconnect_timer: asyncio.TimerHandle | None = None
    silent_drain_task: asyncio.Task[None] | None = None
    monitor_task: asyncio.Task[None] | None = None


def get_session_group(group_id: int) -> BrowserSessionGroup | None:
    return _session_groups.get(group_id)


def group_id_for_member(bot_session_id: int) -> int | None:
    """The live group this session belongs to, or ``None``.

    Registry-backed (not the persisted overrides fragment) so the answer
    reflects what is actually running in this process — a member of an
    already-torn-down group is attachable as a plain session again.
    """
    for group in _session_groups.values():
        if bot_session_id in group.member_ids:
            return group.group_id
    return None


def _teardown_group(group_id: int, *, reason: str | None = None) -> None:
    """Drop the group's process-level structures. Idempotent.

    Member sessions/rows are NOT touched here — each member runner's own
    cleanup marks its row and publishes its status; this only closes the
    group-side audio glue once nothing (or nobody) is left to stream.
    """
    group = _session_groups.pop(group_id, None)
    if group is None:
        return
    _cancel_group_watchdog(group)
    if group.monitor_task is not None and not group.monitor_task.done():
        group.monitor_task.cancel()
    if reason:
        group.audio.notify_ended(reason)
    group.audio.close()
    logger.info("browser group %s torn down (%s)", group_id, reason or "all members ended")


async def _monitor_group(group: BrowserSessionGroup) -> None:
    """Watch member runners; detach ended members; end the group with the last.

    The per-member runner task is the member's lifetime (its cleanup marks
    the row + publishes status). Removing an ended member from the audio
    router keeps the survivors' streams flowing — "end one agent, the group
    survives" — and the group itself tears down only when no member remains.
    """
    pending: dict[asyncio.Task[None], int] = {}
    for member_id in list(group.member_ids):
        runner = get_session_runner(member_id)
        if runner is not None:
            pending[runner.task] = member_id
    try:
        while pending:
            done, _ = await asyncio.wait(
                set(pending), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                member_id = pending.pop(task)
                group.audio.remove_member(member_id)
                with contextlib.suppress(ValueError):
                    group.member_ids.remove(member_id)
                logger.info(
                    "browser group %s: member %s ended (%d left)",
                    group.group_id,
                    member_id,
                    len(pending),
                )
    except asyncio.CancelledError:
        raise
    finally:
        if not pending:
            _teardown_group(group.group_id, reason="group ended")


def _stop_all_member_speech(group: BrowserSessionGroup) -> None:
    """The group Stop control: cut every member's speech + purge group audio.

    Mirrors the single-session stop control (pipeline.interrupt +
    transport.cancel_playback per member) so the stop attribution
    (Johnny-trt.49 ``bot_cut_by_stop``) lands on whichever member was
    actually speaking; members with no active speech settle it as a no-op.
    """
    for member_id in list(group.member_ids):
        runner = get_session_runner(member_id)
        if runner is None:
            continue
        pipeline = getattr(runner, "pipeline", None)
        if pipeline is not None:
            try:
                pipeline.interrupt()
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "group stop: pipeline.interrupt() raised for member=%s", member_id
                )
        try:
            runner.transport.cancel_playback()
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "group stop: cancel_playback raised for member=%s", member_id
            )
    group.audio.cancel_playback()


def _signal_group_stop(group: BrowserSessionGroup) -> None:
    """Ask every member runner to stop; the monitor finishes the teardown."""
    for member_id in list(group.member_ids):
        runner = get_session_runner(member_id)
        if runner is not None:
            runner.stop_event.set()


# --- Endpoints ---------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]


def _group_read(group: BrowserSessionGroup, session: Session) -> BrowserGroupRead:
    members: list[GroupMemberRead] = []
    for member_id in group.member_ids:
        row = session.get(BotSession, member_id)
        if row is None:
            continue
        members.append(
            GroupMemberRead(
                session=_row_to_read(row),
                agent_id=row.agent_id or 0,
                agent_name=group.member_names.get(member_id)
                or row.bot_name
                or f"agent-{member_id}",
            )
        )
    return BrowserGroupRead(
        group_id=group.group_id,
        audio_ws_path=f"/ws/sessions/groups/{group.group_id}/audio",
        sample_rate=DEFAULT_SAMPLE_RATE,
        members=members,
    )


@router.post(
    "/start",
    response_model=BrowserGroupRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_browser_group(
    payload: Annotated[StartBrowserGroupPayload, Body()],
    session: SessionDep,
) -> BrowserGroupRead:
    """Start one playground group: N member sessions behind one audio socket."""
    from app.services.session_scheduler import MAX_AGENTS_PER_MEETING

    await ensure_no_live_browser_session(session)

    if len(payload.agents) > MAX_AGENTS_PER_MEETING:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{len(payload.agents)} agents exceed the per-meeting cap of "
                f"{MAX_AGENTS_PER_MEETING}"
            ),
        )
    agent_ids = [entry.agent_id for entry in payload.agents]
    if len(set(agent_ids)) != len(agent_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="each agent can appear in a group only once",
        )
    agents: dict[int, Agent] = {}
    for agent_id in agent_ids:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"agent id={agent_id} not found",
            )
        agents[agent_id] = agent

    # All member rows first, one flush — the leader's id is the group id and
    # every member's overrides fragment names the full roster.
    rows: list[BotSession] = []
    for _entry in payload.agents:
        row = BotSession(
            meeting_config_id=None,
            account_id=payload.account_id,
            source=BotSessionSource.BROWSER,
            status=BotSessionStatus.JOINING,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    group_id = rows[0].id
    member_ids = [row.id for row in rows]
    scope = floor_scope_for_group(group_id)

    audio = GroupAudioRouter(sample_rate=DEFAULT_SAMPLE_RATE)
    member_names: dict[int, str] = {}
    spawned: list[int] = []
    try:
        # Two passes (Johnny-trt.47): every member's router prompt carries the
        # peer roster, and the roster is only complete once every member's
        # agent has resolved — so resolve all specs first, then stamp each
        # spec's snapshot with the OTHER members' names, then spawn.
        prepared: list[tuple[Any, dict[str, Any]]] = []
        for position, (entry, row) in enumerate(zip(payload.agents, rows, strict=True)):
            member_payload = StartBrowserSessionPayload(
                agent_id=entry.agent_id,
                account_id=payload.account_id,
                context=entry.context if entry.context is not None else payload.context,
                provider_overrides=payload.provider_overrides,
            )
            spec, overrides_snapshot, resolution = _build_spec_playground(
                session, bot_session_id=row.id, payload=member_payload
            )
            spec = dataclasses.replace(spec, floor_scope=scope)
            overrides_snapshot["group"] = {
                "id": group_id,
                "member_ids": member_ids,
                "leader": group_id,
                "position": position,
            }
            row.playground_overrides = overrides_snapshot
            if resolution.agent is not None:
                row.agent_id = resolution.agent.id
                row.bot_name = resolution.agent.name
            member_names[row.id] = (
                resolution.agent.name if resolution.agent is not None else f"agent-{row.id}"
            )
            prepared.append((spec, overrides_snapshot))

        for row, (spec, _overrides) in zip(rows, prepared, strict=True):
            peers = [
                name
                for member_id, name in member_names.items()
                if member_id != row.id and name.strip()
            ]
            snapshot = {**dict(spec.agent_snapshot), "peer_names": peers}
            spec = dataclasses.replace(spec, agent_snapshot=snapshot)
            if row.agent_id is not None:
                row.agent_snapshot = snapshot
            runner = _spawn_runner(bot_session_id=row.id, spec=spec)
            spawned.append(row.id)
            audio.add_member(row.id, runner.transport)
    except Exception:
        # Roll the half-started group back: stop what was spawned (their
        # cleanup is harmless against the rolled-back rows) and close the
        # router. The HTTP error then reports the real cause.
        for member_id in spawned:
            spawned_runner = get_session_runner(member_id)
            if spawned_runner is not None:
                spawned_runner.stop_event.set()
        audio.close()
        raise

    group = BrowserSessionGroup(
        group_id=group_id,
        member_ids=list(member_ids),
        member_names=member_names,
        audio=audio,
    )
    _session_groups[group_id] = group
    group.monitor_task = asyncio.create_task(
        _monitor_group(group), name=f"browser-group-monitor-{group_id}"
    )

    for row in rows:
        try:
            mark_session_joined(session, row.id)
        except BotSessionNotFoundError:  # pragma: no cover — just flushed
            pass
    # The joined broadcasts ride each member's own event bus so the sidebar
    # and the session pages learn about all N sessions, exactly like singles.
    from app.api.browser_sessions import _publish_session_status

    for member_id in member_ids:
        member_runner = get_session_runner(member_id)
        if member_runner is not None and member_runner.event_bus is not None:
            await _publish_session_status(
                member_runner.event_bus, str(member_id), "joined"
            )

    logger.info(
        "browser group %s started: members=%s floor_scope=%s",
        group_id,
        member_ids,
        scope,
    )
    return _group_read(group, session)


@router.get("/active", response_model=list[BrowserGroupRead])
def list_active_groups(session: SessionDep) -> list[BrowserGroupRead]:
    """Live groups in this process (0 or 1 under the one-active rule)."""
    return [_group_read(group, session) for group in _session_groups.values()]


@router.post("/{group_id}/stop", response_model=BrowserGroupRead)
async def stop_browser_group(group_id: int, session: SessionDep) -> BrowserGroupRead:
    """End the whole group: every member stops; the monitor tears down."""
    group = get_session_group(group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="browser session group not found (already ended?)",
        )
    snapshot = _group_read(group, session)
    _signal_group_stop(group)
    return snapshot


class GroupTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=4_000)


@router.post("/{group_id}/text", status_code=status.HTTP_202_ACCEPTED)
async def post_group_text(
    group_id: int,
    payload: Annotated[GroupTextInput, Body()],
    session: SessionDep,
) -> dict[str, Any]:
    """Say one thing to the whole room: the text reaches every member's gate.

    The group analogue of the single-session text endpoint — each member
    runs its own router verdict on the same utterance (the trt.47 turn-claim
    tuning surface). A member whose pipeline isn't ready gets the chunk
    persisted instead, so its transcript still records what was said.
    """
    group = get_session_group(group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="browser session group not found (already ended?)",
        )
    text = payload.text.strip()

    # Concurrent fan-out, not a sequential loop: feed_text awaits the gate's
    # run_turn, and a member whose SPEAK verdict is waiting for the floor
    # blocks inside it for up to the acquire timeout — sequential feeding
    # would wedge this request for the duration of the co-agent's reply.
    async def _feed(member_id: int) -> bool:
        runner = get_session_runner(member_id)
        pipeline = getattr(runner, "pipeline", None) if runner else None
        if pipeline is None:
            return False
        try:
            return bool(await pipeline.feed_text(text))
        except Exception:  # noqa: BLE001 — never block on one member
            logger.exception("group text: feed_text failed for member=%s", member_id)
            return False

    member_ids = list(group.member_ids)
    fed = await asyncio.gather(*(_feed(member_id) for member_id in member_ids))
    results: dict[str, bool] = {}
    for member_id, accepted in zip(member_ids, fed, strict=True):
        if not accepted:
            from app.db.models import TranscriptChunk

            session.add(
                TranscriptChunk(
                    bot_session_id=member_id,
                    start_offset_ms=0,
                    end_offset_ms=0,
                    speaker="user",
                    text=text,
                )
            )
        results[str(member_id)] = accepted
    session.flush()
    return {"accepted": True, "drove_pipeline": results}


# --- WebSocket: the group audio stream ----------------------------------------


@ws_router.websocket("/ws/sessions/groups/{group_id}/audio")
async def group_audio_socket(websocket: WebSocket, group_id: int) -> None:
    """One bidirectional PCM stream for the whole group.

    Same wire protocol as the single-session socket (the browser audio
    client is unchanged): binary frames in = the user's mic (fanned out to
    every member), binary frames out = the merged TTS stream, JSON text =
    control messages. ``{"type":"stop"}`` cuts every member's speech;
    member-originated interrupts are forwarded tagged with ``member``.
    """
    group = get_session_group(group_id)
    if group is None or group.audio.is_closed:
        await websocket.accept()
        await websocket.send_json({"type": "ended", "reason": "group not active"})
        await websocket.close(code=1011)
        return

    if group.ws_connected:
        await websocket.accept()
        await websocket.send_json(
            {"type": "ended", "reason": "group already attached in another tab"}
        )
        await websocket.close(code=1008)
        return

    _cancel_group_watchdog(group)
    await websocket.accept()
    group.ws_connected = True
    await websocket.send_json(
        {
            "type": "ready",
            "session_id": group_id,
            "group_id": group_id,
            "member_ids": list(group.member_ids),
            "sample_rate": DEFAULT_SAMPLE_RATE,
        }
    )

    disconnect = asyncio.Event()

    async def receiver() -> None:
        try:
            while True:
                msg = await websocket.receive()
                kind = msg.get("type")
                if kind == "websocket.disconnect":
                    disconnect.set()
                    return
                if "bytes" in msg and msg["bytes"] is not None:
                    group.audio.push_capture_frame(msg["bytes"])
                    continue
                if "text" in msg and msg["text"] is not None:
                    if _handle_group_control(msg["text"], group=group) == "disconnect":
                        disconnect.set()
                        return
        except WebSocketDisconnect:
            disconnect.set()

    async def sender() -> None:
        async for frame in group.audio.drain_playback_frames():
            if disconnect.is_set():
                return
            try:
                await websocket.send_bytes(frame)
            except (WebSocketDisconnect, RuntimeError):
                disconnect.set()
                return

    async def control_sender() -> None:
        async for control in group.audio.drain_control_messages():
            if disconnect.is_set():
                return
            try:
                await websocket.send_json(control)
            except (WebSocketDisconnect, RuntimeError):
                disconnect.set()
                return

    recv_task = asyncio.create_task(receiver())
    send_task = asyncio.create_task(sender())
    control_task = asyncio.create_task(control_sender())
    try:
        await asyncio.wait(
            (recv_task, send_task, control_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (recv_task, send_task, control_task):
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        group.ws_connected = False
        # Same reattach contract as singles (Johnny-ckz.11): a closed tab
        # does not end the group; the watchdog stops every member only if
        # nobody reattaches within the grace window.
        if not group.audio.is_closed:
            _schedule_group_watchdog(group)
        if websocket.client_state is not WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


def _handle_group_control(raw: str, *, group: BrowserSessionGroup) -> str | None:
    import json

    body = raw.strip()
    if not body:
        return None
    try:
        msg = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    if kind == "end":
        return "disconnect"
    if kind == "stop":
        logger.info("group %s: client stop control received", group.group_id)
        _stop_all_member_speech(group)
        return None
    return None


def _schedule_group_watchdog(group: BrowserSessionGroup) -> None:
    """Grace timer + silent drain while no tab is attached (single parity)."""
    loop = asyncio.get_event_loop()

    def _on_grace_expired() -> None:
        if group.ws_connected or group.audio.is_closed:
            return
        logger.info(
            "browser group %s: disconnect grace expired; stopping all members",
            group.group_id,
        )
        _signal_group_stop(group)

    group.disconnect_timer = loop.call_later(DISCONNECT_GRACE_SECONDS, _on_grace_expired)

    async def _silent_drain() -> None:
        async def _drain_audio() -> None:
            try:
                async for _frame in group.audio.drain_playback_frames():
                    if group.ws_connected:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "group %s: silent playback drain crashed", group.group_id
                )

        async def _drain_control() -> None:
            try:
                async for _msg in group.audio.drain_control_messages():
                    if group.ws_connected:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — defensive
                logger.exception(
                    "group %s: silent control drain crashed", group.group_id
                )

        await asyncio.gather(_drain_audio(), _drain_control(), return_exceptions=True)

    group.silent_drain_task = asyncio.create_task(
        _silent_drain(), name=f"group-silent-drain-{group.group_id}"
    )


def _cancel_group_watchdog(group: BrowserSessionGroup) -> None:
    if group.disconnect_timer is not None:
        with contextlib.suppress(Exception):
            group.disconnect_timer.cancel()
        group.disconnect_timer = None
    if group.silent_drain_task is not None:
        if not group.silent_drain_task.done():
            group.silent_drain_task.cancel()
        group.silent_drain_task = None


__all__ = [
    "BrowserGroupRead",
    "BrowserSessionGroup",
    "GroupAgentEntry",
    "GroupMemberRead",
    "StartBrowserGroupPayload",
    "floor_scope_for_group",
    "get_session_group",
    "group_id_for_member",
    "router",
    "ws_router",
]
