"""Click entrypoint for ``johnny-scenario`` (Johnny-d6w.1 / US-001).

The scenario harness drives a scripted, multi-speaker conversation through the
**real** pipeline so the router delegates, the worker drives the task to
terminal, the four ``task_*`` events fire, and the tool / terminal / result can
be asserted (see :mod:`johnny.smoketest.scenario`). Two subcommands:

* ``check`` — run the committed fixture through the **deterministic** in-process
  engine (SQLite ``:memory:``, recorded router LLM, the pure ``reverse_text``
  tool stand-in) and report PASS/FAIL on the delegate → ``done`` lifecycle, the
  four ``task_*`` events, and INV-1/INV-2. The CI gate is the pytest mirror
  (``tests/smoketest/test_scenario_harness.py``); this is the operator one-shot.
* ``generate`` — run the SAME deterministic engine against the **real Postgres**
  under a fresh ``bot_sessions`` row, committing genuine ``agent_tasks`` rows
  (the DB has 0 task rows today — PRD §11) so later UI phases have real
  delegated-task data to browser-validate.

Docker-only runtime — run inside the api container::

    docker compose exec api python -m johnny.smoketest.scenario_cli check
    docker compose exec api python -m johnny.smoketest.scenario_cli generate

See ``docs/session-view-redesign/SCENARIO-HARNESS.md`` for the full procedure,
including the live-LLM / real-Metabase opt-in for a fresh, fully-realistic
fixture session.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console

DEFAULT_FIXTURE = Path("tests/fixtures/scenarios/delegated-multispeaker")
_TASK_EVENT_TYPES = (
    "task_queued",
    "task_progress",
    "task_completed",
    "task_result_expired",
)


@click.group(
    help=(
        "US-001 scenario harness — a real delegated, multi-speaker session. "
        "'check' runs the deterministic gate; 'generate' commits genuine "
        "agent_tasks rows to the real DB for browser validation."
    )
)
def main() -> None:
    pass


@main.command("check")
@click.option(
    "--fixture",
    "fixture_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_FIXTURE,
    show_default=True,
    help="Scenario fixture directory (with fixture.json).",
)
def check_cmd(fixture_path: Path) -> None:
    """Deterministic in-process gate: delegate → worker → done + four task_* events."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.db.models import (
        Agent,
        AgentTask,
        AgentWorkstream,
        AgentWorkstreamEvent,
        BotSession,
        CapabilityPolicy,
        Workspace,
    )
    from johnny.smoketest.scenario import load_scenario, run_scenario

    console = Console()
    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
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
    session = sessionmaker(bind=engine, future=True)()
    try:
        fixture = load_scenario(fixture_path)
        result = asyncio.run(run_scenario(fixture, session=session))
    finally:
        session.close()

    violations = result.invariant_violations
    event_counts = {t: len(result.events_of_type(t)) for t in _TASK_EVENT_TYPES}
    done_rows = [r for r in result.task_rows if r["status"] == "done"]
    # US-002: the single durable writer produced one workstream envelope per
    # delegated task, FK'd to it and carrying the same terminal result.
    done_streams = [
        w
        for w in result.workstream_rows
        if w["status"] == "done" and w["agent_task_id"] is not None
    ]
    ok = (
        not violations
        and all(event_counts[t] == 1 for t in _TASK_EVENT_TYPES)
        and len(done_rows) == 1
        and len(done_streams) == 1
    )

    console.print(f"[bold]{fixture.label}[/bold]  speakers={list(fixture.speakers)}")
    console.print(f"  task_* events: {event_counts}")
    for r in done_rows:
        console.print(
            f"  agent_task #{r['task_id']} {r['kind']} → {r['status']} "
            f"· result_text={r['result_text']!r}"
        )
    for w in done_streams:
        console.print(
            f"  workstream #{w['id']} (task #{w['agent_task_id']}, {w['source_kind']}) "
            f"→ {w['status']}/{w['delivery_status']}"
        )
    if violations:
        console.print(f"  [red]invariant violations:[/red] {violations}")
    if ok:
        console.print(
            "  [green]PASS[/green] — delegate produced a done task + workstream "
            "envelope, all four task_* events fired, INV-1/INV-2 hold"
        )
        sys.exit(0)
    console.print("  [red]FAIL[/red]")
    sys.exit(1)


@main.command("generate")
@click.option(
    "--fixture",
    "fixture_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_FIXTURE,
    show_default=True,
    help="Scenario fixture directory (with fixture.json).",
)
def generate_cmd(fixture_path: Path) -> None:
    """Commit genuine agent_tasks rows to the REAL Postgres for browser validation."""
    from app.db.models import BotSession, BotSessionSource, BotSessionStatus
    from app.db.session import SessionLocal
    from johnny.smoketest.scenario import load_scenario, run_scenario

    console = Console()
    fixture = load_scenario(fixture_path)
    session = SessionLocal()
    try:
        bot_session = BotSession(
            source=BotSessionSource.BROWSER,
            status=BotSessionStatus.ENDED,
            bot_name="Johnny (scenario US-001)",
        )
        session.add(bot_session)
        session.commit()
        sid = int(bot_session.id)
        result = asyncio.run(
            run_scenario(fixture, session=session, bot_session_id=sid)
        )
    finally:
        session.close()

    console.print(
        f"[green]Generated[/green] canonical delegated session "
        f"bot_session_id=[bold]{sid}[/bold] from {fixture.label!r}"
    )
    for r in result.task_rows:
        console.print(
            f"  agent_task #{r['task_id']} {r['kind']} → {r['status']} "
            f"· result_text={r['result_text']!r}"
        )
    console.print(
        "\nInspect the genuine rows:\n"
        f"  docker compose exec postgres psql -U johnny johnny -c "
        f'"select id, kind, status, result_text from agent_tasks '
        f'where bot_session_id={sid};"'
    )


if __name__ == "__main__":
    main()
