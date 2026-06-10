# Session replay fixtures

One directory per replayable session: `<id>/fixture.json`. The format, the
capture workflow, and the harness internals are documented in
[docs/REPLAY_HARNESS.md](../../../../docs/REPLAY_HARNESS.md). Fixtures are
discovered automatically (`discover_fixtures` globs `*/fixture.json`):
committing a new directory adds it to `johnny-replay --all` and to the
parametrised CI gates — `tests/smoketest/test_replay_harness.py` replays the
unified (S2S) fixtures on `UnifiedVoicePipeline`,
`tests/smoketest/test_replay_harness_agent.py` replays the split fixtures on
the LiveKit-Agents engine (RouterGate + TurnLedger).

| Fixture | Runtime | What it pins |
| --- | --- | --- |
| `14/` | split | The flagship session-14 silent drop: turn 4's router hangs (`"simulate": "timeout"`) and must terminate in a durable `no_reply(stage_error)`. |
| `3/` | split | A real known-good browser session captured from the DB — proves the harness reproduces real per-turn decisions + utterances. |
| `unified-demo/` | unified | Hand-authored unified-S2S conversation — proves the unified pipeline never drops an assistant utterance (INV-U). |
| `delegation-calendar/` | split | **Phase-3 parity baseline** — delegation- and status-shaped asks addressed to the bot, with small-talk pivots. |
| `delegation-smalltalk/` | split | **Phase-3 parity baseline** — the negative half: delegation-/status-*shaped* utterances addressed to humans, plus a low-confidence suppression. |

## Phase-3 verdict-parity baseline (`delegation-calendar`, `delegation-smalltalk`)

These two fixtures are the **parity baseline for the Phase-3 router triage
refactor** (epic Johnny-trt; schema extension Johnny-trt.16, fixtures added in
Johnny-trt.3 *before* any schema change). Phase 3 extends the router schema
with `action: [silent|speak|delegate|status]` and a nullable `task` object —
exactly the utterance shapes these fixtures cover:

- delegation-shaped asks addressed to the bot — *"Hey Johnny, can you check our
  calendar for upcoming meetings?"*, *"could you go through the inbox…"*;
- status queries about the bot's in-flight work — *"Johnny, are you still
  working on that?"*, *"any update on the vendor email?"*;
- the **negative** shapes that must keep their no-speak verdicts: a
  calendar-check request addressed to a human (*"Bob, can you check the
  calendar…"*), a status question aimed at teammates (*"are you guys still
  working on the migration ticket?"*), plain small talk, and a retracted ask
  the router approves at confidence 0.55 that the gate's 0.7 threshold
  suppresses (`no_reply/low_confidence`).

Their `router` payloads are deliberately **old-format** —
`{should_speak, confidence, reason, reply_type, suggested_reply}` with no
`action`/`task` fields (`delegation-calendar` turn 7 omits the optional fields
entirely to pin the parser defaults). The `recorded` blocks pin what the
agent engine on current main does with those payloads, captured from a
zero-divergence replay run.

The drift guard is
`tests/smoketest/test_replay_harness_agent.py::test_delegation_baseline_zero_verdict_drift`:
it replays both fixtures and asserts `diff_against_recorded` returns **zero**
divergences (plus the suppression *reasons* — `router_declined` vs
`low_confidence` — stay distinct). The Phase-3 parser must keep parsing these
old-format outputs byte-for-byte identically, so:

> **Do not regenerate the `recorded` blocks to make a failing run pass.** A
> diff against this baseline means the router parser or the gate changed
> behaviour for old model outputs — that is the regression the epic forbids,
> not a stale fixture.
