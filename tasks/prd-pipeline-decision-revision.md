# PRD: Pipeline decision↔utterance revision — kill silent drops and decision divergence

> **Status:** analysis + design (Johnny-ckz.28.1). **No code in this task.** This document is the
> input gate for sub-tasks Johnny-ckz.28.2 / .3 / .4 / .5 — none may start until the operator has
> reviewed it.
>
> **Source of truth:** session `http://localhost:5173/sessions/14`, reconstructed from the persisted
> `bot_sessions`, `transcript_chunks`, `agent_decisions`, `agent_utterances`, and `session_timings`
> tables (see `backend/app/db/models.py`). Every number and quote below is read back from the DB,
> not hand-authored.
>
> **No UI surface is changed by this task**, so per the project's browser-validation rule there is
> nothing to drive in chrome-devtools here; validation is mandatory for the *implementation*
> sub-tasks (.2–.5), not for this analysis pass.

---

## Section A — Session 14 forensic timeline

### A.0 Session header

| Field | Value |
| --- | --- |
| `bot_sessions.id` | 14 |
| `source` | `browser` (playground) |
| `status` | `ended` (clean close, `error_reason` NULL) |
| `started_at` | 2026-06-07 17:35:43.898 UTC |
| `ended_at` | 2026-06-07 17:37:45.546 UTC (≈ 121.6 s) |
| run mode | `free_auto_speak` (the router `input_window.mode`; this `BotMode` was later consolidated into `autonomous` by commit `0c66baca`, Johnny-ckz.25) |
| `confidence_threshold` | 0.7 |
| `allowed_replies` | `[]` (free-form) |
| instructions | default playground prompt: *"Respond directly without any speaker label, bot name, role prefix, or text before the actual message."* |
| `playground_overrides` | `{persona: "Concise, friendly conversation partner.", pipeline_mode: "split", system_prompt: <default>, calendar_event_id: null}` |
| row counts | 4 transcripts · 3 decisions · 1 utterance · 14 timings |

The asymmetry **4 transcribed turns → 3 router decisions → 1 spoken utterance** is the whole bug
surface in one line: one user turn never produced a decision at all (silent drop), and of the turns
that did, the one that spoke said something other than what the decision recorded (divergence).

### A.1 Per-turn timeline

Offsets are ms from `started_at`. "Decision" = `agent_decisions`; "Spoke" = `agent_utterances`.

| Turn | t (offset) | User utterance (STT final, `transcript_chunks`) | Router decision (`agent_decisions`) | Guard / gate | Bot spoke (`agent_utterances`) | Tag |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 6.3 s | id=29 *"Hey, can you send me something? Because I'm just testing all the things here."* | id=27 `should_speak=false` conf 0.15 → `suppressed`. Reason: "user is testing functionality, no specific request… no prior utterances to reference." | router `should_speak=false` short-circuit (no confidence gate reached) | — (nothing) | `gate-suppression` (plausibly correct, but invisible) |
| 2 | 20.4 s | id=30 *"Hey, but tell me something."* | id=28 `should_speak=false` conf 0.15 → `suppressed`. Reason: "message is vague… no specific topic or action requested." | router `should_speak=false` short-circuit | — (nothing) | `gate-suppression` (**questionable** — a direct request scored as noise) |
| 3 | 33.6 s | id=31 *"Hey Bot, why are you not talking to me? I need to ensure that someone hears me."* | id=29 `should_speak=true` conf 0.92 reply_type `empathetic_response` → `spoken`. `suggested_reply` = **(R)** below | passes `should_speak` + conf 0.92 ≥ 0.7; not rate-limited; not interrupted | id=13 `output_text` = **(U)** below; `audio_duration_ms`=13479; persisted `mode=listen_only` | `decision-divergence` (+ mode-label mismatch) |
| 4 | 61.6 s | id=32 *"Hey, do you have… some sort of updates for the upcoming week, which you're gonna do with your IT and marketing teams because you're the product owner."* | **none** — no `agent_decisions` row written | router LLM call hung; `session_timings` turn 4 `stage=error` `{failed_stage: router_llm}` `duration_ms=60009` (~60 s) | — (nothing) | `silent-drop` (the flagship failure) |

**(R)** decision id=29 `suggested_reply` (what the decisions panel shows):
> "I'm sorry I've been quiet—your voice matters, and I'm here to listen now. What would you like to
> share or discuss? I'm listening and ready to talk with you."

**(U)** utterance id=13 `output_text` (what the chat shows / what was actually spoken):
> "Because I'm here to listen now, and I just realized I've been quiet. I'm sorry—I didn't want to
> interrupt or miss what you said. You asked me to share or discuss something, and I'm listening and
> ready to talk with you. What would you like to share or discuss?"

(R) and (U) are different paraphrases produced by two different LLM calls. There is no operator-visible
record of why the spoken text differs from the recommended text.

### A.2 Per-turn stage timings (`session_timings`)

| turn | stage | started_at_ms | duration_ms | provider | details |
| --- | --- | --- | --- | --- | --- |
| 1 | stt | 6016 | 276 | parakeet | produced_text=true, audio 4180 ms |
| 1 | router_llm | 6295 | 1108 | openai-compatible | finish_reason=stop |
| 2 | stt | 19975 | 447 | parakeet | produced_text=true, audio 2160 ms |
| 2 | router_llm | 20426 | 1402 | openai-compatible | finish_reason=stop |
| 3 | stt | 33377 | 253 | parakeet | produced_text=true, audio 4860 ms |
| 3 | end_to_end | 33377 | 3392 | — | char_count=68 |
| 3 | router_llm | 33631 | 1986 | openai-compatible | finish_reason=stop |
| 3 | answer_llm | 35618 | 1492 | openai-compatible | char_count=259, ttft 1013 ms, interrupted=false |
| 3 | tts ×4 | 36631–37027 | 83–146 | piper | 4 sentence flushes, ttfa 82–143 ms |
| 4 | stt | 60997 | 575 | parakeet | produced_text=true, audio 12100 ms |
| 4 | **error** | 61574 | **60009** | openai-compatible | **failed_stage=router_llm** |

The turn-4 `error` row begins at 61574 ms and lasts 60009 ms — i.e. the router LLM call hung for ~60 s
(a provider HTTP read-timeout) and the exception surfaced at ≈121.6 s, the same instant the session
ended. The user asked a real question, got zero feedback for a full minute, and the session closed.

### A.3 What the failure tags mean here

- **`silent-drop`** (turn 4): user transcribed, no terminal decision/utterance, no after-the-fact
  operator-visible reason. Only trace is one `session_timings` `error` row.
- **`decision-divergence`** (turn 3): `agent_decisions.suggested_reply` (R) ≠
  `agent_utterances.output_text` (U); the two surfaces read different columns written by different
  LLM calls. Secondary defect: the utterance persisted `mode=listen_only` while the session ran
  `free_auto_speak`.
- **`gate-suppression`** (turns 1–2): the router's own `should_speak=false` / confidence gate dropped
  the turn. Turn 1's drop is defensible (a "just testing" throat-clear); turn 2's is questionable (a
  direct "tell me something" scored 0.15). Either way the participant got **no signal that the bot was
  even listening**, which is what produced turn 3 ("why are you not talking to me?"). There is no
  `ok` turn in this session.

---

## Section B — Failure-mode catalogue

### B.0 The spine: persistence is event-sourced through one subscriber

Every failure below is a consequence of one architectural fact, so it is stated once here.

The in-process pipeline (`VoicePipeline`, `backend/johnny/voice_pipeline/pipeline.py`) **does not write
the database directly**. In production it runs with `NoopDecisionSink` / `NoopUtteranceSink` (constructed
at `pipeline.py:622-623`; confirmed by the `apply_agent_spoke_event` docstring at
`session_status_subscriber.py:290-298` — *"in production the pipeline's `decision_sink.update_outcome`
call is short-circuited by NoopDecisionSink"*). The browser runner wires only `event_bus=spec.event_bus`
(`browser_pipeline_runner.py:282,319`), never SQL sinks. Instead the pipeline **publishes events** to a
Redis `EventBus` (`event_bus.py:68-100`), and the **sole durable writer** is
`session_status_subscriber._apply_in_transaction`, whose dispatch table (`session_status_subscriber.py:444-455`)
handles exactly five event types:

| Event | Handler | Writes |
| --- | --- | --- |
| `session_status` | `apply_status_event` | `bot_sessions.status` |
| `transcript_finalized` | `apply_transcript_event` | `transcript_chunks` |
| `router_decision_made` | `apply_router_decision_event` (`:209-284`) | `agent_decisions` (incl. `outcome`) |
| `agent_spoke` | `apply_agent_spoke_event` (`:287-344`) | `agent_utterances` |
| `pipeline_timing` | `apply_pipeline_timing_event` (`:347-398`) | `session_timings` |

Any other event — **`pipeline_stage_failed`, `agent_suggested`, `transcript_filtered`** — has no branch
and is silently dropped (`applied` stays `False`, no row). The live browser WebSocket
(`api/ws.py:427-442`) forwards *all* events in real time, so a viewer watching live sees the failure;
nothing about it is durable. **The set of event types that earn a durable row is a happy-path-plus-timing
whitelist — failure is not in it.** That is the root of the silent drop, and the event-sourced split is
the root of the divergence and the mode mislabel.

### B.1 `silent-drop` — a failed turn leaves no terminal record

**Pattern.** A transcribed user turn whose router stage raises produces **zero** `agent_decisions` and
`agent_utterances` rows. The only durable trace is a single `session_timings` `stage=error` row. The
decisions panel and chat show nothing; the turn vanishes. Session 14 turn 4 (the product-owner question)
is the exemplar: the router LLM hung ~60 s then raised, and the question was never acknowledged.

**Code path.**

| File | Lines | Symbol | Role |
| --- | --- | --- | --- |
| `pipeline.py` | 1759-1762 | `_run_router` | `router_llm.chat` called with **no `asyncio.wait_for`** → inherits the provider's ~60 s HTTP read-timeout. |
| `pipeline.py` | 1389-1396 | `_classify_barge_in_intent` | The contrast: the *same* `router_llm` IS bounded here by `asyncio.wait_for(barge_in_classifier_timeout_s)`. The main path was never given the same guard. |
| `pipeline.py` | 1763-1777 | `_run_router` except | On exception: emits `error` timing row (`failed_stage=router_llm`), calls `_emit_stage_failed`, then **re-raises**. |
| `pipeline.py` | 1519-1532 | `_respond_to_transcript_inner` | `_run_router` is awaited at 1521; `RouterDecisionMade` is only constructed at 1522. The raise escapes **before** the decision event exists, so it is never published and no `_persist_decision` site (1547/1551/1554/1571/1587/1597) is reached. |
| `pipeline.py` | 772-780 | `run._respond_loop` generic `except` | Terminal catch: `logger.exception` only — no event, no row, no UI signal. |
| `pipeline.py` | 2340-2370 | `_emit_stage_failed` | Publishes `PipelineStageFailed` to the bus (live only). |
| `session_status_subscriber.py` | 444-455 | dispatch table | **No branch for `pipeline_stage_failed`** → the failure event is never persisted. |

**Root cause.** Two compounding defects: (1) the main router call is unbounded, turning a provider stall
into a 60 s dead session turn; (2) the failure has no durable representation — the exception aborts before
`RouterDecisionMade`, and the one failure event that *is* emitted (`PipelineStageFailed`) is dropped by the
subscriber for lack of a dispatch branch. A failed turn is therefore unrepresentable in `agent_decisions`.

**Minimum invariant (`INV-1`, terminal-state-per-turn).** Every transcript dequeued from
`_response_queue` ends in **exactly one** terminal `agent_decisions` row — including `outcome=error` with
a reason — before the response loop moves on. Bound the router call; persist a terminal decision on the
exception path; add a `pipeline_stage_failed` dispatch branch so the structured error is queryable after
the session ends. Impossible-by-construction (a turn cannot leave the loop without a terminal row) is the
target; loud operator visibility is the acceptable fallback.

### B.2 `decision-divergence` — the panel shows one text, the chat another

**Pattern.** `agent_decisions.suggested_reply` (the decisions panel) and `agent_utterances.output_text`
(the chat) are produced by **two independent LLM calls** and never reconciled. Session 14 turn 3: the
panel shows the router's *(R)*, the chat shows the answer LLM's *(U)* — different paraphrases, no
operator-visible reason for the swap. Two secondary defects ride along: an **optimistic outcome** and a
**mode mislabel**.

**Code path.**

| File | Lines | Symbol | Role |
| --- | --- | --- | --- |
| `pipeline.py` | 1521-1533 | `_respond_to_transcript_inner` | Router LLM → `RouterDecisionMade(suggested_reply=…, raw_output=…)` published. |
| `pipeline.py` | 1594, 1820-1863 | `_answer_and_speak` | A **separate** `answer_llm` stream (`_stream_answer_into_tts`, 1839) produces `output_text`; persisted via `_persist_utterance` (1856) → `AgentSpoke`. |
| `pipeline.py` | 2191-2192 | `_answer_messages` | The *only* coupling: `suggested_reply` injected as a soft hint `"Router suggested: …"`. Advisory, not binding — the answer LLM rephrases freely. |
| `session_status_subscriber.py` | 234-245 | `apply_router_decision_event` | `outcome` computed from `should_speak`+`mode`; for `autonomous`/`limited_auto_speak` it is **pre-assigned `SPOKEN` at router time** (240-241), before any answer/TTS happened. |
| `session_status_subscriber.py` | 317-330 | `apply_agent_spoke_event` | Links the utterance to a decision by **`SELECT … should_speak=True ORDER BY id DESC LIMIT 1`** — a most-recent scan, not a causal turn key — and flips `PENDING→SPOKEN`. |
| `session_status_subscriber.py` | 311-316 | `apply_agent_spoke_event` | **Mode default = `BotMode.LISTEN_ONLY`**, overridden only from `session_row.meeting_config.mode`. `AgentSpoke` carries no mode. |
| `models.py` | 314-321, 470 | `BotSession`, `AgentUtterance.agent_decision_id` | Playground sessions have `meeting_config_id = NULL` → `meeting_config` is `None` → mode stays `LISTEN_ONLY` for **every** playground utterance, regardless of the mode actually run. |
| `frontend/src/routes/sessions/[id]/+page.svelte` | 256-272, 1105-1108 | `decisionRecordToEntry`, panel render | Decisions panel renders `suggested_reply`. |
| `frontend/src/routes/sessions/[id]/+page.svelte` | 245-253, 993-1056 | `utteranceToLine`, transcript render | Chat renders `output_text`. The `utteranceMap` join (209-233) only pulls `matched_allowed_reply`; the two texts are never compared. |

**Root cause.** The router's reply and the spoken reply are two events from two LLM calls, stitched after
the fact by a fragile "most-recent should_speak" scan rather than a shared turn key. There is no single
field that means *"what the bot said this turn"*: the panel reads the router's intent, the chat reads the
answer LLM's output, and the UI presents them as if they should agree. The optimistic `SPOKEN`
pre-assignment (set at router time) means the decision outcome does not even reflect whether the answer was
delivered. The `listen_only` label on utterance id=13 is not specific to this session — it is the
structural fate of every playground utterance because `AgentSpoke` carries no mode and the subscriber
defaults to `LISTEN_ONLY` when there is no `meeting_config`.

**Minimum invariant (`INV-2`, single spoken-text source of truth).** There is one authoritative field for
*"what the bot will speak this turn"*; the chat and the decisions panel both render it. If a separate
answer LLM is retained, the spoken text becomes that field and the router's `suggested_reply` is relabeled
a *preview* shown only with an explicit divergence badge; the utterance is bound to its decision by a
causal `turn_key` (not a timestamp scan); `outcome` flips to `SPOKEN` only on a confirmed `AgentSpoke`; and
the speak event carries the mode it actually ran under.

### B.3 `gate-suppression` — silence with no participant-visible signal

**Pattern.** The router's own `should_speak=false` / confidence-below-threshold gate drops a turn
(`outcome=suppressed`). Mechanically correct, but two things bite: calibration (session 14 turn 2, a
direct "tell me something", scored 0.15) and **absence of any participant-facing signal that the bot
heard and chose silence** — which is precisely what drove turn 3's "why are you not talking to me?".
Separately, the noise gate's drops are **not durable at all**.

**Code path.**

| File | Lines | Symbol | Role |
| --- | --- | --- | --- |
| `pipeline.py` | 1550-1555 | `_respond_to_transcript_inner` | `should_speak=false` → `suppressed`; `confidence < confidence_threshold` → `suppressed`. |
| `pipeline.py` | 1205-1313 | `_is_audio_below_noise_floor`, `_classify_transcript_as_noise`, `_publish_noise_filtered` | Pre/post-STT noise gate → `TranscriptFiltered` event. |
| `session_status_subscriber.py` | 444-455 | dispatch table | **No `transcript_filtered` branch** → noise-gate drops are live-only, never persisted. |
| `frontend/src/routes/sessions/[id]/+page.svelte` | 283-311, 1196-1234 | `groupTimingsByTurn`, error badge | A failed turn is flagged only inside the **collapsed Activity log**, never inline on the transcript line. |

**Root cause.** Suppression is auditable for router decisions (`agent_decisions` carries `outcome` +
`reason`) but invisible to the *participant* in the moment, and entirely non-durable for the noise gate.
The session-detail UI surfaces a turn's failure/suppression only in a separate collapsed panel, so scanning
the transcript or decisions view does not reveal that a turn was heard-and-dropped.

**Minimum invariant (`INV-3`, auditable & visible silence).** Every suppression — router *or* noise gate —
is a durable terminal record with a reason, surfaced **inline on the turn** (not only in a collapsed
panel). Per the task's out-of-scope note, the decision-mode set is left unchanged and confidence
*calibration* is deferred (filed as a follow-up under Johnny-ckz.28); this invariant covers durability and
visibility, not the scoring model.

---

## Section C — Redesign proposal

> Shape mirrors `tasks/prd-piper-tts-runtime-options.md`: Context / Goals / Non-goals / Architecture /
> Acceptance. This is the design the .2–.5 sub-tasks implement; it is deliberately a **minimal refactor of
> the existing event-sourced pipeline**, not a clean-room rewrite.

### Context

The split pipeline already separates capture from response (Johnny-har) and already emits rich per-stage
events (Johnny-ckz.7). The defect is not the staging — it is that **durable state is reconstructed from a
whitelist of happy-path events by one subscriber**, with no per-turn identity and no terminal-state
guarantee. Three symptoms (B.1–B.3) all trace to that. The fix is to give each turn a stable identity and
a single terminal record, and to make the failure and the spoken-text fields first-class.

### Goals

1. **No silent drops.** Every transcribed turn ends in exactly one terminal `agent_decisions` row
   (`spoken` / `suppressed` / `suggested` / `pending` / `no_reply` / `error`), each carrying a reason.
2. **One spoken-text source of truth.** The chat and the decisions panel render the *same* `final_text`;
   any router-vs-spoken divergence is explicit and labeled, never silent.
3. **Causal linkage, not scans.** `agent_utterances` ↔ `agent_decisions` ↔ `transcript_chunks` ↔
   `session_timings` share a `turn_key`; the most-recent-`should_speak` scan is retired.
4. **Honest outcomes & modes.** `outcome` reflects actual delivery; the utterance records the mode it ran
   under (no `LISTEN_ONLY` fallback for playground).
5. **A queryable reasoning timeline** (the data model sub-task .28.4 needs).

### Non-goals

- No new or changed decision modes (autonomous / suggest / approval set is frozen — task out-of-scope).
- No confidence-score recalibration in this redesign (filed as a separate follow-up).
- No clean-room rewrite of `VoicePipeline`; keep the transcribe/respond split and the event bus.
- No UI redesign beyond the inline turn-outcome badge + planned/spoken divergence display.

### Architecture

**1. Per-turn identity (`turn_key`).** Mint a stable `turn_key` when a transcript is finalized
(`_transcribe_and_emit`, where `_transcript_turn_ids` already assigns a `turn_id` at `pipeline.py:1146`).
Thread it onto every event for that turn: `TranscriptFinalized`, `RouterDecisionMade`, `AgentSpoke`,
`PipelineTiming`, and the new failure event. Add a nullable `turn_key` column to `transcript_chunks`,
`agent_decisions`, `agent_utterances`, `session_timings` (one migration; backfill `NULL` for history). This
is the join key for INV-2/INV-3 and the reasoning timeline.

**2. Terminal-state-per-turn (`INV-1`).**
   - Bound the router: wrap `router_llm.chat` in `asyncio.wait_for(router_llm_timeout_s)` — the exact
     idiom already at `_classify_barge_in_intent` (`pipeline.py:1389-1396`).
   - On any router/answer/TTS exception, publish a terminal decision event for the turn (a
     `RouterDecisionMade` with `should_speak=false`, `outcome=error`, `reason=<stage+message>`, or a new
     `TurnFailed` event) **carrying the `turn_key`**, so the subscriber writes one `agent_decisions` row.
   - Add subscriber dispatch branches for `pipeline_stage_failed`/`TurnFailed` (→ `agent_decisions`
     `outcome=error`) and `transcript_filtered` (→ a durable suppressed record), at
     `session_status_subscriber.py:444-455`.
   - The `_respond_loop` bare `except` (`pipeline.py:772-780`) becomes defense-in-depth that still emits a
     terminal error event before continuing.

**3. Single spoken-text source of truth (`INV-2`).** Preferred (impossible-by-construction): the answer
LLM's streamed `output_text` is written to a single canonical `final_text` on the turn's decision record at
speak-confirm time; the chat and decisions panel both read `final_text`; `suggested_reply` is retained but
relabeled *router preview* and shown only with a divergence badge when it differs beyond a trivial edit
distance. Retire the most-recent-`should_speak` scan (`apply_agent_spoke_event:317-324`) in favor of a
`turn_key` join. Stop pre-assigning `SPOKEN` (`apply_router_decision_event:240-241`): write `PENDING`/`in
progress` at router time and flip to `SPOKEN` only when the `AgentSpoke` for the same `turn_key` arrives.
Carry `mode` on the `AgentSpoke` event so the utterance records the mode actually run (kills the
`LISTEN_ONLY` playground mislabel at `:311-316`).

**4. Reasoning-timeline data model (for sub-task .28.4).** The timeline is the per-`turn_key` ordered list
of steps. Reuse `session_timings` as the backbone (it already has `turn_id`, `stage`, `started_at_ms`,
`duration_ms`, `provider_name`, `details`; `models.py:489-536`) and extend each turn's record with the
**decision reason** and the **two texts** so a reviewer sees *what was heard → how it was classified (and
why) → what was planned → what was said → terminal state* without cross-referencing three panels. Concrete
shape per step:

```
TurnStep { turn_key, seq, kind, actor, t_ms, duration_ms, provider, text?, reason?, details }
  kind  ∈ transcribed | noise_filtered | routed | suppressed | answered | spoke | interrupted | error
  actor ∈ user | noise_gate | router_llm | answer_llm | tts | approval
```

Minimal path: add `turn_key` to the existing tables (no new table required), and add a
`GET /sessions/{id}/turns` endpoint that assembles the timeline by `turn_key` from the joined rows. A
dedicated `turn_events` table is the fallback only if the join proves too lossy.

**5. Minimal refactor checklist (current code → target).**

| # | Change | Touch points |
| --- | --- | --- |
| 1 | Mint + thread `turn_key` | `pipeline.py` `_transcribe_and_emit` (1146), event dataclasses in `events.py` |
| 2 | `turn_key` columns + backfill | one Alembic migration; `models.py` |
| 3 | Bound router call | `pipeline.py:1759` (mirror 1389-1396) |
| 4 | Terminal event on failure | `pipeline.py` `_run_router` except (1763-1777) + `_respond_loop` (772-780) |
| 5 | Subscriber: new dispatch branches; `turn_key` join; no optimistic SPOKEN; mode on speak | `session_status_subscriber.py:240-245, 311-330, 444-455` |
| 6 | `final_text` canonical + divergence badge; inline turn-outcome badge | `+page.svelte` (245-272, 993-1108, 1196-1234), `sessionDetail.ts:40-61`, `api/sessions.py:272-379` |

### Failure-mode → invariant mapping (required by acceptance)

| Failure-mode label (Section A/B) | New invariant |
| --- | --- |
| `silent-drop` | **INV-1** terminal-state-per-turn (bounded router + terminal event on failure + failure-event dispatch branch) |
| `decision-divergence` (incl. optimistic outcome + mode mislabel) | **INV-2** single spoken-text source of truth (one `final_text`, `turn_key` linkage, delivery-honest outcome, mode-on-speak) |
| `gate-suppression` | **INV-3** auditable & visible silence (durable suppressed records incl. noise gate, inline turn signal) |

### Acceptance

- A turn whose router call exceeds `router_llm_timeout_s` produces exactly one `agent_decisions` row with
  `outcome=error` and a reason, visible in the decisions panel and as an inline badge on the transcript
  line — reproduced by faking a router timeout in a pipeline test and asserting the row exists.
- For any spoken turn, the decisions panel and the chat render byte-identical `final_text`; when the router
  preview differs, the panel shows a labeled divergence badge — asserted in a frontend test.
- `agent_utterances` for a playground (no-`meeting_config`) session records the mode actually run, never a
  blanket `listen_only`.
- A noise-gate drop appears as a durable suppressed turn with its `reason`, not only as a live event.
- `GET /sessions/{id}/turns` returns, per `turn_key`, the ordered `transcribed → routed(+reason) →
  answered/suppressed/error → spoke` step list with the two texts and the terminal state.
- Re-running the Section A reconstruction against a *new* session that reproduces all three failure modes
  shows every turn ending in a terminal, operator-visible state.

---

## Section D — Cross-link writeup

Concrete per-issue: what survives, what the redesign replaces, and what each prior fix missed that lets a
session-14-class failure re-emerge. Line ranges are current-tree.

**Johnny-vgl (free-form speech / TTS-absent degradation).** Added `AUTONOMOUS_MODE` (then
`FREE_AUTO_SPEAK`) to `SPEAKING_MODES` and the `FREE_FORM_MODES ⊆ SPEAKING_MODES` invariant
(`pipeline.py:288-312`; test at `test_pipeline.py:3815-3825`), and made `meet_worker/pipeline_runner.py`
`_assemble_pipeline` (466-492) degrade *every* speaking mode to `suggest_only` when TTS is missing.
**Survives:** the `SPEAKING_MODES`/`FREE_FORM_MODES` membership invariant and the degradation branch — keep
both. **Replaced:** nothing structurally, though the redesign's `final_text` + mode-on-speak removes the
adjacent mode-stamp drift. **Missed:** vgl is a *pre-assembly* check; it never enters `_run_router`, so it
has no bearing on the silent drop (B.1) or the divergence (B.2) — its degradation guard fires before any
turn is processed.

**Johnny-cdw (approval gate never wired to Redis).** Added `_build_approval_gate`
(`meet_worker/pipeline_runner.py:494-502`) so `approval_required` sessions get a real `RedisApprovalGate`
instead of the `NoopApprovalGate` that always times out. **Survives:** the gate construction + injection
pattern and its regression test. **Replaced:** the silent `Noop→timeout` branch becomes safer under INV-1
(a misconfigured gate must emit a durable terminal record, not silence). **Missed:** entirely scoped to
`approval_required`; `_build_approval_gate` returns `None` for autonomous mode, so session 14's three
failures are untouched.

**Johnny-arh (end-of-speech / barge-in race).** Raised `DEFAULT_END_OF_SPEECH_MS` 600→800 ms
(`pipeline.py:80-93`) and moved `_interrupt_event.clear()` to the top of `_respond_to_transcript_inner`
(1519) so a barge-in during the router stage survives to the post-router guard (1565). **Survives:** the
interrupt-clear-once-before-router discipline and the post-router `is_set()` guard — the redesign must keep
both, reframing the guard as a named terminal state (`cancelled_mid_router`) under INV-1. **Replaced:** the
raw event-flag check becomes a terminal-state transition. **Missed:** arh lives in `_utterances` and the
interrupt lifecycle; the silent-drop exception is thrown inside `_run_router` and never reaches arh's code,
and the divergence is downstream of the stage arh only touches in the cancel direction.

**Johnny-ckz.14 (STT noise gate).** Added the pre/post-STT noise gate and the `TranscriptFiltered` event
(`pipeline.py:1083-1313`, `events.py:102-165`). **Survives:** the two-layer gate, the
`TranscriptFilteredReason` taxonomy, and the per-provider stoplist knobs — all orthogonal to the redesign
and worth keeping; the reasoning timeline should render filtered turns as explicit `noise_filtered` steps.
**Replaced:** nothing — it is upstream of the router. **Missed:** the gate fires *before* the router, so a
substantive question that passes it (turns 1–2, 4 all passed) is fully exposed to B.1/B.2; and
`TranscriptFiltered` itself has no subscriber branch (`:444-455`), so today its drops are non-durable —
exactly the INV-3 gap.

**Johnny-har (concurrent transcribe/respond split).** Split `run()` into `_transcribe_loop` +
`_respond_loop` joined by `_response_queue` (`pipeline.py:704-787`), guaranteeing transcripts persist even
when the response side stalls — which is *why* turn 4's transcript exists despite the router hang.
**Survives:** the two-loop split (the structural prerequisite for INV-1), the `_transcript_turn_ids` map,
and the `is_current` history slicing. **Replaced:** the bare `except Exception` swallow (772-780) — INV-1
turns it into a terminal-record-then-continue. **Missed:** har guaranteed the *capture* side never drops
audio; it said nothing about the *response* side of a failed turn, so the router exception still yields no
`agent_decisions` row. har is the foundation the no-silent-drops invariant builds on, not a fix for it.

**Johnny-7qp (bot utterances fed back into prompt history).** Appends the bot's own speech to
`_transcript_history` via `_remember_bot_utterance` (`pipeline.py:2421-2447`) and rehydrates on restart
(`_rehydrate_transcript_history`, 800-836). **Survives:** the mixed participant+bot history and
`BOT_SPEAKER_LABEL` tagging — the reasoning timeline reuses the same ordered list; `_remember_bot_utterance`
(called at 1862) is the right hook to record `final_text` under INV-2. **Replaced:** once `final_text` is
canonical, the history records *it* rather than the answer LLM's independent output. **Missed:** orthogonal
to all three modes; it faithfully records whatever was spoken but does nothing to prevent the spoken text
from diverging from the router's intent, and adds no error-path persistence.

**Johnny-ckz.7 (per-turn activity log / `session_timings`).** Created `session_timings`
(`0008_session_timings.py`, `models.py:489-536`) and `_emit_timing` for every stage incl. the `error` row
that is turn 4's only durable trace (`pipeline.py:1765-1771`); rendered as the collapsed activity log
(`+page.svelte:283-381`). **Survives:** the entire observability substrate — table, `PipelineTiming` event,
turn-id resolution, `apply_pipeline_timing_event` — is the backbone the reasoning timeline extends.
**Replaced:** the `error` *timing* row is superseded as the *system of record* by the INV-1 terminal
*decision* row (timing stays for latency; the decision row is what the panel reads). **Missed:** timings
record durations and stage outcomes but no text and no terminal decision, so they can neither surface the
divergence (no `output_text` in timings) nor substitute for the missing `agent_decisions` row — the activity
log shows an anomalous 60 s error while the decisions panel stays blank.

**Johnny-klh (playground `speaker=null` mislabeled "You").** Frontend-only: a three-way
`'user'|'bot'|'speaker'` enum and NULL-guard so unknown-speaker rows render "Speaker"
(`playgroundSession.svelte.ts:66-72,482,673`; `LiveSession.svelte:314-343`). **Survives:** the enum + guard
+ `data-testid` anchors — correct display contract, keep as-is. **Replaced:** nothing — it is one layer
above the pipeline and tables the redesign touches. **Missed:** purely cosmetic relabeling of rows that
*did* reach the DB; a turn with no decision row (B.1) is still invisible, and it never touches the
divergence or the suppression paths.

**Johnny-ckz.25 (consolidate `free_auto_speak` → `autonomous`).** Removed `BotMode.FREE_AUTO_SPEAK`,
migrated rows (`0017_drop_free_auto_speak_mode.py`), and collapsed mode references to `AUTONOMOUS_MODE`
(`pipeline.py:263-312`, `browser_sessions.py:693`). This is why session 14's run mode reads
`free_auto_speak` in historical rows but the live enum is `autonomous`. **Survives:** the single
`AUTONOMOUS_MODE` token in `SPEAKING_MODES`/`FREE_FORM_MODES` and the autonomous playground default — both
load-bearing. **Replaced:** the optimistic `SPOKEN` pre-assignment (`apply_router_decision_event:240-241`)
and the dual-persistence stitch-by-scan (`apply_agent_spoke_event:287-344`) — the exact mechanisms behind
B.2 — are superseded by INV-2's `turn_key` linkage and delivery-honest outcome. **Missed:** a
mode-vocabulary cleanup that never touched the error path, the answer-LLM relationship, or the
`LISTEN_ONLY`-default mode stamp for NULL-`meeting_config` playground sessions (`:311-316`) — so all three
session-14 failures survive it intact.
