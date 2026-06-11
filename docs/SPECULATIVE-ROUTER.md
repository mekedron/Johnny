# Speculative-parallel router — the preemptive-generation decision (Johnny-trt.20)

**Decision: deferred — nothing ships in this epic.** If speculation is ever
revived, revive it as the **gate-owned design in §4**, never as the SDK's
`preemptive_generation` (§2 documents why that toggle is structurally unfit
for a gated session, beyond the one-line correlation note in
`session.py`). Until a reopen trigger fires (§6), `preemptive_generation`
stays `False` on every session that carries a `RouterGate`, and the answer
prompt stays verdict-independent (§7).

This doc records the verified SDK mechanics, the correlation-safe design a
future bead would build, the token/throughput cost model, and the cancel-safe
terminal story — the trt.22 (`docs/TASK-ENGINE.md`) decision-doc pattern.

- Spike bead: Johnny-trt.20 (design-only; no behavior change, no prototype)
- SDK verified hands-on: `livekit-agents==1.5.17` inside the api image
  (`/opt/venv/.../livekit/agents/voice/agent_activity.py`,
  `audio_recognition.py`) — line refs below are to that version
- Johnny anchors: `backend/johnny/agent/router_gate.py` (gate, FIFO,
  say-with-terminal), `backend/johnny/agent/session.py:283,317-325`
  (the `preemptive_generation=False` contract), `docs/ROUTING.md` (§2 flow,
  §6 trt.51 decisions), `docs/LATENCY.md` (Phase-3 capstone numbers)

## 1. The question

Felt latency on a speak turn is serial today: endpointing commit → router LLM
(triage) → answer LLM → TTS. The spike question: start the answer LLM
*concurrently* with triage and cancel it when the verdict is not SPEAK — is
there a correlation-safe design, and is it worth it?

Two candidate shapes exist: the SDK's built-in `preemptive_generation`
(speculates *pre-commit*, on STT finals) and a Johnny-native gate-owned race
(speculates *at commit*, inside `run_turn`). They differ in lead time,
blast radius, and correlation safety.

## 2. Why SDK `preemptive_generation` is unfit for a gated session

Verified mechanics in 1.5.17, in firing order:

1. **Trigger**: `on_preemptive_generation` fires from audio recognition on
   *every final-transcript append* (`audio_recognition.py:918,971`), i.e.
   pre-commit. Each new final cancels and restarts the speculation — a
   multi-final utterance burns several partial generations before the turn
   even commits. Guards: skipped while a non-interrupted speech is playing,
   `max_speech_duration`, `max_retries`.
2. **`speech_created` fires immediately**: the trigger calls
   `_generate_reply(schedule_speech=False)`, which emits
   `speech_created(source="generate_reply")` unconditionally
   (`agent_activity.py:1207`) — *before* the turn commits and *before*
   `on_user_turn_completed` (the gate hook) runs. The pipeline task starts
   LLM inference immediately and parks at
   `speech_handle._wait_for_scheduled()`; chat-ctx insertion happens only
   `if speech_handle.scheduled`, so a cancelled speculation never pollutes
   the context (the one piece that is safe).
3. **Reuse fires no second `speech_created`**: after the hook returns
   normally, the SDK reuses the parked handle iff transcript, chat-ctx
   equivalence, tools, and tool_choice all match
   (`agent_activity.py:2086-2095`) — and schedules it via
   `_schedule_speech` with **no new event**. Invalidation cancels the handle
   and falls through to a fresh `_generate_reply` (which does emit again).
4. **`StopResponse` leaks the speculation**: the hook raising `StopResponse`
   hits `except StopResponse: return` — `self._preemptive_generation` is
   *not* cancelled there. The speculative generation runs to completion in
   the background and is only swept by the *next* preemptive/turn event. On
   a decline-dominant surface that is a full answer generation burned per
   declined turn, with no cancel-at-verdict.
5. **Reuse swaps the user-message identity**: the scheduled speech carries
   `preemptive.user_message` (built in the trigger), not the hook's
   `new_message` (SDK comment at `agent_activity.py:2097`) — a different
   message id than the one the gate keyed the turn on.

Against Johnny's correlation machinery this breaks as follows. The gate's
speak path is a FIFO: `run_turn` pushes the turn id on SPEAK
(`router_gate.py:678`), the session `speech_created` listener pops it in
`bind_reply` (`router_gate.py:1375`). That FIFO is sound today because the
push (end of the hook) and the pop (the `_generate_reply` emit) happen with
no intervening await in `_user_turn_completed_task` — effectively atomic on
the event loop. With preemptive enabled:

- the pre-gate `speech_created` (2) arrives while the FIFO is empty →
  `bind_reply` ignores the speculative handle (plus runs its
  start-of-speech buffer hygiene at a moment no speech is starting);
- on reuse (3) **no event fires after the push** → the SPEAK turn never
  binds → no terminal when the reply completes (an INV-1 hole until the
  `on_exit` sweep) → **and the stale FIFO entry mis-binds the next turn's
  reply** — an off-by-one skew that corrupts every subsequent turn's
  terminal, spoken-text stamping (INV-2/trt.54), and barge-in attribution
  for the rest of the session.

A local fix exists (bind at done-time keyed on `handle.scheduled`, instead
of at create-time), but it still leaves (1) per-final churn, (4) the
StopResponse leak — uncancellable without reaching into the activity's
private `_preemptive_generation` — and (5) the identity swap. All three are
upstream semantics, version-coupled to SDK internals. **Conclusion: do not
build on `preemptive_generation`; the `False` contract in
`build_agent_session` is permanent for gated sessions.**

## 3. The geometry: where the overlap window actually is

Phase-3 capstone, canonical all-local trio, p50 ms (`docs/LATENCY.md`):
commit_wait 405 → router 1664 → answer 660 (TTFT == total until Johnny-dny
lands streaming) → tts_ttfb 87 → felt e2e 2749.

- The SDK's pre-commit head start (STT final leads VAD-end by ~107 ms;
  commit trails speech-end by ~405 ms) is worth ≈ **512 ms** of extra lead.
- Johnny's router span is **1664 ms p50 — 2.5× the whole answer stage**.
  Speculation started *at commit* already finishes the answer (660 ms)
  well inside the router span, with ~1 s to spare; at grown context
  (router 4–5 s, answer 3–4 s) it still fits.

So the pre-commit head start the SDK design exists to capture is worthless
here: **the router IS the overlap window.** A gate-owned race loses nothing
measurable versus SDK preemptive, runs once per committed turn instead of
once per STT final, and never touches the session until the gate itself
decides to speak. Upper bound (uncontended backends): felt 2749 → ~2156 p50,
**−593 ms ≈ −22 %**. After Johnny-dny (answer streaming), the saving shrinks
to the answer's time-to-first-sentence only (~300–450 ms local, ~200–400 ms
cloud) — dny captures the rest of the same win for zero burn.

## 4. The correlation-safe design (gate-owned; build this if ever revived)

Speculation lives entirely inside `RouterGate.run_turn`, through Johnny's
provider layer — the session, ledger, and FIFO never see it until commit.

1. **Enable condition** (`RouterGateConfig.speculative_answer`, default
   off): free-form `autonomous` mode only (no allowed-reply coercion to
   reimplement), `say` attached, and **router backend ≠ answer backend**
   (operator-asserted; per-agent model slots trt.41/42 make it a real
   config property). All-local single-backend configs must never enable it
   (§5).
2. **Spawn**: after the mode guards, before `run_gate`, start a provider
   task: `stream_chat` on the *answer* provider with messages built exactly
   as the answer path builds them (instructions + chat ctx + new message —
   verdict-independent by construction, see §7), tee'd into a sentence
   buffer (`iter_sentences` reuse).
3. **Every non-SPEAK leg cancels**: declined / low-confidence / rate-limited
   / suggest-only / approval / delegate / status / capability-decline /
   gate-timeout / barge-in — one `spec.cancel()` on the way out (the
   provider stream closes; Ollama and OpenAI-compatible backends stop
   generating server-side). The speculation has **zero observable side
   effects** at that point: no speech, no chat-ctx item, no event, no row.
   The leg's existing terminal is untouched — the cancel-safe terminal
   story is "there is nothing to make safe," by construction.
4. **The SPEAK leg is the single commit point.** If the buffer holds ≥ 1
   sentence (or the stream is healthy):
   - insert the user message into the live ctx via the public
     `Agent.update_chat_ctx()` (user-before-assistant ordering — see the
     adjacent gap note below);
   - speak via the existing say-with-terminal machinery with
     `kind="reply"` and the **stream** (`say()` accepts
     `AsyncIterable[str]`, `agent_activity.py:1055`): relay buffered
     sentences, then live tokens. TTS sentence-flush, the caption tee
     (trt.58 partials), interim captions, rate-limit accounting, and the
     `replied`/`no_reply(barge_in)` terminal all ride `_say_with_terminal`
     /`_on_say_done` unchanged — the same engine acks and status lines use
     today;
   - raise `StopResponse` (the SDK generates nothing; the FIFO is never
     involved).
   Otherwise (speculation errored / empty / not yet streaming): **fall
   through to today's SPEAK** — push the FIFO, return normally, let the SDK
   generate. Speculation is an accelerator, never a dependency; its failure
   mode is the status quo.
5. **Observability with zero schema work**: a `speculative` marker in
   `decision.raw` (the trt.50 ride-along: used/cancelled + lead-ms) and a
   gate-emitted `answer_llm` timing row (the trt.19 gate-emitted pattern —
   LiveKit emits no metric for provider-layer side calls).

Build-time details a future bead must settle (none are blockers): widen the
`SaySpeech` seam and `_say_with_terminal` to accept a stream and materialize
the full text for `AgentSpoke` from the relay tee (not the caption buffer);
WAV-flush parity for say-path `kind="reply"` audio (today only FIFO replies
persist WAVs); confirm `say(add_to_chat_ctx=True)` insertion semantics on an
interrupted speech; ensure barge-in on the say handle cancels the upstream
provider stream (relay generator finalization).

**Adjacent gap discovered (pre-existing, orthogonal):** every
`StopResponse` turn — declined *and* delegate/status/capability-decline —
returns from `_user_turn_completed_task` before the SDK's chat-ctx insert,
so the user's utterance never lands in the **live** ctx (the ack does, via
`say(add_to_chat_ctx=True)`). The DB transcript rows are gate-independent
(`stt_node` emits `TranscriptFinalized` for every kept final), so respawn
rehydration restores them — a continuously-running bot has *less* live
context than a respawned one. Filed separately; the design above must not
replicate that gap for actually-replied turns, hence the explicit
`update_chat_ctx` step in (4).

## 5. Token / throughput cost model

**Hit rate.** Speculation pays off only on turns that end in a spoken
reply. Post-restraint playground rows (78, the trt.51 dataset): speak 59
(76 %), delegate 18, silent 1, low-confidence 4 → ~29 % of speculations
burned even on the *speak-friendliest* surface. The bead's target surface —
meetings — is decline-dominant by design (the router's purpose is restraint
through multi-party talk): at a few spoken replies per hour against
~300–600 gate-decided turns, burn is structurally **≥ 80–90 %**. No
post-restraint meeting-mode decision rows exist yet to measure it (the
trt.51 dataset is all-browser) — sizing this is a reopen precondition.

**All-local (the canonical operator config): provably no-gain.** Both calls
hit one Ollama. Serialized (`NUM_PARALLEL=1`): the speculative answer (660
ms) runs first → the verdict lands ~660 ms later on **every** turn; on
speak turns the buffered answer merely cancels out the delay (audio time
unchanged within noise), while delegate acks — already 91 % triage-bound
against a ≤ 2 s target — get strictly worse, and silent turns burn GPU for
nothing. Parallel decode (`NUM_PARALLEL≥2`): both spans stretch under
shared throughput; same conclusion. There is no parameter setting under
which same-backend speculation wins.

**Split backends (local router + cloud answer): cheap in dollars,
structurally wasteful.** Burned prompt tokens dominate (the full chat
history rides every speculation; output is ≤ ~80 tokens and completes
before the verdict, so cancel-at-verdict reclaims nothing):
~0.3–1.7 M prompt tokens per meeting-hour ≈ $0.05–0.26/h at mini-class
pricing, $0.8–5/h at 4o-class. Burn scales with meeting length twice over —
more turns *and* longer context per turn.

**The burn-control knob, if revived: shadow-gated speculation.** Speculate
only when the trt.50 heuristic predicts speak (SIMPLE, no catalog
dimension). Playground numbers: SIMPLE share ~42 % of turns, SIMPLE×speak
agreement 94 % → burn collapses to ~6 % of speculated turns; COMPLEX-speak
turns just fall back to sequential (no harm, no gain). Unlike the rejected
trt.51 fast-path, a shadow miss here costs only a wasted/missed speculation,
never a wrong action — the verdict still routes every turn ("Quit." scoring
SIMPLE burns one speculation; it still delegates to `session.end`). The
shadow's meeting-mode discrimination is unvalidated — same data gap as
above.

## 6. Verdict and reopen triggers

**Deferred**, for four ranked reasons:

1. **Wrong order versus Johnny-dny.** Answer streaming is the measured #1
   latency candidate and captures most of the same felt win with zero burn
   and zero new machinery. Speculation's residual value post-dny is the
   answer TTFS only (~300–450 ms), on speak turns only.
2. **The operator's canonical config is all-local** — provably
   no-gain-to-harmful (§5).
3. **The target surface maximizes burn** and the data to size the
   shadow-gating knob (meeting-mode decision rows) does not exist yet.
4. **The overlap window is shrinking by design**: trt.59 already slimmed
   the no-catalog schema; trt.41/42 small-router slots attack the same
   span. Every ms cut from triage shrinks what speculation can hide.

Reopen when **all** hold: (1) Johnny-dny has landed and speak-turn answer
TTFS still ≥ ~300 ms p50 in the operator's live config; (2) trt.41/42 have
landed with router and answer on genuinely disjoint backends in that
config; (3) ≥ 100 post-restraint meeting-mode decision rows exist and the
measured speak rate (or a meeting-validated shadow gate) bounds burn at an
operator-acceptable level; (4) the felt-latency target is still missed *at
the answer stage* after all of the above. Then build §4 verbatim.

## 7. Constraints this decision freezes

- `preemptive_generation` stays `False` on any session with a `RouterGate`
  (`build_agent_session`); this doc is the full mechanics behind that
  contract.
- **The answer prompt must remain verdict-independent** — built from
  instructions + chat ctx + the user message only, never from router
  output. Today this holds (trt.55 capability notes are static
  per-session). Any future per-turn coupling (a verdict-informed answer
  prompt, a router-authored answer hint) forecloses speculation entirely —
  weigh that explicitly if such a coupling is proposed.
- Task-catalog / delegation work never speculates: a delegate verdict's
  spoken ack is say-path and pays no answer hop (trt.17) — speculation
  cannot help it, only burn alongside it.
