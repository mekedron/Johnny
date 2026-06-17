"""Server-side projection of one session's persisted rows into the three-column
trace view (US-005, PRD §6.3).

:func:`build_session_trace_view` is a **pure** function over already-loaded ORM
rows — it performs no database access, so it is unit-testable in isolation and
gives the frontend ``buildSessionTraceView()`` (US-102) a concrete contract to
mirror. The output models are **camelCase on the wire** (PRD §6.3 /
``RED-TEAM-REVIEW`` §R5: ``SessionTraceView { routerTurns, deliveries,
workstreams, activity }``), unlike the snake_case ``/sessions/{id}`` +
``/history/{id}`` detail payloads which keep serving during migration.

The projection reuses, never forks, the shipped substrate: ``request_id``
correlation (US-003), the ``agent_workstreams`` envelope (US-002) and its
``role='router'`` model call (US-004). Cross-links are computed in memory:
delivery → which request it answered, router-turn → its delivery/workstream
ids, and each workstream → its task + tool/model-call counts + progress events.

Out of scope here (later stories): per-stage timing durations on router turns
(US-104 Decisions column); inline-workstream synthesis for legacy sessions
(US-107); precise ``delivery_kind`` classification once
``agent_utterances.delivery_kind`` lands (US-105/US-301) — for now it is derived
best-effort (``task_result`` when the utterance delivered a workstream result,
else ``reply``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.db.models import (
    AgentDecision,
    AgentModelCall,
    AgentTask,
    AgentToolCall,
    AgentUtterance,
    AgentWorkstream,
    AgentWorkstreamEvent,
    ConversationEvent,
)


class _CamelModel(BaseModel):
    """camelCase-on-the-wire base for the trace projection models.

    Instances are constructed by field name (snake_case) in the projector;
    FastAPI serialises by alias (camelCase) via the default
    ``response_model_by_alias=True``.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class RouterModelCallView(_CamelModel):
    """Light summary of a turn's ``role='router'`` model call (US-004).

    The raw router prompt/response already live on ``agent_decisions``
    (``input_window`` / ``raw_output``) and in the detail payload's full
    ``model_calls`` list; this carries only the headline cost so the Decisions
    column can render "router: <model>, <tokens>, <ms>" without a disclosure.
    """

    id: int
    model_provider: str | None
    model_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    time_to_first_token_ms: int | None
    duration_ms: int | None
    finish_reason: str | None


class RouterTurnView(_CamelModel):
    """One router decision, with links to what it produced (PRD §7 Router view)."""

    decision_id: int
    turn_id: int | None
    request_id: str | None
    # Participant attribution (US-401): who asked. NULL → "Unknown speaker".
    requested_by: str | None
    created_at: datetime
    # Best-effort action chip: ``delegate`` when the turn spun up a workstream,
    # else ``speak`` / ``silent`` from ``should_speak``. ``status`` is not
    # distinctly derivable in v1 (no router-action column) — the raw fields
    # below are passed through so US-102/US-104 can refine without a re-pull.
    action: str
    should_speak: bool
    confidence: float
    reason: str
    reply_type: str | None
    outcome: str
    terminal_state: str | None
    no_reply_reason: str | None
    router_model_call: RouterModelCallView | None
    delivery_ids: list[int]
    workstream_ids: list[int]


class DeliveryView(_CamelModel):
    """One thing the bot said, back-linked to the request it answered (PRD §7)."""

    utterance_id: int
    created_at: datetime
    turn_id: int | None
    decision_id: int | None
    answers_request_id: str | None
    # Participant attribution (US-401): the participant this delivery answered.
    requested_by: str | None
    delivery_kind: str
    final_text: str
    interrupted: bool
    mode: str
    audio_file: str | None
    audio_duration_ms: int | None
    source_workstream_id: int | None
    # US-105 drill-through. ``prompt`` is the answer-LLM prompt that produced
    # this delivery (migrated off the legacy per-turn timeline). The divergence
    # trio is pulled from the linked decision (INV-2): what the router
    # recommended vs ``final_text`` (the utterance's ``output_text``), and why /
    # who rewrote it. ``status_read_workstream_ids`` is, for a ``status``
    # delivery only, the workstreams it read — empty otherwise (PRD §7, AC#3).
    prompt: str
    decision_recommended_text: str | None
    divergence_reason: str | None
    override_actor: str | None
    status_read_workstream_ids: list[int]


class WorkstreamEventView(_CamelModel):
    """One append-only progress/audit row on a workstream (US-002)."""

    id: int
    sequence: int
    event_type: str
    text: str | None
    payload_json: Any | None
    created_at: datetime


class WorkstreamView(_CamelModel):
    """One unit of work as its own thread (PRD §7 Workstreams view)."""

    id: int
    source_kind: str
    source_turn_id: int | None
    source_decision_id: int | None
    agent_task_id: int | None
    request_id: str | None
    # Participant attribution (US-401): who requested this workstream.
    requested_by: str | None
    title: str | None
    user_request_text: str | None
    status: str
    delivery_status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    delivered_at: datetime | None
    result_available_at: datetime | None
    result_expires_at: datetime | None
    expired_reason: str | None
    delivered_utterance_id: int | None
    result_text: str | None
    result_json: Any | None
    error: str | None
    # Derived from the backing ``agent_tasks`` row (delegated only).
    task_kind: str | None
    task_status: str | None
    ack_text: str | None
    tool_call_count: int
    model_call_count: int
    events: list[WorkstreamEventView]


class ActivityEventView(_CamelModel):
    """One conversation-dynamics event: interruption / floor / turn-claim."""

    id: int
    event_type: str
    timestamp_ms: int
    turn_id: int | None
    agent_name: str | None
    counterpart_name: str | None
    duration_ms: int | None
    reason: str
    details: dict[str, Any]


class SessionTraceView(_CamelModel):
    """The three-column projection (PRD §6.3), consumed by live + history."""

    router_turns: list[RouterTurnView]
    deliveries: list[DeliveryView]
    workstreams: list[WorkstreamView]
    activity: list[ActivityEventView]


def _status_read_workstream_ids(
    utterance: AgentUtterance,
    workstreams: Sequence[AgentWorkstream],
) -> list[int]:
    """Workstreams a ``status`` delivery read (US-105 AC#3).

    The durable proxy for "what the status should have reported": every
    workstream that existed and was not yet delivered as of the status
    delivery's timestamp, plus any whose result this very utterance delivered.
    Rendering it beside the spoken text exposes session-3's bug — a status that
    read **zero** workstreams while inline work was in flight. Called only for
    ``status`` deliveries; the read model carries ``[]`` for every other kind.

    Mirrors ``buildSessionTraceView`` in ``frontend/src/lib/sessionTrace.ts`` —
    keep the two derivations identical.
    """

    when = utterance.created_at
    ids: set[int] = set()
    for ws in workstreams:
        if ws.delivered_utterance_id == utterance.id:
            ids.add(ws.id)
        elif ws.created_at <= when and (
            ws.delivered_at is None or ws.delivered_at >= when
        ):
            ids.add(ws.id)
    return sorted(ids)


def _enum_value(value: Any) -> Any:
    """Return ``value.value`` for (Str)Enum members, else ``value`` unchanged.

    ``None`` passes through. Keeps the wire payload free of ``Enum`` reprs so a
    field typed ``str`` always serialises to its bare value.
    """

    return getattr(value, "value", value)


def build_session_trace_view(
    *,
    decisions: Sequence[AgentDecision],
    utterances: Sequence[AgentUtterance],
    tasks: Sequence[AgentTask],
    tool_calls: Sequence[AgentToolCall],
    model_calls: Sequence[AgentModelCall],
    workstreams: Sequence[AgentWorkstream],
    workstream_events: Sequence[AgentWorkstreamEvent],
    conversation_events: Sequence[ConversationEvent],
) -> SessionTraceView:
    """Project one session's loaded rows into the three-column trace view.

    Pure and order-independent: every output array is sorted deterministically
    (turns/deliveries/workstreams chronologically by ``created_at`` then id;
    activity by ``timestamp_ms`` then id), so callers may load rows in any order.
    """

    # Router vs answer model calls keyed by turn (US-004 added the router rows).
    router_call_by_turn: dict[int, AgentModelCall] = {}
    answer_calls_by_turn: dict[int, int] = defaultdict(int)
    for mc in model_calls:
        if mc.turn_id is None:
            continue
        if mc.role == "router":
            router_call_by_turn.setdefault(mc.turn_id, mc)
        elif mc.role == "answer":
            answer_calls_by_turn[mc.turn_id] += 1

    # Tool-call counts per delegated task (the workstream's execution row).
    tool_calls_by_task: dict[int, int] = defaultdict(int)
    for tc in tool_calls:
        if tc.agent_task_id is not None:
            tool_calls_by_task[tc.agent_task_id] += 1

    task_by_id: dict[int, AgentTask] = {t.id: t for t in tasks}

    events_by_ws: dict[int, list[AgentWorkstreamEvent]] = defaultdict(list)
    for ev in workstream_events:
        events_by_ws[ev.workstream_id].append(ev)

    # Workstream links back to the delegating decision/turn, and forward to the
    # utterance that delivered its result.
    ws_ids_by_decision: dict[int, list[int]] = defaultdict(list)
    ws_ids_by_turn: dict[int, list[int]] = defaultdict(list)
    ws_by_delivered_utterance: dict[int, int] = {}
    for ws in workstreams:
        if ws.source_decision_id is not None:
            ws_ids_by_decision[ws.source_decision_id].append(ws.id)
        if ws.source_turn_id is not None:
            ws_ids_by_turn[ws.source_turn_id].append(ws.id)
        if ws.delivered_utterance_id is not None:
            ws_by_delivered_utterance[ws.delivered_utterance_id] = ws.id

    utt_ids_by_decision: dict[int, list[int]] = defaultdict(list)
    for u in utterances:
        if u.agent_decision_id is not None:
            utt_ids_by_decision[u.agent_decision_id].append(u.id)

    decision_by_id: dict[int, AgentDecision] = {d.id: d for d in decisions}

    # --- routerTurns --------------------------------------------------------
    router_turns: list[RouterTurnView] = []
    for d in decisions:
        workstream_ids = list(ws_ids_by_decision.get(d.id, []))
        if not workstream_ids and d.turn_id is not None:
            workstream_ids = list(ws_ids_by_turn.get(d.turn_id, []))
        if workstream_ids:
            action = "delegate"
        elif d.should_speak:
            action = "speak"
        else:
            action = "silent"
        rc = (
            router_call_by_turn.get(d.turn_id)
            if d.turn_id is not None
            else None
        )
        router_turns.append(
            RouterTurnView(
                decision_id=d.id,
                turn_id=d.turn_id,
                request_id=d.request_id,
                requested_by=d.requested_by,
                created_at=d.created_at,
                action=action,
                should_speak=d.should_speak,
                confidence=d.confidence,
                reason=d.reason,
                reply_type=d.reply_type,
                outcome=_enum_value(d.outcome),
                terminal_state=_enum_value(d.terminal_state),
                no_reply_reason=_enum_value(d.no_reply_reason),
                router_model_call=(
                    RouterModelCallView(
                        id=rc.id,
                        model_provider=rc.model_provider,
                        model_name=rc.model_name,
                        prompt_tokens=rc.prompt_tokens,
                        completion_tokens=rc.completion_tokens,
                        total_tokens=rc.total_tokens,
                        time_to_first_token_ms=rc.time_to_first_token_ms,
                        duration_ms=rc.duration_ms,
                        finish_reason=rc.finish_reason,
                    )
                    if rc is not None
                    else None
                ),
                delivery_ids=sorted(utt_ids_by_decision.get(d.id, [])),
                workstream_ids=sorted(workstream_ids),
            )
        )
    router_turns.sort(key=lambda r: (r.created_at, r.decision_id))

    # --- deliveries ---------------------------------------------------------
    deliveries: list[DeliveryView] = []
    for u in utterances:
        decision = (
            decision_by_id.get(u.agent_decision_id)
            if u.agent_decision_id is not None
            else None
        )
        source_ws = ws_by_delivered_utterance.get(u.id)
        # Persisted ``AgentSpoke.kind`` (US-105) is authoritative; rows written
        # before the column existed fall back to the old best-effort derivation.
        delivery_kind = u.delivery_kind or (
            "task_result" if source_ws is not None else "reply"
        )
        deliveries.append(
            DeliveryView(
                utterance_id=u.id,
                created_at=u.created_at,
                turn_id=decision.turn_id if decision is not None else None,
                decision_id=u.agent_decision_id,
                answers_request_id=u.answers_request_id,
                requested_by=u.requested_by,
                delivery_kind=delivery_kind,
                final_text=u.output_text,
                interrupted=bool(u.interrupted),
                mode=_enum_value(u.mode),
                audio_file=u.audio_file,
                audio_duration_ms=u.audio_duration_ms,
                source_workstream_id=source_ws,
                prompt=u.prompt or "",
                decision_recommended_text=(
                    decision.decision_recommended_text if decision is not None else None
                ),
                divergence_reason=(
                    decision.divergence_reason if decision is not None else None
                ),
                override_actor=(
                    decision.override_actor if decision is not None else None
                ),
                status_read_workstream_ids=(
                    _status_read_workstream_ids(u, workstreams)
                    if delivery_kind == "status"
                    else []
                ),
            )
        )
    deliveries.sort(key=lambda x: (x.created_at, x.utterance_id))

    # --- workstreams --------------------------------------------------------
    workstream_views: list[WorkstreamView] = []
    for ws in workstreams:
        task = (
            task_by_id.get(ws.agent_task_id)
            if ws.agent_task_id is not None
            else None
        )
        tool_count = (
            tool_calls_by_task.get(ws.agent_task_id, 0)
            if ws.agent_task_id is not None
            else 0
        )
        model_count = (
            answer_calls_by_turn.get(ws.source_turn_id, 0)
            if ws.source_turn_id is not None
            else 0
        )
        workstream_views.append(
            WorkstreamView(
                id=ws.id,
                source_kind=_enum_value(ws.source_kind),
                source_turn_id=ws.source_turn_id,
                source_decision_id=ws.source_decision_id,
                agent_task_id=ws.agent_task_id,
                request_id=ws.request_id,
                requested_by=ws.requested_by,
                title=ws.title,
                user_request_text=ws.user_request_text,
                status=_enum_value(ws.status),
                delivery_status=_enum_value(ws.delivery_status),
                created_at=ws.created_at,
                started_at=ws.started_at,
                completed_at=ws.completed_at,
                delivered_at=ws.delivered_at,
                result_available_at=ws.result_available_at,
                result_expires_at=ws.result_expires_at,
                expired_reason=ws.expired_reason,
                delivered_utterance_id=ws.delivered_utterance_id,
                result_text=ws.result_text,
                result_json=ws.result_json,
                error=ws.error,
                task_kind=task.kind if task is not None else None,
                task_status=_enum_value(task.status) if task is not None else None,
                ack_text=task.ack_text if task is not None else None,
                tool_call_count=tool_count,
                model_call_count=model_count,
                events=[
                    WorkstreamEventView(
                        id=e.id,
                        sequence=e.sequence,
                        event_type=e.event_type,
                        text=e.text,
                        payload_json=e.payload_json,
                        created_at=e.created_at,
                    )
                    for e in sorted(
                        events_by_ws.get(ws.id, []), key=lambda e: e.sequence
                    )
                ],
            )
        )
    workstream_views.sort(key=lambda w: (w.created_at, w.id))

    # --- activity -----------------------------------------------------------
    activity = [
        ActivityEventView(
            id=e.id,
            event_type=e.event_type,
            timestamp_ms=e.timestamp_ms,
            turn_id=e.turn_id,
            agent_name=e.agent_name,
            counterpart_name=e.counterpart_name,
            duration_ms=e.duration_ms,
            reason=e.reason,
            details=e.details,
        )
        for e in conversation_events
    ]
    activity.sort(key=lambda a: (a.timestamp_ms, a.id))

    return SessionTraceView(
        router_turns=router_turns,
        deliveries=deliveries,
        workstreams=workstream_views,
        activity=activity,
    )
