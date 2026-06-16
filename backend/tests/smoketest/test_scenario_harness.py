"""US-001 scenario harness: a real delegated, multi-speaker session (Johnny-d6w.1).

Drives the committed ``delegated-multispeaker`` fixture through the real gate +
``TaskCoordinator`` + worker claim/settle path and asserts the delegate produced
a genuine ``agent_tasks`` row, the happy-path ``task_*`` events fired, the tool
result is correct, and INV-1/INV-2 hold — deterministically, on SQLite, with no
Redis / MCP SDK / live LLM.

Requires the ``agent`` extra (the engine pulls ``RouterGate``), guarded by
``importorskip`` exactly like ``test_replay_harness_agent.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

pytest.importorskip("livekit.agents")

from app.db import Base  # noqa: E402
from app.db.models import (  # noqa: E402
    Agent,
    AgentModelCall,
    AgentTask,
    AgentWorkstream,
    AgentWorkstreamEvent,
    BotSession,
    CapabilityPolicy,
    Workspace,
)
from johnny.agent.router_gate import BACKGROUND_PROMOTION_KEY  # noqa: E402
from johnny.agent.tasks import (  # noqa: E402
    STATUS_NOTHING_IN_FLIGHT,
    QueuedTask,
    TaskResult,
)
from johnny.skills.executor import TaskProgressReporter  # noqa: E402
from johnny.smoketest.replay import discover_fixtures  # noqa: E402
from johnny.smoketest.scenario import (  # noqa: E402
    load_scenario,
    make_multistep_reverse_executor,
    run_scenario,
)
from johnny.voice_pipeline.events import (  # noqa: E402
    AgentSpoke,
    RouterDecisionMade,
    TaskCompleted,
    TaskProgress,
    TaskQueued,
    TaskResultExpired,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SCENARIO_FIXTURE = _FIXTURES / "scenarios" / "delegated-multispeaker"
PROMOTION_FIXTURE = _FIXTURES / "scenarios" / "delegated-background-promotion"
PROGRESS_FIXTURE = _FIXTURES / "scenarios" / "delegated-progress"
SESSIONS_DIR = _FIXTURES / "sessions"

# A deterministic stand-in for the demo MCP `server_time` tool (real wall-clock is
# non-deterministic; the CI gate is hermetic). The reverse_text kind keeps the
# pure-reversal contract of the default executor.
_SERVER_TIME_RESULT = "2026-06-16T12:00:00Z"


async def _two_kind_executor(
    task: QueuedTask, *, reporter: TaskProgressReporter | None = None
) -> TaskResult:
    """Per-kind deterministic executor for the two-workstream promotion scenario.

    The promoted ``server_time`` task carries empty args (the gate synthesises the
    task_request), so it cannot derive a result from ``args`` like the default
    reverse_text executor — it returns a fixed deterministic timestamp instead.
    """
    kind = task.spec.kind
    if kind == "mcp__demo-http__server_time":
        out = _SERVER_TIME_RESULT
    else:
        out = str(task.spec.args.get("text", ""))[::-1]
    return TaskResult(
        status="done",
        result_text=out,
        result_json={"mcp_server": "demo-http", "mcp_tool": kind, "output": out},
    )


@pytest.fixture
def db() -> Iterator[Session]:
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    # agent_tasks plus the tables the worker-test fixture creates (the claim /
    # settle path touches only agent_tasks; the rest keep FK shape sane).
    Base.metadata.create_all(
        bind=engine,
        tables=[
            AgentModelCall.__table__,  # type: ignore[list-item]
            AgentTask.__table__,  # type: ignore[list-item]
            AgentWorkstream.__table__,  # type: ignore[list-item]
            AgentWorkstreamEvent.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            CapabilityPolicy.__table__,  # type: ignore[list-item]
            Workspace.__table__,  # type: ignore[list-item]
            Agent.__table__,  # type: ignore[list-item]
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def test_scenario_produces_real_delegated_session(db: Session) -> None:
    fixture = load_scenario(SCENARIO_FIXTURE)

    # session-3 shape: ≥2 speakers, ≥1 delegate, ≥1 status, ≥3 requests.
    assert len(fixture.speakers) >= 2
    actions = [t.router.get("action") for t in fixture.turns]
    assert actions.count("delegate") >= 1
    assert actions.count("status") >= 1
    assert actions.count("speak") >= 2  # the dashboards / weather / Q1 requests

    result = asyncio.run(run_scenario(fixture, session=db))

    # INV-1 (one terminal/turn; the delegate ack is the turn terminal, the task
    # result is never a turn terminal) + INV-2 (decision↔utterance parity).
    assert result.invariant_violations == []

    # The happy-path task_* events fired, all on one task_id.
    queued = cast(list[TaskQueued], result.events_of_type("task_queued"))
    progress = cast(list[TaskProgress], result.events_of_type("task_progress"))
    completed = cast(list[TaskCompleted], result.events_of_type("task_completed"))
    expired = cast(
        list[TaskResultExpired], result.events_of_type("task_result_expired")
    )
    assert len(queued) == 1, "delegate must write exactly one queued task"
    assert len(progress) == 1, "the worker claim must emit one TaskProgress"
    assert len(completed) == 1, "the settle must emit one TaskCompleted"
    assert len(expired) == 1, "the undelivered done result must expire (TaskResultExpired)"
    task_id = queued[0].task_id
    assert progress[0].task_id == task_id
    assert completed[0].task_id == task_id
    assert expired[0].task_id == task_id
    assert expired[0].reason, "TaskResultExpired must name its drop reason"
    assert completed[0].status == "done"
    assert queued[0].turn_id == completed[0].turn_id  # same delegating turn

    # A genuine agent_tasks row reached `done` with the expected tool result.
    assert len(result.task_rows) == 1
    row = result.task_rows[0]
    assert row["kind"] == "mcp__demo-http__reverse_text"
    assert row["status"] == "done"

    delegate_turn = next(t for t in fixture.turns if t.router.get("action") == "delegate")
    sent_text = delegate_turn.router["task"]["args"]["text"]
    assert row["result_text"] == sent_text[::-1]  # the deterministic tool output
    assert row["result_json"]["mcp_tool"] == "reverse_text"
    assert row["result_json"]["is_error"] is False

    # US-002: the single durable writer produced exactly one workstream envelope
    # for the delegated task, FK'd to its agent_tasks row, carrying the same
    # terminal execution result. The fixture never voices the result (fake TTS)
    # and lets it expire, so delivery_status settles at `expired`, not delivered.
    assert len(result.workstream_rows) == 1, "one workstream per delegated task"
    ws = result.workstream_rows[0]
    assert ws["agent_task_id"] == task_id  # the envelope FKs to the task row
    assert ws["source_kind"] == "delegate"
    assert ws["status"] == "done"  # execution reached the same terminal as the task
    assert ws["delivery_status"] == "expired"  # undelivered result aged out
    assert ws["result_text"] == sent_text[::-1]  # mirrors the task result
    assert ws["result_json"]["mcp_tool"] == "reverse_text"  # copied from the task row
    assert ws["source_turn_id"] == queued[0].turn_id  # bound to the delegating turn
    assert ws["title"] == "mcp__demo-http__reverse_text"


def test_scenario_status_query_reflects_inflight_workstream(db: Session) -> None:
    """US-203 (AC#3): the progress query fired while the delegated workstream is
    in flight speaks the in-flight task from the registry — never the empty
    "nothing in flight" line — and names the same workstream the column renders
    (the session-3 split-brain, reproduced and fixed)."""
    fixture = load_scenario(SCENARIO_FIXTURE)
    result = asyncio.run(run_scenario(fixture, session=db))

    assert result.invariant_violations == []

    # The turn-3 status verdict spoke exactly one status-kind delivery, rendered
    # from the task registry (run_scenario drives all turns before the worker
    # leg, so the CO2 task is still queued/in-flight at the status turn).
    status_spokes = [
        s
        for s in cast(list[AgentSpoke], result.events_of_type("agent_spoke"))
        if s.kind == "status"
    ]
    assert len(status_spokes) == 1, "the progress query must speak exactly one status"
    spoken = status_spokes[0].text

    # The bug fixed: not the empty-registry line while work runs.
    assert spoken != STATUS_NOTHING_IN_FLIGHT
    assert "don't have any tasks in flight" not in spoken
    # It names the in-flight delegated workstream.
    assert "Still working on the mcp demo http reverse text task" in spoken

    # Parity: the same workstream the Workstreams column renders (the durable
    # agent_workstreams envelope) is the one the status spoke about.
    assert len(result.workstream_rows) == 1
    assert result.workstream_rows[0]["title"] == "mcp__demo-http__reverse_text"


def test_scenario_request_id_correlation(db: Session) -> None:
    """US-003: a stable ``request_id`` minted once per opened turn, propagated to
    the decision, the delivery (``answers_request_id``), and the delegated
    workstream + every one of its task events — the cross-turn correlation key
    the Deliveries column reads. Exercised through the SAME real gate + emitters
    + single durable writer the live system runs.
    """
    fixture = load_scenario(SCENARIO_FIXTURE)
    result = asyncio.run(run_scenario(fixture, session=db))

    decisions = cast(
        list[RouterDecisionMade], result.events_of_type("router_decision_made")
    )
    spokes = cast(list[AgentSpoke], result.events_of_type("agent_spoke"))
    queued = cast(list[TaskQueued], result.events_of_type("task_queued"))
    progress = cast(list[TaskProgress], result.events_of_type("task_progress"))
    completed = cast(list[TaskCompleted], result.events_of_type("task_completed"))

    # v1 mints exactly one id per opened turn — every decision carries one and
    # they are all distinct (the documented one-per-turn semantics, AC#5).
    assert decisions, "the scenario decides at least one turn"
    assert all(d.request_id for d in decisions), "every decision mints a request_id"
    assert len({d.request_id for d in decisions}) == len(decisions)

    # Each turn-bound delivery names the request ITS OWN turn answered (AC#2/#3) —
    # bound by the turn's request_id, not FIFO "oldest pending" (RED-TEAM C8).
    rid_by_turn = {d.turn_id: d.request_id for d in decisions}
    bound_spokes = [s for s in spokes if s.turn_id is not None]
    assert bound_spokes, "the scenario speaks at least one turn-bound delivery"
    for s in bound_spokes:
        assert s.answers_request_id == rid_by_turn.get(s.turn_id)

    # The delegated workstream + EVERY one of its task events carry the
    # delegating turn's id — queued/progress/completed all agree, so the
    # durable envelope is stamped regardless of task-event arrival order (AC#2).
    deleg_turn_id = queued[0].turn_id
    deleg_rid = rid_by_turn[deleg_turn_id]
    assert deleg_rid is not None
    assert queued[0].request_id == deleg_rid
    assert progress[0].request_id == deleg_rid
    assert completed[0].request_id == deleg_rid
    assert len(result.workstream_rows) == 1
    assert result.workstream_rows[0]["request_id"] == deleg_rid


def test_scenario_captures_router_model_call(db: Session) -> None:
    """US-004: every decided turn's router LLM call is captured as a
    ``role='router'`` row in ``agent_model_calls`` — the Decisions-view symmetry
    with the answer side — exercised through the REAL gate + the production
    ``SqlAlchemyModelCallSink``. Verified via SQL that ``agent_model_calls.role``
    returns ``router`` (today the live table is ``answer``-only). The deterministic
    harness binds recorded answers without the answer adapter, so the ``answer``
    half is asserted on the live/generation stack; here we pin the new router half.
    """
    fixture = load_scenario(SCENARIO_FIXTURE)
    result = asyncio.run(run_scenario(fixture, session=db))

    router_rows = db.scalars(
        sa.select(AgentModelCall)
        .where(AgentModelCall.bot_session_id == fixture.bot_session_id)
        .where(AgentModelCall.role == "router")
        .order_by(AgentModelCall.id)
    ).all()

    decisions = cast(
        list[RouterDecisionMade], result.events_of_type("router_decision_made")
    )
    assert decisions, "the scenario decides at least one turn"
    # Exactly one role='router' row per decided turn (silent / speak / delegate),
    # each sharing its decision's durable int turn id.
    assert len(router_rows) == len(decisions)
    decided_turn_ids = {d.turn_id for d in decisions}
    assert {row.turn_id for row in router_rows} == decided_turn_ids

    for row in router_rows:
        assert row.role == "router"
        assert row.turn_id in decided_turn_ids
        assert row.step_index == 0
        assert row.prompt_json, "the router prompt (messages array) is persisted"
        assert row.response_text, "the router's raw response text is persisted"
        assert row.finish_reason == "stop"  # the recorded router LLM's finish_reason
        assert row.model_provider, "the router provider name is recorded"
    # The 8.0 s router budget is untouched: capture only stamps timing/clock reads,
    # so the duration is a real, non-negative span (no assertion on its magnitude —
    # the recorded LLM returns instantly and timestamps are wall-clock).
    assert all(r.duration_ms is not None and r.duration_ms >= 0 for r in router_rows)


def test_scenario_emits_and_persists_per_step_progress(db: Session) -> None:
    """US-202: the executor narrates milestones (step 1..n) through the SAME
    reporter → ``TaskProgress`` → durable-writer seam production runs, and each is
    persisted as an ordered ``agent_workstream_events`` row so an ended session
    can replay "when each step happened". Exercised through the real gate +
    coordinator + worker leg; the multi-step stand-in only supplies the
    ``.report()`` calls (the live skill/MCP executors do that in production).
    """
    fixture = load_scenario(PROGRESS_FIXTURE)
    result = asyncio.run(
        run_scenario(fixture, session=db, executor=make_multistep_reverse_executor())
    )

    # Progress events emit NO turn terminal — INV-1 (one terminal/turn) and INV-2
    # (decision↔utterance parity) still hold with the extra TaskProgress frames.
    assert result.invariant_violations == []

    # The worker's step-0 claim signal + the executor's two milestones, monotonic.
    progress = cast(list[TaskProgress], result.events_of_type("task_progress"))
    assert [p.step for p in progress] == [0, 1, 2]
    assert progress[0].progress_text == "" and progress[0].phase is None  # claim
    assert progress[1].progress_text == "Fetching the input…"
    assert progress[1].phase == "availability_check"
    assert progress[2].progress_text == "Reversing the text…"
    assert progress[2].phase == "run"
    task_id = progress[0].task_id
    assert all(p.task_id == task_id for p in progress)
    assert all(p.turn_id is not None for p in progress)  # the delegating turn

    # The durable progress log: queued → running (the step-0 claim flip) →
    # progress×2 (the milestones) → completed → expired, with a contiguous,
    # monotonic per-workstream sequence.
    assert len(result.workstream_rows) == 1
    ws_id = result.workstream_rows[0]["id"]
    rows = db.scalars(
        sa.select(AgentWorkstreamEvent)
        .where(AgentWorkstreamEvent.workstream_id == ws_id)
        .order_by(AgentWorkstreamEvent.sequence)
    ).all()
    assert [r.event_type for r in rows] == [
        "queued",
        "running",
        "progress",
        "progress",
        "completed",
        "expired",
    ]
    assert [r.sequence for r in rows] == [0, 1, 2, 3, 4, 5]

    # Each progress row carries its milestone text + step/phase payload, in order —
    # the timeline can label/order steps without re-parsing the human text.
    progress_rows = [r for r in rows if r.event_type == "progress"]
    assert [r.text for r in progress_rows] == [
        "Fetching the input…",
        "Reversing the text…",
    ]
    assert progress_rows[0].payload_json == {"step": 1, "phase": "availability_check"}
    assert progress_rows[1].payload_json == {"step": 2, "phase": "run"}

    # Execution still reached the same terminal as without progress.
    assert result.workstream_rows[0]["status"] == "done"


def test_scenario_progress_after_terminal_appends_no_row(db: Session) -> None:
    """US-202 guard: a late ``task_progress`` racing a settled workstream appends
    NO durable row — the terminal status guard holds, so the monotonic-status
    invariant (no regression to running, no orphan progress) is preserved.
    """
    from app.services.session_status_subscriber import apply_task_event

    # No BotSession row needed: apply_task_event tolerates a missing session
    # (agent_id stays NULL) and the harness runs FK-off SQLite.
    session_id = 9202
    # Queue → complete → THEN a late progress for the same task.
    base = {"session_id": session_id, "task_id": 77, "kind": "mcp__demo-http__reverse_text"}
    apply_task_event(db, {**base, "type": "task_queued", "turn_id": 1})
    apply_task_event(
        db, {**base, "type": "task_completed", "status": "done", "result_text": "ok"}
    )
    apply_task_event(
        db,
        {**base, "type": "task_progress", "progress_text": "late straggler", "step": 9},
    )
    db.commit()

    ws = db.scalar(
        sa.select(AgentWorkstream).where(AgentWorkstream.agent_task_id == 77)
    )
    assert ws is not None and ws.status.value == "done"
    rows = db.scalars(
        sa.select(AgentWorkstreamEvent)
        .where(AgentWorkstreamEvent.workstream_id == ws.id)
        .order_by(AgentWorkstreamEvent.sequence)
    ).all()
    # queued + completed only — the late progress added nothing.
    assert [r.event_type for r in rows] == ["queued", "completed"]
    assert all(r.text != "late straggler" for r in rows)


def test_scenario_promotes_background_request(db: Session) -> None:
    """US-201: an EXPLICIT background request promotes a dropped-delegate verdict to
    an off-turn workstream that runs CONCURRENTLY with a router-delegated workstream
    across interleaved user turns (the AC#5 demonstration).

    The fixture is a meeting surface with two delegatable kinds. Turn 1 is a
    router-emitted ``delegate`` (reverse_text); turn 3 is a recorded ``speak`` that
    the gate must DETERMINISTICALLY promote to ``delegate`` (server_time) because
    the utterance carries an explicit background request — keyword recovery is
    suppressed on the meeting surface, so the promotion (not etu.6) is what fires.
    Both workstreams reach ``done`` and both are open before either settles.
    """
    fixture = load_scenario(PROMOTION_FIXTURE)

    # Shape: meeting surface, one recorded delegate + one recorded speak to promote,
    # interleaved with inline speak turns and a status query.
    assert fixture.meeting_backed is True
    actions = [t.router.get("action") for t in fixture.turns]
    assert actions.count("delegate") == 1  # turn 1: the router-emitted delegate
    assert actions.count("speak") >= 3  # weather + revenue + the to-be-promoted turn
    assert actions.count("status") == 1

    result = asyncio.run(run_scenario(fixture, session=db, executor=_two_kind_executor))

    # INV-1 (one terminal/turn) + INV-2 (decision↔utterance parity) across the
    # whole interleaved conversation, including the promoted turn.
    assert result.invariant_violations == []

    # Two workstreams opened — one delegated, one promoted — both reached `done`.
    queued = cast(list[TaskQueued], result.events_of_type("task_queued"))
    completed = cast(list[TaskCompleted], result.events_of_type("task_completed"))
    assert len(queued) == 2, "two workstreams: the router delegate + the promotion"
    assert len(completed) == 2
    assert {c.status for c in completed} == {"done"}
    assert {q.kind for q in queued} == {
        "mcp__demo-http__reverse_text",
        "mcp__demo-http__server_time",
    }

    # Concurrency: BOTH tasks were open before EITHER settled — every task_queued
    # precedes every task_completed (the worker leg runs after the conversation, so
    # both workstreams are live in the registry during the interleaved turns).
    events = result.events
    last_queued = max(
        i for i, e in enumerate(events) if getattr(e, "type", None) == "task_queued"
    )
    first_completed = min(
        i for i, e in enumerate(events) if getattr(e, "type", None) == "task_completed"
    )
    assert last_queued < first_completed, "both workstreams open before either settles"

    # The promotion is DETERMINISTIC: exactly one decision carries the
    # BACKGROUND_PROMOTION marker — on the recorded-SPEAK turn, for server_time —
    # proving the gate (not the recorded verdict) produced the second workstream.
    decisions = cast(
        list[RouterDecisionMade], result.events_of_type("router_decision_made")
    )
    promoted = [d for d in decisions if BACKGROUND_PROMOTION_KEY in d.raw_output]
    assert len(promoted) == 1, "exactly one turn was promoted by the gate"
    assert promoted[0].raw_output[BACKGROUND_PROMOTION_KEY] == {
        "from_action": "speak",
        "to_action": "delegate",
        "kind": "mcp__demo-http__server_time",
    }
    server_time_q = next(q for q in queued if q.kind == "mcp__demo-http__server_time")
    assert server_time_q.turn_id == promoted[0].turn_id  # task bound to the promoted turn

    # Both durable workstream envelopes exist (US-002), FK'd to their tasks, with
    # the deterministic per-kind tool results.
    assert len(result.workstream_rows) == 2
    by_kind = {row["kind"]: row for row in result.task_rows}
    assert by_kind["mcp__demo-http__reverse_text"]["result_text"] == "Q3 launch tagline"[::-1]
    assert by_kind["mcp__demo-http__server_time"]["result_text"] == _SERVER_TIME_RESULT
    for ws in result.workstream_rows:
        assert ws["source_kind"] == "delegate"
        assert ws["status"] == "done"


def test_scenario_fixture_outside_frozen_replay_suite() -> None:
    """The frozen replay fixtures (zero-verdict-drift guard) must stay untouched.

    The scenario fixture lives under ``fixtures/scenarios/``, NOT
    ``fixtures/sessions/`` — so the replay harness's ``discover_fixtures`` (which
    parametrizes ``test_replay_harness_agent.py``) never picks it up.
    """
    discovered = discover_fixtures(SESSIONS_DIR)
    assert SCENARIO_FIXTURE not in discovered
    assert PROMOTION_FIXTURE not in discovered
    assert all("scenarios" not in str(p) for p in discovered)
