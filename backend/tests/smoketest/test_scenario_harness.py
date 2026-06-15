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
    AgentTask,
    AgentWorkstream,
    AgentWorkstreamEvent,
    BotSession,
    CapabilityPolicy,
    Workspace,
)
from johnny.smoketest.replay import discover_fixtures  # noqa: E402
from johnny.smoketest.scenario import load_scenario, run_scenario  # noqa: E402
from johnny.voice_pipeline.events import (  # noqa: E402
    TaskCompleted,
    TaskProgress,
    TaskQueued,
    TaskResultExpired,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SCENARIO_FIXTURE = _FIXTURES / "scenarios" / "delegated-multispeaker"
SESSIONS_DIR = _FIXTURES / "sessions"


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


def test_scenario_fixture_outside_frozen_replay_suite() -> None:
    """The frozen replay fixtures (zero-verdict-drift guard) must stay untouched.

    The scenario fixture lives under ``fixtures/scenarios/``, NOT
    ``fixtures/sessions/`` — so the replay harness's ``discover_fixtures`` (which
    parametrizes ``test_replay_harness_agent.py``) never picks it up.
    """
    discovered = discover_fixtures(SESSIONS_DIR)
    assert SCENARIO_FIXTURE not in discovered
    assert all("scenarios" not in str(p) for p in discovered)
