"""Build a replay fixture from a persisted session (Johnny-ckz.28.5).

Bridges the durable session rows (``bot_sessions`` + ``agent_decisions`` +
``agent_utterances``) to the runtime-agnostic :class:`ReplayFixture` the offline
replay harness drives. Used by two callers:

* the offline capture step that writes the committed
  ``tests/fixtures/sessions/<id>/fixture.json`` fixtures, and
* the live ``POST /sessions/{id}/replay`` endpoint behind the per-session
  page's Replay button (replays the session's *current* persisted transcripts).

The reconstruction keys on the **decision row as the per-turn spine**: each
``agent_decisions`` row already carries its heard STT text (the ``is_current``
entry in ``input_window.transcript_window``, Johnny-ckz.28.4), its router output,
and a link to the utterance it produced (``agent_utterances.agent_decision_id``).
That avoids fragile positional pairing of transcript_chunks against decisions
(real sessions have backfilled / extra decision rows that don't line up 1:1).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AgentDecision, AgentUtterance, BotSession
from johnny.smoketest.replay import ReplayFixture, fixture_from_dict


def _heard_from_input_window(input_window: dict[str, Any] | None) -> tuple[str, float]:
    """Pull the (text, confidence) of the turn's heard utterance.

    The router prompt's rolling window marks the turn under decision with
    ``is_current: true`` (Johnny-ckz.28.4). Returns ``("", 0.9)`` when the
    window is absent (pre-.28.4 rows) so the caller can skip the turn.
    """
    if not isinstance(input_window, dict):
        return "", 0.9
    window = input_window.get("transcript_window")
    if not isinstance(window, list):
        return "", 0.9
    current = [e for e in window if isinstance(e, dict) and e.get("is_current")]
    chosen = current[-1] if current else (window[-1] if window else None)
    if not isinstance(chosen, dict):
        return "", 0.9
    text = str(chosen.get("text") or "")
    conf = chosen.get("confidence")
    return text, float(conf) if isinstance(conf, (int, float)) else 0.9


def _session_run_config(decisions: list[AgentDecision]) -> dict[str, Any]:
    """Recover mode / instructions / threshold / allowed_replies from a decision.

    The first decision's ``input_window`` snapshots the run config the router
    actually saw — more reliable than re-deriving it from playground_overrides.
    """
    for d in decisions:
        iw = d.input_window if isinstance(d.input_window, dict) else {}
        if iw:
            return {
                "mode": str(iw.get("mode") or "autonomous"),
                "instructions": str(iw.get("instructions") or ""),
                "confidence_threshold": float(
                    iw.get("confidence_threshold") or 0.7
                ),
                "allowed_replies": list(iw.get("allowed_replies") or []),
            }
    return {
        "mode": "autonomous",
        "instructions": "",
        "confidence_threshold": 0.7,
        "allowed_replies": [],
    }


def build_replay_fixture_dict(
    session: BotSession,
    decisions: list[AgentDecision],
    utterances: list[AgentUtterance],
) -> dict[str, Any]:
    """Map persisted rows to the on-disk ``fixture.json`` dict shape (pure)."""
    overrides = session.playground_overrides or {}
    runtime = str(overrides.get("pipeline_mode") or "split")
    answer_by_decision: dict[int, str] = {}
    for u in utterances:
        if u.agent_decision_id is not None and u.agent_decision_id not in answer_by_decision:
            answer_by_decision[u.agent_decision_id] = u.output_text

    run = _session_run_config(decisions)
    turns: list[dict[str, Any]] = []
    for d in decisions:
        heard, confidence = _heard_from_input_window(d.input_window)
        if not heard:
            # No reconstructable heard text — a backfilled / malformed row that
            # can't be replayed as a turn. Skip it rather than invent input.
            continue
        spoke_text = answer_by_decision.get(d.id)
        terminal_state = d.terminal_state.value if d.terminal_state else None
        turns.append(
            {
                "text": heard,
                "confidence": confidence,
                "router": {
                    "should_speak": bool(d.should_speak),
                    "confidence": float(d.confidence),
                    "reason": d.reason or "",
                    "reply_type": d.reply_type,
                    "suggested_reply": d.suggested_reply,
                },
                "answer": spoke_text if d.should_speak else None,
                "recorded": {
                    "should_speak": bool(d.should_speak),
                    "terminal_state": terminal_state,
                    "outcome": d.outcome.value if d.outcome else None,
                    "spoke_text": spoke_text,
                },
            }
        )

    return {
        "session_id": str(session.id),
        "label": f"session-{session.id}-{session.source.value}",
        "runtime": runtime if runtime in ("split", "unified") else "split",
        "mode": run["mode"],
        "confidence_threshold": run["confidence_threshold"],
        "allowed_replies": run["allowed_replies"],
        "instructions": run["instructions"],
        "turns": turns,
    }


def load_replay_fixture(db: Session, bot_session_id: int) -> ReplayFixture | None:
    """Load a persisted session and reconstruct its :class:`ReplayFixture`.

    Returns ``None`` when the session does not exist. Orders decisions by
    emission time (``created_at``, id) so the replay's 1-based turn ids line up
    with the original conversation order.
    """
    session = db.get(BotSession, bot_session_id)
    if session is None:
        return None
    decisions = list(
        db.scalars(
            select(AgentDecision)
            .where(AgentDecision.bot_session_id == bot_session_id)
            .order_by(AgentDecision.created_at.asc(), AgentDecision.id.asc())
        ).all()
    )
    utterances = list(
        db.scalars(
            select(AgentUtterance)
            .where(AgentUtterance.bot_session_id == bot_session_id)
            .order_by(AgentUtterance.created_at.asc(), AgentUtterance.id.asc())
        ).all()
    )
    return fixture_from_dict(
        build_replay_fixture_dict(session, decisions, utterances)
    )


__all__ = ["build_replay_fixture_dict", "load_replay_fixture"]
