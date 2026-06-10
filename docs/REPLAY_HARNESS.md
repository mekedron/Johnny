# Offline replay harness

> Feed any persisted session's transcripts back through the **real** voice
> pipeline, capture every event it emits, and assert that the redesign
> invariants still hold — without re-running a live Google Meet.

Before this harness, every pipeline change shipped on hope: the operator ran a
fresh Meet, watched the bot, and formed a judgement from a single session. The
[pipeline decision redesign](PIPELINE.md) (Johnny-ckz.28.2 / .28.3) rests on two
claims — *no transcribed turn is ever silently dropped*, and *the chat and the
decisions panel can never disagree about a turn* — that are only verifiable by
running past sessions through the new pipeline and confirming the old failure
modes are gone **and** the previously-working sessions still work.

The replay harness is that verification, and it runs in CI on every PR.

---

## TL;DR

```bash
# Replay one fixture and assert the invariants (the CI gate):
uv run johnny-replay --session-id 14 --mode invariants

# Replay every committed fixture:
uv run johnny-replay --all --mode invariants

# Diff a replayed session against what it originally recorded (manual review):
uv run johnny-replay --session-id 3 --mode regression
```

In the UI: open any session at `/sessions/<id>` and click **Replay** — it
re-runs that session's persisted transcripts through the pipeline and shows the
invariant verdict plus a per-turn diff against what was recorded.

---

## What it does

Given a saved session, the harness:

1. **Loads the recorded turns** — each `agent_decisions` row carries its heard
   STT text (the `is_current` entry of `input_window.transcript_window`), its
   router output (`should_speak` / `confidence` / `suggested_reply`), and a link
   to the utterance it produced. That decision row is the per-turn spine, so the
   reconstruction never has to fragilely pair `transcript_chunks` against
   `agent_decisions` positionally.
2. **Drives the real pipeline** against fake STT / fake TTS adapters and the
   **recorded** LLM outputs (so runs are deterministic and CI-safe). For a
   `split` session it synthesises one VAD-detectable tone burst per turn, lets
   the real `EnergyVAD` segment them, and the fake STT returns the recorded
   transcript for each segment — the same `router → answer → terminal` path a
   live meeting takes. For a `unified` session it drives the real
   `UnifiedVoicePipeline` through a recorded S2S provider.
3. **Captures every pipeline event** (`transcript_finalized`,
   `router_decision_made`, `agent_spoke`, `turn_terminal`, …) on an in-memory
   event bus — no database, no Redis.
4. **Asserts** one of two things, depending on `--mode`.

### Why drive synthetic audio instead of injecting text?

The silent-drop bug lived in the concurrency between the transcribe and response
loops, and the per-turn `turn_id` correlation the invariants key on is only
assigned on the audio path — `pipeline.feed_text()` leaves every injected turn at
`turn_id=0`. Driving the audio path reproduces the live turn-by-turn flow
faithfully, with proper per-turn ids.

---

## The two modes

### `--mode invariants` (the CI gate)

Asserts the redesign invariants over the captured event stream. Exits non-zero
on any violation.

| Invariant | Runtime | Statement |
| --- | --- | --- |
| **INV-1** | split | Every turn that reached the router emits **exactly one** `turn_terminal`; a `no_reply` terminal names its suppressor. A decided turn with no terminal is the silent drop the invariant forbids. |
| **INV-2** | split | The chat and the decisions panel cannot diverge in **existence**: every `agent_spoke` traces to a `should_speak` decision and a `replied` terminal, the counts match, and no `replied` terminal lacks a spoken utterance. (Text *rephrase* between the recommended and spoken reply is allowed — the answer LLM is a second call — and is reconciled by the subscriber's ORM parity guard, covered by `test_decision_parity.py`.) |
| **INV-U** | unified | The unified analogue of "no turn vanishes": every assistant response the S2S model produced reaches the user as exactly one `agent_spoke` (existence parity between assistant transcripts and spoken utterances). The unified pipeline has no router/terminal spine, so INV-1/INV-2 don't apply. |

### `--mode regression` (manual review)

Diffs the replayed per-turn outcome (`should_speak`, `terminal_state`,
`outcome`, `spoke_text`) against what the session **originally recorded**,
field by field. This is the "did my refactor change a session that used to
work?" mode. It reports divergences but does not fail the run.

A divergence is not always a regression — sometimes it is the **fix** showing up.
Replaying `session 14` (the flagship silent drop) in regression mode reports:

```
turn 4 · terminal_state: recorded=None → replayed='no_reply'
turn 4 · outcome:        recorded=None → replayed='suppressed'
```

i.e. the turn that *vanished* in the original run now ends in a durable
`no_reply(stage_error)` — the proof the [.28.3 router-timeout fix](PIPELINE.md)
closed the gap.

---

## Fixtures

Committed fixtures live at `backend/tests/fixtures/sessions/<id>/fixture.json`
(see the [fixture README](../backend/tests/fixtures/sessions/README.md) for the
per-fixture contract). The harness ships five, covering both runtimes:

| Fixture | Runtime | What it proves |
| --- | --- | --- |
| `14/` | split | The flagship session-14 silent drop. Turn 4's router hangs (`"simulate": "timeout"`); with the timeout fix in place the turn now terminates cleanly. Reconstructed from the [.28.1 forensic analysis](../tasks/prd-pipeline-decision-revision.md). |
| `3/` | split | A real known-good browser session captured from the DB — proves the harness reproduces a real session's decisions + utterances and the invariants hold on real data. |
| `unified-demo/` | unified | A hand-authored unified-S2S conversation (no real unified session existed in the DB) — proves the unified pipeline never drops an assistant utterance. |
| `delegation-calendar/` | split | **Phase-3 verdict-parity baseline** (Johnny-trt.3): delegation- and status-shaped asks addressed to the bot ("can you check our calendar for upcoming meetings?", "are you still working on that?") with small-talk pivots. Must replay with **zero** divergence from its recorded verdicts. |
| `delegation-smalltalk/` | split | The negative half of the parity baseline: the same delegation/status *phrasing* addressed to humans (router declines), plain small talk, and a retracted ask suppressed by the confidence threshold (`no_reply/low_confidence`). |

The two `delegation-*` fixtures were added **before** the Phase-3 router triage
refactor (epic Johnny-trt) extends the router schema with `action`/`task`
fields: their router payloads are deliberately old-format, and
`test_replay_harness_agent.py::test_delegation_baseline_zero_verdict_drift`
asserts a zero-diff replay — the drift guard that proves old model outputs
keep parsing identically across the schema change. Do not regenerate their
`recorded` blocks to make a failing run pass; a diff there is the regression
the gate exists to catch.

### The fixture format

```jsonc
{
  "session_id": "14",
  "label": "session-14-silent-drop",
  "runtime": "split",            // "split" | "unified"
  "mode": "autonomous",
  "confidence_threshold": 0.7,
  "allowed_replies": [],
  "instructions": "…",
  "turns": [
    {
      "text": "the heard STT transcript for this turn",
      "confidence": 0.9,
      "router": {                // recorded router structured output
        "should_speak": true,
        "confidence": 0.92,
        "reason": "…",
        "reply_type": "empathetic_response",
        "suggested_reply": "what the router recommended saying"
      },
      "answer": "what the answer LLM actually produced (null if the router declined)",
      "simulate": "timeout",     // optional: reproduce the session-14 router hang
      "recorded": {              // what was ORIGINALLY persisted, for regression diffing
        "should_speak": true,
        "terminal_state": "replied",
        "outcome": "spoken",
        "spoke_text": "…"
      }
    }
  ]
}
```

### Capturing a new fixture

`app.services.replay_session.build_replay_fixture_dict` maps a live session's
rows to this shape. To dump one from the running stack:

```python
# inside the api container (docker compose exec api python)
from app.db.session import SessionLocal
from app.db.models import BotSession, AgentDecision, AgentUtterance
from app.services.replay_session import build_replay_fixture_dict
from sqlalchemy import select
import json

SID = 3
with SessionLocal() as db:
    s = db.get(BotSession, SID)
    decs = list(db.scalars(select(AgentDecision)
        .where(AgentDecision.bot_session_id == SID)
        .order_by(AgentDecision.created_at.asc(), AgentDecision.id.asc())).all())
    utts = list(db.scalars(select(AgentUtterance)
        .where(AgentUtterance.bot_session_id == SID)
        .order_by(AgentUtterance.created_at.asc(), AgentUtterance.id.asc())).all())
    print(json.dumps(build_replay_fixture_dict(s, decs, utts), indent=2))
```

Write the result to `backend/tests/fixtures/sessions/<id>/fixture.json` and the
CI parametrised test picks it up automatically.

---

## CI wiring

`backend/tests/smoketest/test_replay_harness.py` (unified fixtures on
`UnifiedVoicePipeline`) and `test_replay_harness_agent.py` (split fixtures on
the LiveKit-Agents engine) run as part of the normal test suite and:

- replay **every** committed fixture through `--mode invariants` and fail the
  build on any violation,
- assert a **zero-divergence** regression diff for the `delegation-*`
  verdict-parity fixtures (the Phase-3 drift guard), and
- prove the invariant checker has teeth — hand-crafted event streams that
  violate INV-1 / INV-2 / INV-U are asserted to be flagged, so a checker that
  always returned "no violations" could never pass silently.

```bash
docker compose exec api pytest tests/smoketest/test_replay_harness.py tests/smoketest/test_replay_harness_agent.py
```

---

## The UI Replay button

Every session detail page (`/sessions/<id>`) has a **Replay** panel above the
reasoning timeline. Clicking **Replay** calls `POST /sessions/{id}/replay`, which
reconstructs the fixture from the session's *current* persisted transcripts,
drives the real pipeline, and returns:

- the invariant verdict (green "invariants hold" / red with the specific
  violations), and
- a per-turn table diffing the **recorded** outcome against the **replayed**
  one, with changed fields highlighted and a "SPOKE INSTEAD" badge on turns
  where the spoken text diverged from the router's recommendation.

This lets the operator iterate on prompt / config changes against the same
session without re-running a live Meet.

---

## Architecture at a glance

```
fixture.json ─┐
              ├─► run_replay() ─► real the retired split engine / UnifiedVoicePipeline
DB session  ──┘      │                    │  (fake STT/TTS, recorded LLM/S2S)
 (load_replay_       │                    ▼
  fixture)           │            InMemoryEventBus  ──►  captured events
                     ▼                                        │
            assemble_turns() ◄──────────────────────────────┘
                     │
        ┌────────────┴─────────────┐
        ▼                          ▼
  check_invariants()       diff_against_recorded()
   (INV-1/2/U)               (replayed vs recorded)
```

- **Pure half** (`johnny/smoketest/replay.py`): the fixture model,
  `assemble_turns`, `check_invariants`, `diff_against_recorded` — no providers,
  no DB.
- **Driving half** (same module): the fake providers + `run_replay`, which spin
  the real pipeline.
- **DB bridge** (`app/services/replay_session.py`): turns a persisted session
  into a `ReplayFixture` — shared by the capture step and the live endpoint.
- **CLI** (`johnny/smoketest/replay_cli.py`): the `johnny-replay` entrypoint,
  modelled on `johnny-tts-smoke`.

See [docs/PIPELINE.md](PIPELINE.md) for the pipeline internals the harness
exercises, and [docs/LATENCY.md](LATENCY.md) for the adjacent latency-capture
tooling.
