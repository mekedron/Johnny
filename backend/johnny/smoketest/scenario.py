"""Scenario harness — a real delegated, multi-speaker session (Johnny-d6w.1 / US-001).

Sibling to :mod:`johnny.smoketest.replay`. The replay harness drives recorded
single-speaker turns through :meth:`RouterGate.run_turn` against an in-memory
event bus and asserts the decision/terminal invariants — but it deliberately
never builds a :class:`~johnny.agent.tasks.TaskCoordinator` or a worker, so it
cannot exercise the *delegate → queued row → worker → tool → terminal* path the
Session-View redesign needs real data for (the DB has **0** ``agent_tasks`` rows
today; PRD §11).

This module adds exactly that, reusing the replay building blocks unchanged:

* the pure checkers :func:`~johnny.smoketest.replay.check_invariants` and
  :func:`~johnny.smoketest.replay.assemble_turns` (they read the captured event
  stream, so they apply to any engine), and
* the recorded-LLM / say() / reply doubles
  (:class:`~johnny.smoketest.replay_agent._RecordedRouterLLM`,
  :class:`~johnny.smoketest.replay_agent._ReplaySayStub`,
  :class:`~johnny.smoketest.replay_agent._ReplaySpeechHandle`).

On top it wires a **real** ``TaskCoordinator`` (with the production
:class:`~app.services.agent_tasks.SqlAlchemyTaskSink`) and drives the **real**
worker claim/settle functions (:func:`~app.services.task_worker.claim_queued_tasks`
/ :func:`~app.services.task_worker.settle_claimed_task`) in-process, so a scripted
``delegate`` verdict produces a genuine ``agent_tasks`` row, the four ``task_*``
events fire on the bus, and the tool / terminal / result can be asserted.

Two ways to run it (mirroring the replay "recorded-CLI vs real-LLM-UI" split):

* **deterministic** (:func:`run_scenario`, the CI gate): recorded router verdicts
  + fake say/reply + a SQLite session + a deterministic tool executor — hermetic,
  no Redis, no network, no live LLM.
* **generation** (the live stack, documented in
  ``docs/session-view-redesign/SCENARIO-HARNESS.md``): drive the same script
  through the browser-session endpoints against the running api + worker +
  ``mcp-demo-http`` so the rows are committed for later browser validation.

Requires the ``agent`` extra (``livekit-agents``) like
:mod:`johnny.smoketest.replay_agent`; imported only by the CLI / tests.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from livekit.agents.llm import ChatContext, StopResponse
from livekit.agents.llm.chat_context import ChatMessage as LKChatMessage
from livekit.agents.voice import SpeechHandle
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AgentModelCall, AgentWorkstream
from app.services.agent_tasks import SqlAlchemyTaskSink
from app.services.task_worker import claim_queued_tasks, settle_claimed_task
from johnny.agent.gate import TurnIndex, TurnLedger
from johnny.agent.model_call_trace import ModelCallTrace
from johnny.agent.observability import build_observability
from johnny.agent.router_gate import RouterGate, RouterGateConfig
from johnny.agent.speech_queue import (
    RESULT_DEFAULT_TTL_S,
    SpeechPriority,
    SpeechQueue,
)
from johnny.agent.task_catalog import TaskCatalogEntry
from johnny.agent.task_wiring import (
    build_publish_task_completed,
    build_publish_task_queued,
    make_task_progress_reporter,
)
from johnny.agent.tasks import QueuedTask, TaskCoordinator, TaskResult
from johnny.skills.executor import (
    PHASE_AVAILABILITY_CHECK,
    PHASE_RUN,
    TaskProgressReporter,
)
from johnny.smoketest.replay import (
    SPLIT_RUNTIME,
    ReplayTurn,
    TurnRecord,
    assemble_turns,
    check_invariants,
)
from johnny.smoketest.replay_agent import (
    SIMULATED_HANG_TIMEOUT_S,
    _RecordedRouterLLM,
    _ReplaySayStub,
    _ReplaySpeechHandle,
)
from johnny.voice_pipeline import InMemoryEventBus, PipelineEvent, TranscriptFinalized
from johnny.voice_pipeline.events import TaskProgress, TaskResultExpired


# A monotonic-int clock for the task events, so timestamps are deterministic
# across a run (no wall clock — the replay-parity stance).
class TaskExecutorFn(Protocol):
    """Harness executor contract: a deterministic stand-in for the worker's
    real skill/MCP executor. Accepts an optional ``reporter`` (US-202) so a
    multi-step variant narrates milestones through the SAME seam production
    runs; single-call stand-ins accept-and-ignore it."""

    def __call__(
        self, task: QueuedTask, *, reporter: TaskProgressReporter | None = None
    ) -> Awaitable[TaskResult]: ...


# --- fixture model (a superset of the replay fixture) -----------------------


@dataclass(frozen=True)
class ScenarioTurn(ReplayTurn):
    """One scripted utterance — a :class:`ReplayTurn` (``text`` / ``speaker`` /
    ``confidence`` / ``router`` / ``answer`` / ``simulate``) plus per-turn
    expectations the test asserts on for the delegating turn.

    For a delegating turn ``router`` carries the Phase-3 shape ``{should_speak,
    confidence, reason, action: "delegate", task: {kind, args, ack}}`` (parsed by
    ``reasoning._parse_task_request``); ``expect`` documents the asserted outcome,
    e.g. ``{"kind": "...", "terminal_status": "done"}``.
    """

    expect: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioFixture:
    """A scripted multi-speaker scenario plus the delegatable-kind config that
    lets the router emit ``delegate`` (the catalog + executor-known set)."""

    session_id: str
    label: str
    bot_session_id: int = 1
    mode: str = "autonomous"
    confidence_threshold: float = 0.7
    instructions: str = ""
    task_catalog: tuple[TaskCatalogEntry, ...] = ()
    executor_kinds: frozenset[str] = frozenset()
    # Meeting surface (Johnny-trt.50): suppresses keyword delegate-recovery for a
    # bare SPEAK so ambient meeting chatter never triggers an unasked skill run.
    # US-201 promotion (an EXPLICIT background request) overrides it, so a
    # meeting-surface fixture is how the scenario exercises that override.
    meeting_backed: bool = False
    turns: tuple[ScenarioTurn, ...] = ()
    runtime: str = SPLIT_RUNTIME

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def speakers(self) -> tuple[str, ...]:
        """Distinct speakers in first-seen order."""
        seen: list[str] = []
        for t in self.turns:
            if t.speaker not in seen:
                seen.append(t.speaker)
        return tuple(seen)


def scenario_from_dict(data: dict[str, Any]) -> ScenarioFixture:
    """Parse a ``fixture.json`` dict into a :class:`ScenarioFixture`."""
    turns = tuple(
        ScenarioTurn(
            text=str(t["text"]),
            speaker=str(t.get("speaker", "user")),
            confidence=float(t.get("confidence", 0.9)),
            router=dict(t.get("router", {})),
            answer=t.get("answer"),
            simulate=t.get("simulate"),
            expect=dict(t.get("expect", {})),
        )
        for t in data.get("turns", [])
    )
    catalog = tuple(
        TaskCatalogEntry(
            kind=str(e["kind"]),
            one_liner=str(e.get("one_liner", "")),
            keywords=tuple(e.get("keywords", []) or []),
            available=bool(e.get("available", True)),
            unavailable_reason=str(e.get("unavailable_reason", "")),
            hidden=bool(e.get("hidden", False)),
            internal=bool(e.get("internal", False)),
        )
        for e in data.get("task_catalog", [])
    )
    return ScenarioFixture(
        session_id=str(data["session_id"]),
        label=str(data.get("label", f"scenario-{data['session_id']}")),
        bot_session_id=int(data.get("bot_session_id", 1)),
        mode=str(data.get("mode", "autonomous")),
        confidence_threshold=float(data.get("confidence_threshold", 0.7)),
        instructions=str(data.get("instructions", "")),
        task_catalog=catalog,
        executor_kinds=frozenset(data.get("executor_kinds", []) or []),
        meeting_backed=bool(data.get("meeting_backed", False)),
        turns=turns,
        runtime=str(data.get("runtime", SPLIT_RUNTIME)),
    )


def load_scenario(path: Path) -> ScenarioFixture:
    """Load a scenario from ``<dir>/fixture.json`` or a JSON file."""
    fixture_path = path / "fixture.json" if path.is_dir() else path
    with fixture_path.open("r", encoding="utf-8") as fh:
        return scenario_from_dict(json.load(fh))


# --- deterministic tool executor (the CI gate's stand-in for the demo MCP) --


def _reverse_result(task: QueuedTask) -> TaskResult:
    text = str(task.spec.args.get("text", ""))
    reversed_text = text[::-1]
    return TaskResult(
        status="done",
        result_text=reversed_text,
        result_json={
            "mcp_server": "demo-http",
            "mcp_tool": "reverse_text",
            "output": reversed_text,
            "is_error": False,
        },
    )


def reverse_text_executor(
    task: QueuedTask, *, reporter: TaskProgressReporter | None = None
) -> Awaitable[TaskResult]:
    """A pure, deterministic stand-in for the ``mcp__demo-http__reverse_text`` tool.

    The CI gate is hermetic — no Redis, no MCP SDK, no network — so the default
    executor mirrors the demo server's ``reverse_text`` tool (a pure string
    reversal) and the ``result_json`` shape the real
    :func:`johnny.mcp.executor.build_mcp_task_executor` produces
    (``mcp_server`` / ``mcp_tool`` / ``is_error``). The **generation** run uses
    the real worker against the running ``mcp-demo-http`` service instead (see
    the module docstring), which is where the genuine MCP tool call happens.

    ``reporter`` is accepted for the uniform harness executor contract but
    ignored: a single-call stand-in narrates no mid-run milestones (the
    multi-step variant below does). The worker still publishes the step-0 claim
    signal, so this executor's workstream reaches ``running`` regardless.
    """

    async def _run() -> TaskResult:
        return _reverse_result(task)

    return _run()


def make_multistep_reverse_executor(
    *, steps: tuple[tuple[str, str], ...] = (
        ("Fetching the input…", PHASE_AVAILABILITY_CHECK),
        ("Reversing the text…", PHASE_RUN),
    ),
) -> TaskExecutorFn:
    """A deterministic stand-in that narrates ≥2 milestones (US-202, Johnny-d6w.14).

    Emits each ``(text, phase)`` in ``steps`` through the ``reporter`` — the SAME
    milestone → ``TaskProgress`` → durable-writer seam the production skill/MCP
    executors drive — then returns the same ``reverse_text`` result. Used by the
    progress-fixture test to prove milestones emit (steps 1..n on top of the
    worker's step-0 claim) and persist to ``agent_workstream_events`` in order.
    """

    async def _run(
        task: QueuedTask, *, reporter: TaskProgressReporter | None = None
    ) -> TaskResult:
        if reporter is not None:
            for text, phase in steps:
                await reporter.report(text, phase=phase)
        return _reverse_result(task)

    return _run


# --- router model-call sink (US-004) ----------------------------------------


class _ScenarioModelCallSink:
    """Harness :class:`~johnny.agent.model_call_trace.ModelCallSink` (US-004).

    Writes each router :class:`ModelCallTrace` as a ``role='router'``
    ``agent_model_calls`` row through the scenario's SHARED session — the same way
    :class:`~app.services.agent_tasks.SqlAlchemyTaskSink` is wired here. The
    production :class:`~app.services.model_calls.SqlAlchemyModelCallSink` opens its
    own short-lived session per write (correct for the live Postgres path), but on
    the CI gate's in-memory SQLite ``StaticPool`` (a single shared connection) a
    second session collides with the shared session's transaction, so the harness
    shares the one session like every other writer.
    """

    def __init__(self, session: Session, bot_session_id: int) -> None:
        self._session = session
        self._bot_session_id = bot_session_id

    async def record(self, trace: ModelCallTrace) -> None:
        self._session.add(
            AgentModelCall(
                bot_session_id=self._bot_session_id,
                turn_id=trace.turn_id,
                role=trace.role,
                step_index=trace.step_index,
                model_provider=trace.model_provider,
                model_name=trace.model_name,
                prompt_json=trace.prompt or None,
                response_text=trace.response_text,
                tool_calls_json=trace.tool_calls or None,
                finish_reason=trace.finish_reason,
                prompt_tokens=trace.prompt_tokens,
                completion_tokens=trace.completion_tokens,
                total_tokens=trace.total_tokens,
                time_to_first_token_ms=trace.time_to_first_token_ms,
                duration_ms=trace.duration_ms,
                started_at=trace.started_at,
                finished_at=trace.finished_at,
            )
        )
        self._session.commit()


# --- result record ----------------------------------------------------------


@dataclass
class ScenarioResult:
    """Everything one deterministic scenario run produced, for assertions."""

    fixture: ScenarioFixture
    events: list[PipelineEvent]
    records: list[TurnRecord]
    # The terminal ``agent_tasks`` rows, read back from the DB after settle:
    # ``[{"task_id", "kind", "status", "result_text", "result_json", "turn_id"}]``.
    task_rows: list[dict[str, Any]]
    # The durable ``agent_workstreams`` rows (US-002) the single durable writer
    # produced from the captured task_*/workstream events — the envelope on top
    # of ``task_rows``. ``[{"id","agent_task_id","source_kind","status",
    # "delivery_status","result_text","result_json","title","source_turn_id"}]``.
    workstream_rows: list[dict[str, Any]] = field(default_factory=list)

    def events_of_type(self, type_name: str) -> list[PipelineEvent]:
        return [e for e in self.events if getattr(e, "type", None) == type_name]

    @property
    def invariant_violations(self) -> list[Any]:
        return check_invariants(self.events, self.fixture.runtime)


# --- the deterministic engine -----------------------------------------------


async def run_scenario(
    fixture: ScenarioFixture,
    *,
    session: Session,
    executor: TaskExecutorFn = reverse_text_executor,
    bot_session_id: int | None = None,
    bus: InMemoryEventBus | None = None,
) -> ScenarioResult:
    """Drive ``fixture`` through the real gate + coordinator + worker, in-process.

    Assembles the gate / ledger / observability exactly as
    :func:`johnny.smoketest.replay_agent.run_agent_replay` does, then adds a
    real :class:`~johnny.agent.tasks.TaskCoordinator` (production SQLite-backed
    sink + the live ``task_*`` publish seams) so a scripted ``delegate`` verdict:

    1. writes a ``queued`` ``agent_tasks`` row (``TaskCoordinator.begin`` →
       ``SqlAlchemyTaskSink.record_queued``) and publishes ``TaskQueued``;
    2. speaks the ack (the turn's single ``replied`` terminal — INV-1);

    and then, after the conversation, the **worker leg** runs in-process —
    :func:`claim_queued_tasks` (publishes ``TaskProgress``) → ``executor`` →
    :func:`settle_claimed_task` (terminal row) → ``TaskCompleted`` — so all the
    happy-path ``task_*`` events fire and the row reaches ``done``.

    ``session`` is a SQLAlchemy session (SQLite ``:memory:`` in the CI gate);
    ``executor`` defaults to the deterministic ``reverse_text`` stand-in.

    ``bus`` is an optional event bus to capture/forward the pipeline events. It
    defaults to a fresh :class:`InMemoryEventBus` (the ``check``/``generate``
    capture-then-persist path); the ``live`` CLI mode injects an
    :class:`InMemoryEventBus` subclass that ALSO publishes each frame to Redis in
    real time, so a browser on the live session page sees genuine
    ``task_*`` transitions (US-101 browser validation). It must expose
    ``snapshot()`` — the durable-writer replay reads it post-run.
    """
    if fixture.runtime != SPLIT_RUNTIME:
        raise ValueError(
            f"the scenario engine is split-only; fixture {fixture.label!r} is "
            f"runtime={fixture.runtime!r}"
        )

    if bus is None:
        bus = InMemoryEventBus()
    turn_index = TurnIndex()
    obs = build_observability(
        bus,
        turn_index,
        mode=fixture.mode,
        allowed_replies=(),
        resolve_turn_id=lambda _speech_id: turn_index.last(),
        session_id=fixture.session_id,
    )
    ledger = TurnLedger(obs.session_terminal_emitter)
    router = _RecordedRouterLLM(fixture.turns)

    # Deterministic monotonic-int clock for the task events.
    _tick = {"v": 0}

    def _clock() -> int:
        _tick["v"] += 1
        return _tick["v"]

    sink = SqlAlchemyTaskSink(session, bot_session_id or fixture.bot_session_id)
    coordinator = TaskCoordinator(
        sink,
        executor=executor,
        publish_queued=build_publish_task_queued(
            bus, session_id=fixture.session_id, clock=_clock
        ),
        publish_completed=build_publish_task_completed(
            bus, session_id=fixture.session_id, clock=_clock
        ),
        # Worker-owned: leave the row queued for the worker leg below rather
        # than resolving it in-session (matches production, where real kinds
        # are claimed by the worker, not run in the session loop).
        runs_in_session=lambda _kind: False,
    )
    # Suppress the poll watcher begin() would otherwise spawn for a worker-owned
    # kind: this harness drives the worker leg itself, deterministically, so it
    # needs no background polling task to clean up (the Phase-5 "push listener
    # active" branch of begin()).
    coordinator._remote_listener_active = True

    has_timeout = any(t.simulate == "timeout" for t in fixture.turns)
    config = RouterGateConfig(
        confidence_threshold=fixture.confidence_threshold,
        mode=fixture.mode,
        instructions=fixture.instructions,
        allowed_replies=(),
        task_catalog=fixture.task_catalog,
        executor_kinds=fixture.executor_kinds,
        meeting_backed=fixture.meeting_backed,
        router_llm_timeout_s=(SIMULATED_HANG_TIMEOUT_S if has_timeout else 0.0),
    )
    # US-004: record each decided turn's router LLM call as a ``role='router'``
    # agent_model_calls row, through the shared session (see _ScenarioModelCallSink).
    model_call_sink = _ScenarioModelCallSink(
        session, bot_session_id or fixture.bot_session_id
    )
    gate = RouterGate(
        router,
        config=config,
        ledger=ledger,
        record_decision=obs.record_decision,
        record_spoke=obs.record_spoke,
        record_suggested=obs.record_suggested,
        tasks=coordinator,
        resolve_turn_id=lambda _speech_id: turn_index.last(),
        # US-003: mint into the same TurnIndex the obs emitters read so request_id
        # propagates to decisions, utterances, and the delegated workstream.
        assign_request_id=turn_index.assign_request_id,
        model_call_sink=model_call_sink,
    )
    say_stub = _ReplaySayStub()
    gate.attach_say(say_stub)

    await _drive_turns(fixture, gate, say_stub, obs, bus)
    await gate.aclose()

    # The worker leg: claim every queued row this run produced and carry it to a
    # terminal status, firing TaskProgress (claim) + TaskCompleted (settle).
    publish_completed = build_publish_task_completed(
        bus, session_id=fixture.session_id, clock=_clock
    )
    task_rows = await _drive_worker(
        session,
        executor,
        bus,
        publish_completed,
        fixture.session_id,
        _clock,
        # Scope the claim to the scenario's own kinds so a run against a SHARED
        # Postgres (the `generate` path) never claims the operator's unrelated
        # queued tasks. Empty → None disables the filter (the SQLite gate).
        only_kinds=fixture.executor_kinds or None,
    )

    await coordinator.aclose(drain_grace_s=0.0)

    events = bus.snapshot()
    records = assemble_turns(events, SPLIT_RUNTIME)
    # US-002: replay the captured task_*/workstream events through the single
    # durable writer's handlers (the same code path the live subscriber runs,
    # invoked directly here — the harness has no Redis subscriber) to produce
    # the durable ``agent_workstreams`` envelope later UI phases render.
    workstream_rows = _persist_workstreams(
        session, events, bot_session_id or fixture.bot_session_id
    )
    return ScenarioResult(
        fixture=fixture,
        events=events,
        records=records,
        task_rows=task_rows,
        workstream_rows=workstream_rows,
    )


def _persist_workstreams(
    session: Session, events: list[PipelineEvent], bot_session_id: int
) -> list[dict[str, Any]]:
    """Feed captured task_*/workstream events through the durable writer (US-002).

    Invokes the subscriber's pure handlers directly (no Redis here), committing
    the durable ``agent_workstreams`` rows, then reads back this session's rows
    for assertions. Scoped by ``bot_session_id`` so a run against a shared
    Postgres (the ``generate`` path) returns only its own envelopes.
    """
    from app.services.session_status_subscriber import (
        TASK_EVENT_TYPES,
        WORKSTREAM_DELIVERY_EVENT_TYPE,
        apply_task_event,
        apply_workstream_delivery_event,
    )
    from johnny.voice_pipeline.events import event_to_dict

    for event in events:
        etype = getattr(event, "type", None)
        if etype not in TASK_EVENT_TYPES and etype != WORKSTREAM_DELIVERY_EVENT_TYPE:
            continue
        payload = event_to_dict(event)
        # In production an event's ``session_id`` IS the numeric bot_session_id;
        # the harness labels sessions by name, so stamp the real int id (the
        # same one the agent_tasks rows carry) before the durable writer coerces it.
        payload["session_id"] = bot_session_id
        if etype in TASK_EVENT_TYPES:
            apply_task_event(session, payload)
        else:
            apply_workstream_delivery_event(session, payload)
    session.commit()
    rows = session.scalars(
        select(AgentWorkstream)
        .where(AgentWorkstream.bot_session_id == bot_session_id)
        .order_by(AgentWorkstream.id)
    ).all()
    return [
        {
            "id": w.id,
            "agent_task_id": w.agent_task_id,
            "source_kind": w.source_kind.value,
            "status": w.status.value,
            "delivery_status": w.delivery_status.value,
            "result_text": w.result_text,
            "result_json": w.result_json,
            "title": w.title,
            "source_turn_id": w.source_turn_id,
            "request_id": w.request_id,
        }
        for w in rows
    ]


async def _drive_turns(
    fixture: ScenarioFixture,
    gate: RouterGate,
    say_stub: _ReplaySayStub,
    obs: Any,
    bus: InMemoryEventBus,
) -> None:
    """Feed each scripted turn through ``gate.run_turn`` (the replay loop shape).

    A ``delegate`` verdict speaks its ack via ``say()`` and raises
    ``StopResponse``; the ack handle's done-callback emits the turn's ``replied``
    terminal + ``AgentSpoke`` (and ``begin()`` already wrote the queued row +
    published ``TaskQueued`` inside ``run_turn``). A plain SPEAK turn binds its
    recorded answer through a reply handle, exactly like the replay harness.
    """
    ctx = ChatContext.empty()
    for i, turn in enumerate(fixture.turns):
        await obs.transcript_finalized_sink(
            TranscriptFinalized(
                text=turn.text,
                timestamp_ms=(i + 1) * 1000,
                speaker=turn.speaker,
                confidence=turn.confidence,
                session_id=fixture.session_id,
            )
        )
        msg = LKChatMessage(role="user", content=[turn.text])
        say_before = len(say_stub.handles)
        spoke = False
        try:
            await gate.run_turn(ctx, msg)
            spoke = True
        except StopResponse:
            spoke = False
        ctx.add_message(role="user", content=turn.text)

        # say()-path verdict (delegate ack / status): fire its done so the gate
        # emits the replied terminal + AgentSpoke, then skip the reply path.
        if len(say_stub.handles) > say_before:
            say_handle = say_stub.handles[-1]
            say_handle.fire_done()
            if gate._reply_tasks:
                await _gather(gate._reply_tasks)
            ctx.add_message(role="assistant", content=say_stub.texts[-1])
            continue

        if not spoke:
            continue
        if turn.simulate == "barge_before_bind":
            # US-301 / C8 repro: this SPEAK turn is pushed onto the pending queue,
            # but its generate_reply never binds — the user barges in first, so it
            # terminalizes no_reply(barge_in) via the interruption path (not its
            # own reply callback). Its id is left a STALE head that a LATER reply
            # must not FIFO-pop (the session-3 cross-thread bleed: a long inline
            # turn's stranded id was bound to a quick hearing-check reply).
            await gate._ledger.emit(
                msg.id,
                terminal_state="no_reply",
                no_reply_reason="barge_in",
                detail="cut before its reply bound (US-301 overlap repro)",
            )
            continue
        chat_items = (
            [LKChatMessage(role="assistant", content=[turn.answer])]
            if turn.answer
            else []
        )
        handle = _ReplaySpeechHandle(handle_id=f"item_reply_{i}", chat_items=chat_items)
        gate.bind_reply(_as_speech_handle(handle))
        handle.fire_done()
        if gate._reply_tasks:
            await _gather(gate._reply_tasks)
        if turn.answer:
            ctx.add_message(role="assistant", content=turn.answer)


async def _drive_worker(
    session: Session,
    executor: TaskExecutorFn,
    bus: InMemoryEventBus,
    publish_completed: Any,
    session_id: str,
    clock: Callable[[], int],
    only_kinds: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the real worker claim/settle path for every queued row, in-process."""
    from app.db.models import AgentTask

    claim_kwargs: dict[str, Any] = {"limit": 64}
    if only_kinds:
        claim_kwargs["only_kinds"] = only_kinds
    claimed = claim_queued_tasks(session, **claim_kwargs)
    session.commit()
    rows: list[dict[str, Any]] = []
    for ct in claimed:
        # Step-0 claim signal (queued→running), exactly like the worker's
        # _claim_once. progress_text="" and step defaults to 0.
        await bus.publish(
            TaskProgress(
                task_id=ct.task_id,
                kind=ct.kind,
                timestamp_ms=clock(),
                progress_text="",
                turn_id=ct.turn_id,
                request_id=ct.request_id,  # US-003: echo from the claimed row
                session_id=session_id,
            )
        )
        queued = ct.as_queued_task()
        # US-202: the SAME reporter the worker builds — milestones (step 1..n)
        # the executor narrates flow through bus.publish as TaskProgress, then
        # the durable writer turns each into an agent_workstream_events row.
        reporter = make_task_progress_reporter(
            bus.publish,
            task_id=ct.task_id,
            kind=ct.kind,
            turn_id=ct.turn_id,
            request_id=ct.request_id,
            session_id=session_id,
            clock=clock,
        )
        result = await executor(queued, reporter=reporter)
        ok = settle_claimed_task(
            session,
            task_id=ct.task_id,
            claim_attempts=ct.attempts,
            status=result.status,
            result_text=result.result_text or "",
            result_json=result.result_json,
            error=result.error or "",
        )
        session.commit()
        if not ok:
            continue
        await publish_completed(queued, result.status, result)
        # The deterministic harness has no live speech-delivery loop, so a done
        # result is never spoken — drive it through the REAL SpeechQueue and let
        # it expire past the 120s RESULT TTL. This fires the fourth task_* event
        # (TaskResultExpired) through the real expiry trigger and models the
        # PRD's "done but undelivered/expired" delivery state (§7).
        if result.status == "done" and (result.result_text or "").strip():
            await _expire_undelivered_result(
                bus,
                task_id=ct.task_id,
                kind=ct.kind,
                turn_id=ct.turn_id,
                result_text=result.result_text or "",
                session_id=session_id,
                clock=clock,
            )
        row = session.get(AgentTask, ct.task_id)
        rows.append(
            {
                "task_id": ct.task_id,
                "kind": ct.kind,
                "status": str(row.status.value) if row is not None else None,
                "result_text": row.result_text if row is not None else None,
                "result_json": row.result_json if row is not None else None,
                "turn_id": ct.turn_id,
            }
        )
    return rows


async def _expire_undelivered_result(
    bus: InMemoryEventBus,
    *,
    task_id: int,
    kind: str,
    turn_id: int | None,
    result_text: str,
    session_id: str,
    clock: Callable[[], int],
) -> None:
    """Fire ``TaskResultExpired`` through the real :class:`SpeechQueue` expiry.

    Enqueues the done result as a ``RESULT_UNSOLICITED`` item and sweeps past the
    120 s RESULT TTL so the queue's own ``on_dropped`` fires with the real expiry
    reason — the same drop trigger the Phase-5 ``TaskSpeechDeliverer`` wires to
    ``TaskResultExpired`` (task_wiring.py). The harness publishes the resulting
    event onto its bus so all four ``task_*`` event types are captured.
    """
    queue = SpeechQueue(now=0.0)
    dropped: list[str] = []
    queue.enqueue(
        result_text,
        SpeechPriority.RESULT_UNSOLICITED,
        now=0.0,
        on_dropped=lambda _item, reason: dropped.append(reason),
        task_id=task_id,
        kind=kind,
    )
    queue.sweep_expired(now=RESULT_DEFAULT_TTL_S + 1.0)
    for reason in dropped:
        await bus.publish(
            TaskResultExpired(
                task_id=task_id,
                kind=kind,
                timestamp_ms=clock(),
                reason=reason,
                turn_id=turn_id,
                session_id=session_id,
            )
        )


async def _gather(tasks: Iterable[Any]) -> None:
    import asyncio

    await asyncio.gather(*tuple(tasks))


def _as_speech_handle(handle: _ReplaySpeechHandle) -> SpeechHandle:
    from typing import cast

    return cast(SpeechHandle, handle)


__all__ = [
    "ScenarioFixture",
    "ScenarioResult",
    "ScenarioTurn",
    "load_scenario",
    "reverse_text_executor",
    "run_scenario",
    "scenario_from_dict",
]
