"""Click entrypoint for ``johnny-replay`` (Johnny-ckz.28.5).

Replays one (or every) committed session fixture through the real voice
pipeline and either asserts the redesign invariants (CI gate) or diffs the
replayed outcome against what the session originally recorded (manual review).

Modelled on ``johnny-tts-smoke`` (Johnny-1ge.7): one PASS / FAIL row per
fixture, exits non-zero on any invariant failure so CI can gate on it.

Run it from the backend workspace::

    uv run johnny-replay --session-id 14 --mode invariants --use-recorded-llm
    uv run johnny-replay --all --mode invariants
    uv run johnny-replay --session-id 2 --mode regression

The exit-code contract (the proof the .28.x redesign closed the gap): a
fixture whose turns all terminate cleanly exits 0; an invariant violation
(e.g. a silent drop) exits 1.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console

from johnny.smoketest.replay import (
    ReplayFixture,
    ReplayResult,
    check_invariants,
    diff_against_recorded,
    discover_fixtures,
    load_fixture,
    run_replay,
)

logger = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR = Path("tests/fixtures/sessions")


def _resolve_fixtures(
    fixtures_dir: Path, session_id: str | None, run_all: bool
) -> list[ReplayFixture]:
    """Load the requested fixtures (one by id, or every one under the dir)."""
    if session_id is not None:
        path = fixtures_dir / session_id
        if not (path / "fixture.json").exists():
            raise click.ClickException(
                f"no fixture for session-id={session_id!r} under {fixtures_dir} "
                f"(looked for {path / 'fixture.json'})"
            )
        return [load_fixture(path)]
    if run_all:
        dirs = discover_fixtures(fixtures_dir)
        if not dirs:
            raise click.ClickException(f"no fixtures found under {fixtures_dir}")
        return [load_fixture(d) for d in dirs]
    raise click.ClickException("pass --session-id <N> or --all")


def _render_invariants(
    console: Console, result: ReplayResult
) -> int:
    """Print the invariants verdict for one fixture; return its failure count."""
    fx = result.fixture
    violations = check_invariants(result.events, fx.runtime)
    # A mis-segmented split fixture (VAD produced a different number of turns
    # than transcripts) is itself a hard failure — the replay didn't reproduce
    # the session, so any green would be a lie. Unified has no STT segmentation.
    seg_ok = fx.runtime != "split" or result.stt_calls == fx.turn_count
    label = f"[bold]{fx.label}[/bold] (session {fx.session_id}, {fx.runtime})"
    if not seg_ok:
        console.print(
            f"  [red]FAIL[/red] {label} — synthesized audio segmented into "
            f"{result.stt_calls} turn(s), expected {fx.turn_count}"
        )
        return 1
    if violations:
        console.print(f"  [red]FAIL[/red] {label} — {len(violations)} invariant violation(s):")
        for v in violations:
            where = f"turn {v.turn_id}" if v.turn_id is not None else "session"
            console.print(f"        [red]·[/red] {v.invariant} {where}: {v.detail}")
        return 1
    console.print(
        f"  [green]PASS[/green] {label} — {fx.turn_count} turn(s), "
        f"all terminated; decision↔utterance parity holds"
    )
    return 0


def _render_regression(console: Console, result: ReplayResult) -> None:
    """Print the replayed-vs-recorded diff for one fixture (never fails the run)."""
    fx = result.fixture
    diffs = diff_against_recorded(fx, result.records)
    label = f"[bold]{fx.label}[/bold] (session {fx.session_id}, {fx.runtime})"
    if not diffs:
        console.print(f"  [green]MATCH[/green] {label} — replayed outcome matches recorded")
        return
    console.print(f"  [yellow]DIFF[/yellow] {label} — {len(diffs)} field(s) changed vs recorded:")
    for d in diffs:
        console.print(
            f"        turn {d.turn_id} · {d.field}: "
            f"recorded={d.recorded!r} → replayed={d.replayed!r}"
        )


@click.command(
    help=(
        "Replay committed session fixtures through the voice engine and assert "
        "the .28.x invariants (mode=invariants, the CI gate) or diff against the "
        "originally-recorded outcome (mode=regression). Split fixtures run on the "
        "LiveKit-Agents engine; unified (S2S) fixtures run on UnifiedVoicePipeline."
    )
)
@click.option("--session-id", type=str, default=None, help="Replay one fixture by session id.")
@click.option("--all", "run_all", is_flag=True, default=False, help="Replay every fixture.")
@click.option(
    "--mode",
    type=click.Choice(["invariants", "regression"]),
    default="invariants",
    show_default=True,
    help="invariants = assert INV-1/INV-2 (CI gate); regression = diff vs recorded.",
)
@click.option(
    "--use-recorded-llm/--use-real-llm",
    default=True,
    show_default=True,
    help=(
        "Use the fixture's recorded LLM outputs (deterministic). Real-LLM "
        "replay runs against live providers via the session UI's Replay button."
    ),
)
@click.option(
    "--fixtures-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_FIXTURES_DIR,
    show_default=True,
    help="Directory of <session-id>/fixture.json fixtures.",
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Enable DEBUG logging.")
def main(
    session_id: str | None,
    run_all: bool,
    mode: str,
    use_recorded_llm: bool,
    fixtures_dir: Path,
    verbose: bool,
) -> None:
    """Replay harness entrypoint."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        # A fixture that simulates the session-14 router hang makes the engine
        # log the (expected) router-timeout exception. That's the path under
        # test, not a harness failure — mute the engine loggers so the report is
        # clean. ``--verbose`` brings it back for debugging.
        logging.getLogger("johnny.agent.gate").setLevel(logging.CRITICAL)
        logging.getLogger("johnny.agent.router_gate").setLevel(logging.CRITICAL)
    if not use_recorded_llm:
        raise click.ClickException(
            "--use-real-llm is only supported via the session UI's Replay "
            "button (it needs live provider credentials); the CLI replays the "
            "fixture's recorded LLM outputs."
        )

    # Split fixtures run on the LiveKit-Agents engine (lazy import so the
    # unified/S2S path stays usable without the ``agent`` extra); unified
    # fixtures run on UnifiedVoicePipeline via run_replay.
    console = Console()
    console.print(
        f"[bold cyan]Johnny replay harness[/bold cyan] — "
        f"mode={mode}, recorded-llm\n"
    )

    fixtures = _resolve_fixtures(fixtures_dir, session_id, run_all)
    failures = 0
    for fx in fixtures:
        if fx.runtime == "split":
            from johnny.smoketest.replay_agent import run_agent_replay

            runner = run_agent_replay
        else:
            runner = run_replay
        result = asyncio.run(runner(fx))
        if mode == "invariants":
            failures += _render_invariants(console, result)
        else:
            _render_regression(console, result)

    if mode == "invariants":
        verdict = "[green]all fixtures hold the invariants[/green]" if failures == 0 else (
            f"[red]{failures} fixture(s) failed[/red]"
        )
        console.print(f"\nSummary: {len(fixtures)} fixture(s) · {verdict}")
        sys.exit(1 if failures else 0)
    console.print(f"\nSummary: {len(fixtures)} fixture(s) diffed (regression is manual-review)")
    sys.exit(0)


if __name__ == "__main__":
    main()
