"""Unit tests for the pure trace projector (US-005, PRD §6.3).

``build_session_trace_view`` takes already-loaded ORM rows and emits the
camelCase ``SessionTraceView`` — no database access — so these tests construct
transient (un-flushed) ORM instances and assert the cross-links + serialization
directly. This is the contract the frontend ``buildSessionTraceView()`` (US-102)
mirrors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.models import (
    AgentDecision,
    AgentModelCall,
    AgentTask,
    AgentTaskStatus,
    AgentToolCall,
    AgentUtterance,
    AgentWorkstream,
    AgentWorkstreamEvent,
    BotMode,
    ConversationEvent,
    DecisionOutcome,
    TerminalState,
    WorkstreamDeliveryStatus,
    WorkstreamSourceKind,
    WorkstreamStatus,
)
from app.services.session_trace import build_session_trace_view

_T0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _at(seconds: int) -> datetime:
    return _T0 + timedelta(seconds=seconds)


def _scenario() -> dict[str, list[object]]:
    """A two-turn session: turn 1 speaks a reply; turn 2 delegates a workstream
    whose result is delivered off-turn by a later utterance."""
    decisions = [
        AgentDecision(
            id=1,
            bot_session_id=1,
            should_speak=True,
            confidence=0.91,
            reason="direct answer",
            reply_type="answer",
            turn_id=1,
            request_id="req-1",
            terminal_state=TerminalState.REPLIED,
            no_reply_reason=None,
            outcome=DecisionOutcome.SPOKEN,
            created_at=_at(0),
        ),
        AgentDecision(
            id=2,
            bot_session_id=1,
            should_speak=True,
            confidence=0.74,
            reason="needs a data lookup",
            reply_type="delegate",
            turn_id=2,
            request_id="req-2",
            terminal_state=TerminalState.REPLIED,
            no_reply_reason=None,
            outcome=DecisionOutcome.SPOKEN,
            created_at=_at(10),
        ),
    ]
    utterances = [
        AgentUtterance(
            id=10,
            bot_session_id=1,
            agent_decision_id=1,
            answers_request_id="req-1",
            mode=BotMode.APPROVAL_REQUIRED,
            output_text="It is sunny.",
            audio_file="reply-10.wav",
            audio_duration_ms=1200,
            interrupted=False,
            created_at=_at(2),
        ),
        # Off-turn task result: no decision link, delivered by workstream 200.
        AgentUtterance(
            id=11,
            bot_session_id=1,
            agent_decision_id=None,
            answers_request_id="req-2",
            mode=BotMode.APPROVAL_REQUIRED,
            output_text="Found 155 CO2 orders.",
            audio_file=None,
            audio_duration_ms=None,
            interrupted=False,
            created_at=_at(30),
        ),
    ]
    tasks = [
        AgentTask(
            id=100,
            bot_session_id=1,
            agent_decision_id=2,
            turn_id=2,
            request_id="req-2",
            kind="metabase",
            status=AgentTaskStatus.DONE,
            ack_text="On it.",
            result_text="155 orders.",
            created_at=_at(10),
            updated_at=_at(30),
        )
    ]
    tool_calls = [
        AgentToolCall(
            id=400,
            bot_session_id=1,
            agent_task_id=100,
            turn_id=2,
            tool_name="execute_query",
            ok=True,
            timed_out=False,
            truncated=False,
            denied=False,
            request_json={},
            created_at=_at(12),
        )
    ]
    model_calls = [
        AgentModelCall(
            id=500,
            bot_session_id=1,
            turn_id=1,
            role="router",
            step_index=0,
            model_provider="openai",
            model_name="gpt-5.5",
            prompt_tokens=100,
            completion_tokens=8,
            total_tokens=108,
            duration_ms=45,
            finish_reason="stop",
            created_at=_at(0),
        ),
        AgentModelCall(
            id=501,
            bot_session_id=1,
            turn_id=2,
            role="router",
            step_index=0,
            model_name="gpt-5.5",
            duration_ms=52,
            created_at=_at(10),
        ),
        # Answer-loop call on turn 2 → counts toward the workstream's models.
        AgentModelCall(
            id=502,
            bot_session_id=1,
            turn_id=2,
            role="answer",
            step_index=0,
            model_name="gpt-5.5",
            created_at=_at(12),
        ),
    ]
    workstreams = [
        AgentWorkstream(
            id=200,
            bot_session_id=1,
            source_kind=WorkstreamSourceKind.DELEGATE,
            source_turn_id=2,
            source_decision_id=2,
            agent_task_id=100,
            request_id="req-2",
            title="metabase",
            user_request_text="how many CO2 orders?",
            status=WorkstreamStatus.DONE,
            delivery_status=WorkstreamDeliveryStatus.DELIVERED,
            started_at=_at(11),
            completed_at=_at(28),
            delivered_at=_at(30),
            result_available_at=_at(28),
            delivered_utterance_id=11,
            result_text="155 orders.",
            result_json={"count": 155},
            created_at=_at(10),
        )
    ]
    # Passed out of order to prove the projector sorts by sequence.
    workstream_events = [
        AgentWorkstreamEvent(
            id=302,
            workstream_id=200,
            bot_session_id=1,
            sequence=3,
            event_type="done",
            created_at=_at(28),
        ),
        AgentWorkstreamEvent(
            id=300,
            workstream_id=200,
            bot_session_id=1,
            sequence=1,
            event_type="queued",
            created_at=_at(10),
        ),
        AgentWorkstreamEvent(
            id=301,
            workstream_id=200,
            bot_session_id=1,
            sequence=2,
            event_type="running",
            created_at=_at(11),
        ),
    ]
    conversation_events = [
        ConversationEvent(
            id=600,
            bot_session_id=1,
            event_type="interruption_recorded",
            timestamp_ms=5000,
            turn_id=1,
            duration_ms=120,
            reason="user_over_bot",
            details={"speech_kind": "reply"},
            created_at=_at(5),
        )
    ]
    return {
        # Decisions/events deliberately shuffled to prove deterministic ordering.
        "decisions": [decisions[1], decisions[0]],
        "utterances": utterances,
        "tasks": tasks,
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "workstreams": workstreams,
        "workstream_events": workstream_events,
        "conversation_events": conversation_events,
    }


def test_router_turns_projection_and_links() -> None:
    view = build_session_trace_view(**_scenario())  # type: ignore[arg-type]

    assert [t.decision_id for t in view.router_turns] == [1, 2]  # chronological

    speak, delegate = view.router_turns
    assert speak.action == "speak"
    assert speak.request_id == "req-1"
    assert speak.delivery_ids == [10]
    assert speak.workstream_ids == []
    assert speak.router_model_call is not None
    assert speak.router_model_call.model_name == "gpt-5.5"
    assert speak.router_model_call.duration_ms == 45
    assert speak.router_model_call.total_tokens == 108

    assert delegate.action == "delegate"  # a workstream shares the turn
    assert delegate.workstream_ids == [200]
    assert delegate.delivery_ids == []  # off-turn result is not decision-linked
    assert delegate.router_model_call is not None
    assert delegate.router_model_call.id == 501


def test_deliveries_kind_and_request_linkage() -> None:
    view = build_session_trace_view(**_scenario())  # type: ignore[arg-type]

    by_id = {d.utterance_id: d for d in view.deliveries}
    assert [d.utterance_id for d in view.deliveries] == [10, 11]  # chronological

    reply = by_id[10]
    assert reply.delivery_kind == "reply"
    assert reply.answers_request_id == "req-1"
    assert reply.turn_id == 1
    assert reply.decision_id == 1
    assert reply.source_workstream_id is None
    assert reply.mode == "approval_required"
    assert reply.audio_file == "reply-10.wav"

    task_result = by_id[11]
    assert task_result.delivery_kind == "task_result"
    assert task_result.answers_request_id == "req-2"
    assert task_result.turn_id is None  # fallback/off-turn speech, no decision
    assert task_result.decision_id is None
    assert task_result.source_workstream_id == 200


def test_workstream_projection_counts_and_events() -> None:
    view = build_session_trace_view(**_scenario())  # type: ignore[arg-type]

    assert len(view.workstreams) == 1
    ws = view.workstreams[0]
    assert ws.id == 200
    assert ws.source_kind == "delegate"
    assert ws.status == "done"
    assert ws.delivery_status == "delivered"
    assert ws.task_kind == "metabase"
    assert ws.task_status == "done"
    assert ws.ack_text == "On it."
    assert ws.tool_call_count == 1
    assert ws.model_call_count == 1  # the answer-loop call on turn 2
    assert ws.delivered_utterance_id == 11
    assert ws.result_json == {"count": 155}
    assert [e.event_type for e in ws.events] == ["queued", "running", "done"]


def test_activity_projection() -> None:
    view = build_session_trace_view(**_scenario())  # type: ignore[arg-type]

    assert len(view.activity) == 1
    event = view.activity[0]
    assert event.event_type == "interruption_recorded"
    assert event.timestamp_ms == 5000
    assert event.reason == "user_over_bot"
    assert event.details == {"speech_kind": "reply"}


def test_serialises_camelcase_by_alias() -> None:
    view = build_session_trace_view(**_scenario())  # type: ignore[arg-type]
    dumped = view.model_dump(by_alias=True)

    assert set(dumped) == {"routerTurns", "deliveries", "workstreams", "activity"}
    assert "routerModelCall" in dumped["routerTurns"][0]
    assert "deliveryIds" in dumped["routerTurns"][0]
    assert "workstreamIds" in dumped["routerTurns"][0]
    assert "deliveryKind" in dumped["deliveries"][0]
    assert "answersRequestId" in dumped["deliveries"][0]
    ws = dumped["workstreams"][0]
    assert ws["deliveryStatus"] == "delivered"
    assert ws["toolCallCount"] == 1
    assert ws["modelCallCount"] == 1


def test_empty_session_yields_empty_arrays() -> None:
    view = build_session_trace_view(
        decisions=[],
        utterances=[],
        tasks=[],
        tool_calls=[],
        model_calls=[],
        workstreams=[],
        workstream_events=[],
        conversation_events=[],
    )
    assert view.router_turns == []
    assert view.deliveries == []
    assert view.workstreams == []
    assert view.activity == []
