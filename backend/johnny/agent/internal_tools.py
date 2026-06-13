"""Internal tools — first-party actions on Johnny's own application (Johnny-trt.57).

Skills run *outside* (sandbox commands, Johnny-trt.23) and future MCP tools run
*elsewhere* (Johnny-trt.36); internal tools are the third capability layer and
the first in the resolution order (internal → skills → mcp, the Johnny-trt.24
contract): in-process callables that act on Johnny itself, so the bot can obey
"please leave the meeting" or "end the session" by voice. v1 ships exactly two:

* ``meeting.leave`` — speak the farewell (the router-authored delegate ack),
  mark the meeting dismissed with ``actor=voice`` (the Johnny-trt.56 state, so
  the scheduler does NOT re-join this occurrence), and disconnect. Available
  only in Meet-backed sessions (surface scoping; off-surface it is an
  unavailable catalog entry the router declines honestly, Johnny-trt.55).
* ``session.end`` — same minus the meeting state: farewell, then a clean
  session stop. Available on every surface (playground + Meet).

**UI parity by construction**: each tool calls the SAME api endpoint its UI
button does — ``meeting.leave`` posts the Johnny-trt.56 bot-dismissal endpoint
("End for this meeting"), ``session.end`` posts ``/sessions/{id}/stop`` ("Leave
now") — so voice and click share one code path, including the Johnny-ajc stop
verification: a bot that did not actually disconnect comes back as a non-2xx,
the task settles ``failed``, and the Johnny-trt.53 correction says so out loud.
Never a silent no-op.

**Execution locality**: internal kinds run in the agent process itself
(session-local, the approval-flow precedent) — never the worker, never the
sandbox. :func:`johnny.skills.executor.build_skill_task_executor` carries the
matching locality guard refusing these kinds, so a stale catalog or a
hand-queued row cannot smuggle them into ``sandbox.exec``.

**Sequencing** (the bead's contract): the delegate ack IS the farewell — the
runner first awaits the attached farewell-wait seam
(:meth:`johnny.agent.router_gate.RouterGate.wait_recent_say_done`, attached
post-construction like ``attach_say``) so the goodbye finishes playing, then
performs the state change + teardown, then settles the ``agent_tasks`` row —
the Johnny-trt.54 history chain shows ask → decision → farewell → state change
→ teardown. The :meth:`TaskCoordinator.aclose` drain grace keeps the settle
from being cancelled by the very teardown the tool triggered.

Audit rides the ordinary ``agent_tasks`` row (no parallel record): row-before-
ack at queue time, ``done``/``failed`` + speech-ready ``result_text`` at
settle, linked to the turn like every delegated task.

Import-cheap on purpose (httpx only, the :mod:`johnny.skills.sandbox`
precedent) — :mod:`johnny.skills.executor` imports the kind predicate for its
locality guard, and the unit tests run without the ``agent`` extra.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.agent.tasks import QueuedTask, TaskExecutor, TaskResult

logger = logging.getLogger(__name__)

MEETING_LEAVE_KIND = "meeting.leave"
"""Leave the current meeting and stay out of this occurrence (Johnny-trt.56
dismissal with ``actor=voice``)."""

SESSION_END_KIND = "session.end"
"""End the running session cleanly — playground and Meet alike."""

INTERNAL_TOOL_KINDS: frozenset[str] = frozenset({MEETING_LEAVE_KIND, SESSION_END_KIND})
"""Every internal task kind. Exact-match membership (no prefix magic): the
registry below is the source of truth, and a skill package on the volume can
never claim one of these kinds (catalog merge drops shadows; the executor
resolves internal first; the skill executor refuses them outright)."""

API_BASE_URL_ENV = "JOHNNY_API_BASE_URL"
DEFAULT_API_BASE_URL = "http://api:8000"

FAREWELL_WAIT_TIMEOUT_S = 30.0
"""Upper bound on waiting for the farewell ack to finish playing before the
teardown proceeds — a wedged TTS must delay the leave, never block it."""

HTTP_TIMEOUT_S = 60.0
"""Read timeout for the in-app control calls. ``/stop`` and the dismissal
endpoint await the container launcher (a docker stop with its own grace), so
this stays generous; the coordinator's teardown drain grace — not this
timeout — bounds how long a settle may outlive the session."""


def api_base_url_from_env() -> str:
    """The Johnny api base URL for in-app control calls.

    Compose sets :data:`API_BASE_URL_ENV` for api / agent-worker (both run
    internal tools: the playground gate lives in the api process, the Meet
    gate in the agent worker); the default is the compose-network service
    address, which also works for the api calling itself.
    """
    return os.environ.get(API_BASE_URL_ENV, "").strip().rstrip("/") or DEFAULT_API_BASE_URL


def is_internal_kind(kind: str) -> bool:
    """True when ``kind`` is an internal tool — runs in the agent process only."""
    return kind in INTERNAL_TOOL_KINDS


@dataclass(frozen=True, slots=True)
class InternalToolSpec:
    """One internal tool: catalog face + surface scope.

    ``meeting_only`` is the surface predicate: ``True`` means the tool works
    only in Meet-backed sessions. Internal tools join Johnny-trt.55's
    availability model — off its surface the tool renders as an
    *unavailable* catalog entry carrying ``unavailable_reason`` (spoken-form,
    actionable), so the router declines honestly ("there's no meeting to
    leave") instead of improvising, and the gate's delegate backstop can
    never act on it. Unavailable entries carry no keywords (the trt.50
    delegate prior must not fire for impossible work).
    """

    kind: str
    one_liner: str
    keywords: tuple[str, ...] = ()
    meeting_only: bool = False
    unavailable_reason: str = ""

    def catalog_entry(self, *, available: bool = True) -> TaskCatalogEntry:
        # ``internal=True`` (Johnny-etu.7) so the answer-prompt positive block
        # never advertises a session-control verb as a user-facing capability;
        # the router catalog and the gate backstop read it unchanged.
        if available:
            return TaskCatalogEntry(
                kind=self.kind,
                one_liner=self.one_liner,
                keywords=self.keywords,
                internal=True,
            )
        return TaskCatalogEntry(
            kind=self.kind,
            one_liner=self.one_liner,
            keywords=(),
            available=False,
            internal=True,
            unavailable_reason=self.unavailable_reason,
        )


INTERNAL_TOOLS: tuple[InternalToolSpec, ...] = (
    InternalToolSpec(
        kind=MEETING_LEAVE_KIND,
        one_liner=(
            "Leave this meeting for good when asked to go — your acknowledgment "
            "is the goodbye; the bot disconnects and stays out of this meeting."
        ),
        keywords=(
            "leave",
            "leave the meeting",
            "go away",
            "get out",
            "drop off",
            "disconnect",
            "goodbye",
            "dismissed",
        ),
        meeting_only=True,
        unavailable_reason=(
            "this session isn't connected to a meeting, so there's no meeting "
            "to leave — ask me to end the session if you want to stop here."
        ),
    ),
    InternalToolSpec(
        kind=SESSION_END_KIND,
        one_liner=(
            "End this voice session when asked to stop or wrap up — your "
            "acknowledgment is the sign-off; the session shuts down cleanly."
        ),
        keywords=(
            "end the session",
            "end session",
            "stop the session",
            "shut down",
            "wrap up",
            "log off",
        ),
    ),
)
"""The v1 internal-tool registry. Extending it (mute, set-status,
schedule-followup, …) means: add a spec here, add its runner branch in
:func:`build_internal_task_executor` — catalog rendering, surface scoping,
the skill-executor locality guard, and the catalog-shadow merge all follow
from :data:`INTERNAL_TOOL_KINDS` / this tuple automatically."""


def internal_catalog_entries(*, meeting_backed: bool) -> tuple[TaskCatalogEntry, ...]:
    """The internal kinds as this session's catalog entries, availability-scoped.

    ``meeting_backed`` is the surface predicate the assembly derives from the
    job payload (``calendar_event_id`` set ⇒ a Meet session linked to a
    calendar occurrence). Off its surface a ``meeting_only`` tool renders as
    an *unavailable* entry (Johnny-trt.55) rather than dropping out: the
    router learns the kind exists, that delegating it is impossible here,
    and the spoken-form reason to decline with — and the gate backstop
    degrades any delegate verdict that targets it anyway.
    """
    return tuple(
        spec.catalog_entry(available=meeting_backed or not spec.meeting_only)
        for spec in INTERNAL_TOOLS
    )


def executor_known_kinds(
    skill_kinds: Iterable[str] = (), mcp_kinds: Iterable[str] = ()
) -> frozenset[str]:
    """The kinds the executor chain can actually resolve (Johnny-trt.62).

    Internal tools + the skills volume (``SkillRegistry.kinds()`` — any
    eligibility, since broken skills still settle honestly with
    skill-specific copy) + the MCP servers' cached, filter-surviving
    qualified tools (Johnny-trt.36 — probe-failed servers stay in: their
    catalog entries render unavailable, so the gate degrades to the spoken
    decline, and the worker still attempts the lazy reconnect). This set is
    the membership truth the gate's pre-ack kind validation checks delegate
    verdicts against — the rendered catalog is only its spoken projection,
    so a kind the render missed but the executor can run still delegates.
    """
    return INTERNAL_TOOL_KINDS | frozenset(skill_kinds) | frozenset(mcp_kinds)


def merge_task_catalog(
    internal: tuple[TaskCatalogEntry, ...],
    skills: tuple[TaskCatalogEntry, ...],
    mcp: tuple[TaskCatalogEntry, ...] = (),
) -> tuple[TaskCatalogEntry, ...]:
    """Compose the session catalog in resolution order: internal → skills → mcp.

    An entry whose kind collides with an internal kind is dropped — the
    internal executor resolves first anyway (a volume skill can never run for
    an internal kind), so advertising the shadowed duplicate would only teach
    the router a lie about where the work happens. The same earlier-source-
    wins rule applies between skills and MCP (the worker dispatches skills
    before the MCP leg, Johnny-trt.24): a duplicate kind keeps the entry of
    the source that will actually run it.
    """
    merged = list(internal)
    seen = {entry.kind for entry in internal}
    for entry in (*skills, *mcp):
        if is_internal_kind(entry.kind):
            logger.warning(
                "internal tools: catalog entry %r shadows an internal kind — dropped",
                entry.kind,
            )
            continue
        if entry.kind in seen:
            logger.warning(
                "internal tools: duplicate catalog kind %r — keeping the "
                "earlier (resolution-order) entry",
                entry.kind,
            )
            continue
        merged.append(entry)
        seen.add(entry.kind)
    return tuple(merged)


WaitForFarewell = Callable[[], Awaitable[None]]
"""Await "the farewell ack finished playing" (bounded, never raising) —
:meth:`RouterGate.wait_recent_say_done` in production, attached after the gate
exists (the ``attach_say`` ordering pattern)."""


@dataclass(slots=True)
class InternalToolContext:
    """Session-local seams the internal runners act through.

    Built by :func:`johnny.agent.job_session.build_agent_runtime` for every
    delegation-capable assembly; carried on the runtime so teardown can close
    the owned HTTP client. ``calendar_event_id`` is the Meet linkage
    (``None`` on the playground — the surface predicate);
    ``wait_for_farewell`` arrives post-construction via
    :meth:`attach_farewell_wait` because the gate (which owns say()) is built
    after the coordinator/executor chain.

    ``teardown_began`` is the double-ask latch: once a leave/end actually
    started, a second ask settles ``done`` ("already on my way out") without
    a second control call; a *failed* teardown clears it so a retry-ask works.
    """

    bot_session_id: int
    calendar_event_id: int | None = None
    api_base_url: str = field(default_factory=api_base_url_from_env)
    wait_for_farewell: WaitForFarewell | None = None
    http_client: httpx.AsyncClient | None = None
    teardown_began: bool = False

    @property
    def meeting_backed(self) -> bool:
        return self.calendar_event_id is not None

    def attach_farewell_wait(self, wait: WaitForFarewell) -> None:
        """Attach the farewell-completion seam once the gate exists."""
        self.wait_for_farewell = wait

    def _http(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(HTTP_TIMEOUT_S, connect=5.0)
            )
        return self.http_client

    async def post(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        """POST one in-app control call against the api base URL."""
        url = f"{self.api_base_url}{path}"
        return await self._http().post(url, json=json)

    async def aclose(self) -> None:
        if self.http_client is not None:
            try:
                await self.http_client.aclose()
            finally:
                self.http_client = None


def _result_json(kind: str, path: str, status_code: int | None) -> dict[str, Any]:
    return {"kind": kind, "endpoint": path, "status_code": status_code}


def build_internal_task_executor(
    context: InternalToolContext,
    *,
    fallback: TaskExecutor,
) -> TaskExecutor:
    """The head of the session executor chain: internal kinds in-process, rest → ``fallback``.

    ``fallback`` is the Johnny-trt.23 skill executor (itself falling through
    to the Phase-3 stub), so the chain implements the documented resolution
    order internal → skills → fail-fast. Internal runners never raise — every
    leg settles ``done``/``failed`` with speech-ready text, the no-dead-
    promises contract.
    """

    async def _execute(task: QueuedTask) -> TaskResult:
        kind = task.spec.kind
        if not is_internal_kind(kind):
            return await fallback(task)
        if kind == SESSION_END_KIND:
            return await _run_session_end(context, task)
        if kind == MEETING_LEAVE_KIND:
            if not context.meeting_backed:
                # Backstop only: off the Meet surface the catalog carries
                # meeting.leave as unavailable and the gate degrades delegate
                # verdicts targeting it (Johnny-trt.55), so reaching here
                # means a hallucinated kind or a hand-queued row — settle
                # honestly.
                return TaskResult(
                    status="failed",
                    result_text=(
                        "I'm not in a meeting right now, so there's nothing to "
                        "leave. Ask me to end the session if you want to stop here."
                    ),
                    error="meeting.leave unavailable: session is not meeting-backed",
                )
            return await _run_meeting_leave(context, task)
        # A kind in INTERNAL_TOOL_KINDS with no runner branch is a registry bug.
        return TaskResult(  # pragma: no cover - defensive
            status="failed",
            result_text=f"I can't run the {kind} action yet.",
            error=f"internal kind {kind!r} has no runner",
        )

    return _execute


async def _wait_farewell(context: InternalToolContext) -> None:
    """Let the farewell finish playing; bounded and contained (never raises)."""
    wait = context.wait_for_farewell
    if wait is None:
        return
    try:
        await asyncio.wait_for(wait(), timeout=FAREWELL_WAIT_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "internal tools: farewell wait timed out after %.0fs — proceeding "
            "with the teardown",
            FAREWELL_WAIT_TIMEOUT_S,
        )
    except Exception:
        logger.exception("internal tools: farewell wait failed — proceeding")


async def _run_meeting_leave(context: InternalToolContext, task: QueuedTask) -> TaskResult:
    """Farewell → dismiss (actor=voice) → disconnect, all via the trt.56 endpoint.

    The bot-dismissal endpoint stamps the durable dismissal FIRST and then
    stops every active session for the meeting (this one), so the scheduler
    will not re-join this occurrence (Johnny-trt.56) and the disconnect is the
    very same launcher path the "End for this meeting" button drives —
    including the Johnny-ajc verification: 502 means the dismissal landed but
    nothing could be stopped, i.e. the bot is honestly still connected, and
    the failed settle speaks exactly that (the trt.53 correction is audible
    precisely because the bot did not leave).
    """
    kind = task.spec.kind
    if context.teardown_began:
        return TaskResult(
            status="done",
            result_text="I'm already on my way out of the meeting.",
            result_json=_result_json(kind, "", None),
        )
    context.teardown_began = True
    path = f"/calendar/events/{context.calendar_event_id}/meeting-config/bot-dismissal"
    await _wait_farewell(context)
    try:
        response = await context.post(path, json={"dismissed_by": "voice"})
    except httpx.HTTPError as exc:
        context.teardown_began = False
        logger.exception("internal tools: meeting.leave control call failed")
        return TaskResult(
            status="failed",
            result_text=(
                "I couldn't reach my own controls to leave the meeting, so I'm "
                "still here. Please remove me with the End-for-this-meeting button."
            ),
            result_json=_result_json(kind, path, None),
            error=f"meeting.leave control call failed: {type(exc).__name__}: {exc}",
        )

    if response.is_success:
        logger.info(
            "internal tools: meeting.leave dismissed event=%s session=%s (voice)",
            context.calendar_event_id,
            context.bot_session_id,
        )
        return TaskResult(
            status="done",
            result_text=(
                "I've left the meeting and won't rejoin this one. See you next time."
            ),
            result_json=_result_json(kind, path, response.status_code),
        )

    context.teardown_began = False
    detail = _response_detail(response)
    logger.error(
        "internal tools: meeting.leave dismissal returned HTTP %s: %s",
        response.status_code,
        detail,
    )
    if response.status_code == 502:
        # Johnny-ajc semantics surfaced to the ear: the durable dismissal
        # landed (no re-join) but the disconnect verifiably did not happen.
        text = (
            "I couldn't actually disconnect from the meeting — I've marked it "
            "as finished so I won't be re-invited, but you may need to remove "
            "me manually."
        )
    else:
        text = "Leaving the meeting didn't work, so I'm still here."
    return TaskResult(
        status="failed",
        result_text=text,
        result_json=_result_json(kind, path, response.status_code),
        error=f"meeting.leave dismissal HTTP {response.status_code}: {detail}",
    )


async def _run_session_end(context: InternalToolContext, task: QueuedTask) -> TaskResult:
    """Farewell → stop this session via the same endpoint as the "Leave now" button.

    No meeting state is touched — on a Meet surface the scheduler may rejoin
    while the occurrence window is open (by design, Johnny-trt.56; "leave and
    stay gone" is ``meeting.leave``). The endpoint routes browser sessions to
    the in-process runner stop and Meet sessions through the container
    launcher, with the Johnny-ajc verification on the launcher path.
    """
    kind = task.spec.kind
    if context.teardown_began:
        return TaskResult(
            status="done",
            result_text="I'm already shutting this session down.",
            result_json=_result_json(kind, "", None),
        )
    context.teardown_began = True
    path = f"/sessions/{context.bot_session_id}/stop"
    await _wait_farewell(context)
    try:
        response = await context.post(path)
    except httpx.HTTPError as exc:
        context.teardown_began = False
        logger.exception("internal tools: session.end control call failed")
        return TaskResult(
            status="failed",
            result_text=(
                "I couldn't reach my own controls to end the session — you may "
                "need to stop it from the dashboard."
            ),
            result_json=_result_json(kind, path, None),
            error=f"session.end control call failed: {type(exc).__name__}: {exc}",
        )

    if response.is_success:
        logger.info(
            "internal tools: session.end stopped session=%s (voice)",
            context.bot_session_id,
        )
        return TaskResult(
            status="done",
            result_text="Session ended. Talk to you later.",
            result_json=_result_json(kind, path, response.status_code),
        )

    context.teardown_began = False
    detail = _response_detail(response)
    logger.error(
        "internal tools: session.end stop returned HTTP %s: %s",
        response.status_code,
        detail,
    )
    return TaskResult(
        status="failed",
        result_text=(
            "I couldn't shut this session down — you may need to stop it from "
            "the dashboard."
        ),
        result_json=_result_json(kind, path, response.status_code),
        error=f"session.end stop HTTP {response.status_code}: {detail}",
    )


def _response_detail(response: httpx.Response) -> str:
    """A short diagnostic string off an error response (never raises)."""
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])[:500]
    except ValueError:
        pass
    return (response.text or "")[:500]


__all__ = [
    "API_BASE_URL_ENV",
    "DEFAULT_API_BASE_URL",
    "FAREWELL_WAIT_TIMEOUT_S",
    "INTERNAL_TOOLS",
    "INTERNAL_TOOL_KINDS",
    "MEETING_LEAVE_KIND",
    "SESSION_END_KIND",
    "InternalToolContext",
    "InternalToolSpec",
    "api_base_url_from_env",
    "build_internal_task_executor",
    "executor_known_kinds",
    "internal_catalog_entries",
    "is_internal_kind",
    "merge_task_catalog",
]
