# Routing: triage, complexity heuristics, and addressing

**Status: design document, written ahead of implementation (2026-06-11).** Each section
names the bead(s) that build it; the status table at the bottom says what is shipped
versus planned. Until a section's bead is closed, this file describes intent, not
behavior. Sources: the Johnny-trt epic (Phases 3–6) and the operator-requested
evaluation of [ClawRouter](https://github.com/BlockRunAI/ClawRouter) (2026-06-11).

Related docs: [PIPELINE.md](PIPELINE.md) (session engine, events, invariants),
[LATENCY.md](LATENCY.md) (stage budgets and measured numbers).

---

## 1. Why a router at all

Johnny's primary surface is Google Meet — multi-participant. Before any question of
*how* to answer, the bot must answer a **social** question every turn:

> Should I speak at all? Is this utterance mine to handle?

That judgment needs conversational context (who is talking to whom, what was just
said, meeting mode) and is made by an LLM — the **triage router**. Cost- and
complexity-based routing (the ClawRouter question: "which model serves this
request?") only matters *after* the social gate. This ordering is why Johnny cannot
simply adopt a request-shape router: heuristics on the transcript alone cannot decide
"stay silent, they're talking to each other."

The router runs inside the blocking `on_user_turn_completed` hook
(`backend/johnny/agent/router_gate.py`); everything below is shaped by the constraint
that this hook stalls the reply pipeline while it runs.

## 2. Turn decision flow (target architecture)

```
 user turn (final transcript)
        │
        ▼
 ┌─────────────────────────────┐
 │ deterministic pre-stage     │  pure Python, ~0 ms, no LLM
 │  • name-addressing check    │  trt.52 — can terminate the turn:
 │    (if address_required)    │    no_reply(not_addressed), NO LLM call
 │  • heuristic complexity     │  trt.50 — shadow verdict
 │    scorer (shadow)          │    {score, tier, confidence, signals}
 └────────────┬────────────────┘
              ▼
 ┌─────────────────────────────┐
 │ triage LLM (router)         │  one call, tight JSON schema, ~8 s hard
 │  action ∈ {silent, speak,   │  budget (trt.19); task catalog rendered
 │   delegate, status}         │  into the prompt; cheap+fast model slot
 └────────────┬────────────────┘
              ▼
   silent ──► terminal no_reply(...)                      (INV-1)
   speak  ──► streaming answer LLM ──► sentence-level TTS (answer slot)
   status ──► TaskCoordinator.status_summary() ──► spoken reply
   delegate ─► say(ack) "this is complicated, give me a   (terminal = ack)
              moment — I'll be back with updates"
              + TaskCoordinator.begin() ──► agent_tasks row
                        │
                        ▼            worker executor (reasoning slot,
              async execution        skills-sandbox for CLI skills)
                        │
                        ▼
              speech queue ──► result spoken at a conversational
              boundary (floor free, user not speaking, grace window)
```

The two-level idea in one sentence: a **cheap, fast triage call** decides per turn,
and the "second level" is either the **streaming answer LLM** (simple asks: "when did
WW2 start?" → immediate answer) or the **delegated executor** (complex asks: "update
our Google Calendar" → instant verbal ack, async execution, result re-enters later).
There is deliberately **no second router call** stacked on the first.

Built by: trt.16 (schema/action vocabulary), trt.17 (gate branching + ack terminal),
trt.18 (agent_tasks + TaskCoordinator), trt.19 (budget + catalog + observability),
Phases 4–5 (executor, events, speech queue).

**Ack contract & decision transparency (trt.53 shipped 2026-06-11, trt.54).**
Operator principle (2026-06-11, from live use of the freshly-landed delegate path):
*the conversation must feel smooth and natural, and participants must always
understand the bot's decision.* Concretely:

- **Acks are LLM-authored, per turn, in the user's language** — they name the
  specific work and why it needs time ("I'll look through the connected calendar for
  tomorrow's events — give me a minute"). Enforced three ways (trt.53): `task.ack`
  is **schema-required** next to `kind` (constrained decoders must emit it; the
  parser stays lenient, so old recorded outputs parse identically), the schema
  description demands per-request authorship in the user's language (the canned
  example the live 3B model copied verbatim is gone), and the catalog header
  repeats the contract.
- **The ackless-delegate rule** (trt.53 — the rule chosen and shipped, of the two
  the bead allowed): a delegate verdict with no usable ack **degrades to `speak`**
  — the answer pipeline produces a real contextual reply instead of a hollow
  canned promise. Instrumented: an `ack_fallback` marker
  (`{from_action, to_action, kind, reason}`) lands in
  `agent_decisions.raw_output` before the decision emit, plus a warning log. The
  canned `DEFAULT_DELEGATE_ACK` ("Let me check on that — I'll get back to you")
  survives only as a logged defensive last resort for hand-built decisions that
  bypass `run_turn`; it is unreachable through the normal flow. Per-session
  **delegate rate** = decision rows with `raw_output->>'action' = 'delegate'` over
  all decision rows; **fallback-ack rate** = rows with `raw_output ? 'ack_fallback'`
  over the delegate-intent rows — the trt.21 capstone reads both.
- **Delegate restraint** (trt.53, in the catalog header + the `action` schema
  description): answerable-from-context → `speak`, even when catalog keywords
  appear; only catalog-listed kinds are delegatable; when unsure between speak and
  delegate, **speak** — a real answer beats a hollow promise.
- **No dead promises** (trt.53 stopgap until the Phase-5 re-entry queue): a
  delegated task that settles `failed` (stub executor today, executor crash later)
  re-enters immediately as a short honest spoken correction — "Actually — I can't
  do that yet: <speech-ready failure text>" — via
  `RouterGate.report_task_failure`, attached to the `TaskCoordinator` failure-report
  seam at gate construction and invoked only *after* the row settles. Session-scoped
  `say()` speech per the approval-reply precedent: no turn terminal (the ack already
  settled INV-1); `done` / `cancelled` settles report nothing. Since trt.54 the
  completed correction IS recorded — `AgentSpoke(kind="correction", turn_id=None)`
  from its say-handle done-callback — so it lands in `agent_utterances` and the
  chat/history exactly as spoken while stamping **no** decision row's `final_text`
  (an interrupted correction keeps its caption partial the same way — trt.58 below).
  Replaced wholesale by trt.29.
- **The whole chain is visible in history** (trt.54, shipped 2026-06-11): final
  transcript (`input_window.transcript_window`, `is_current` entry — also what makes
  agent sessions replayable) → heuristic shadow verdict → router action + confidence
  + stated reason → spoken text (recommended vs final, divergence flagged; a
  delegate verdict's `task.ack` is the recommendation) → linked `agent_tasks` row
  (kind/status/result) → terminal + stage timings. Every `AgentSpoke` carries
  `kind` (`reply|ack|status|correction`) + the durable int `turn_id`, so
  `final_text` stamps the exact turn. No turn may leave "what did it say, and
  why?" unanswerable from the UI.
- **Interrupted speech keeps its partial** (trt.58, shipped 2026-06-11): a barge-in
  used to make the in-flight phrase vanish (the streamed caption bubble was
  discarded, no `AgentSpoke`, no row). Now every speech path — streamed reply,
  delegate ack, status stub, the failed-task correction — that already flushed at
  least one caption sentence emits `AgentSpoke(interrupted=true)` with the caption
  text at cut time (the gate's `SpeechCaptionBuffer`, fed by a `tts_node` sink tee;
  an honest *approximation* of what was audibly heard, since a sentence flushes to
  synthesis slightly ahead of playout). The turn's terminal stays
  `no_reply(barge_in)` — INV-1 untouched — while the utterance row is flagged
  `interrupted` and the decision row's `final_text` carries the partial, audited as
  `override_actor="user"` / barge-in divergence. Chat + history render the partial
  with an "interrupted" marker; speech cut before any flush still records nothing
  (nothing was heard).

**Capability awareness (trt.55, Phase 4).** Operator rule (2026-06-11): *the
decision-making must know what it is actually capable of.* "Check our Google
Calendar" → with access, delegate and check; without, an honest actionable decline in
the same turn ("I don't have access to your calendar — link a Google account in
settings"). Mechanism: the **catalog is the capability source of truth**, assembled
per session through an availability predicate — skill bins present in the sandbox
(trt.23) ∧ credentials/accounts linked (e.g. gog authed) ∧ frontmatter prerequisites,
with per-agent policy (trt.38) and MCP health (trt.36) joining the same predicate
later. Unavailable kinds render compactly *as unavailable with the reason* so the
router can decline and name the fix; the gate degrades any delegate verdict targeting
an unavailable kind to speak-with-decline (defense in depth); unavailable keywords
are excluded from the scorer's delegate prior; the executor revalidates at claim time
(links can break mid-session → trt.53 correction). Never a pretend-check, never
delegate-into-failure.

**How capabilities reach the prompt — snapshot + progressive disclosure (trt.23,
trt.55; openclaw-verified pattern).** The capabilities live in a separate sandbox
image, but the router prompt never talks to the sandbox:

- **Capability snapshot, off the hot path**: the api/worker builds a cached, versioned
  snapshot via one batched `GET /bins` probe against the sandbox (trt.35's exec API) at
  stack boot and on change events (skills-volume change, integration link/unlink,
  sandbox restart). Session start *reads the cache* — zero sandbox round-trips; the
  session's catalog is frozen at start (openclaw equivalent: chokidar watcher + version
  bump + coalesced `system.which`, caps at 150 skills / 18 K chars).
- **Progressive disclosure**: the router prompt carries only `kind: one-liner` rows
  (plus capped unavailable-reasons) — today ~1.2 K chars total, rebuilt per turn. Full
  SKILL.md instructions are read **only by the executor's reasoning model at execution
  time**. The router prompt stays near-constant as capabilities grow; the catalog block
  is capped (~2 K chars, overflow summarized) and its size is recorded in the triage
  timing details.
- **Kind-validation backstop (trt.53)**: the gate validates `task.kind` against the
  catalog *before* persisting or acking — a hallucinated kind degrades to `speak`
  instead of becoming a spoken promise that the stub executor then breaks. (Code-verified
  gap 2026-06-11: parser and gate previously accepted any kind; "when did WW2 start" →
  "let me check on that" was the symptom.) Knowledge questions are never delegated —
  the restraint guidance carries an explicit general-knowledge negative example.
- **Internal tools (trt.57)**: first-party in-app actions (`meeting.leave`,
  `session.end`) are catalog kinds like any other, surface-scoped by the availability
  predicate, but execute **session-locally in the agent process** — never the worker,
  never the sandbox. Asking "can you leave the meeting?" yields a farewell, the
  trt.56 dismissed state (so the scheduler doesn't auto-rejoin), and a clean teardown.

## 3. Per-agent model roles

Agents (Phase 6, trt.41) pin a model per pipeline level — so a light local model can
do triage while a heavyweight API model does delegated reasoning:

| Role slot | Drives | Guidance | Example |
|---|---|---|---|
| `router_llm_provider_id` | the triage call | super cheap + fast; small local model | Ollama `qwen2.5:7b` |
| `answer_llm_provider_id` | what the agent says (streaming) | conversational quality, low TTFT | mid local/cloud model |
| `reasoning_llm_provider_id` | delegated complex tasks in the executor | capability over latency | GPT-class via API |

All three are nullable with one fallback chain:
**agent role slot → global default for that role → global active LLM.**

- Today the runtime cannot express this split: one provider drives both router and
  answer (`router_llm=answer_llm` in `backend/johnny/agent/job_session.py`). trt.42
  splits the payload into roles behind a single resolution seam; Phases 3–4 keep the
  global model behind that same seam so the per-agent slots drop in without rework
  (trt.19/trt.24 notes).
- The reasoning provider is **stamped into the `agent_tasks` row at delegation time**
  so the worker executes each task with the *requesting agent's* reasoning model
  (trt.42 → trt.24).
- **Runtime fallback chains** (ClawRouter pattern, trt.42 note): the same chain
  applies at *call* time — a hard provider error mid-turn falls through one hop max,
  emitting a warning event naming the agent and provider. If that retry hop threatens
  the latency budget, it gets scoped down to session-start health-check fallback;
  decided during trt.42 implementation.

## 4. Heuristic complexity scorer (trt.50 — shadow first)

A pure-stdlib port of ClawRouter's *pattern* (`src/router/rules.ts`, MIT — attribution
kept in the module docstring), adapted to voice turns:
`backend/johnny/agent/complexity.py`.

**Mechanism.** Weighted keyword/regex dimensions (weights sum to 1.0) produce a
score; the score maps to a tier; confidence is a sigmoid of the distance to the
nearest tier boundary; strong reasoning markers override directly.

| Dimension (Johnny adaptation) | Intent |
|---|---|
| reasoning markers ("prove", "step by step", …) | REASONING prior; ≥2 markers → direct override |
| multi-step patterns ("first … then", numbered lists) | complexity prior |
| agentic/imperative verbs ("schedule", "update", "send") | delegate prior |
| **task/skill-catalog keywords** (dynamic, from the trt.19 catalog interface) | the *delegate* prior — "calendar", "email" etc. come from installed skills, not a hardcoded list |
| simple indicators ("what is", "define", greetings) | scored **negatively** |
| token estimate | very short → simple; very long → complex |
| output-format markers ("as a list", "summarize", "таблицей", "ranskalaiset viivat") | structured-output prior |

Shipped calibration (2026-06-11, trt.50): tier boundaries
`SIMPLE < 0.0 ≤ MEDIUM < 0.3 ≤ COMPLEX < 0.5 ≤ REASONING`, sigmoid steepness 12,
ambiguity threshold 0.7, ambiguous → safe default tier **MEDIUM** (all ClawRouter
config.ts values; provenance documented per constant in the module). Voice-turn
adaptations: token thresholds 50/500 → **12/60** spoken tokens (`words × 1.3`);
agentic ladder 4/3/1 → **3/2/1** matches; keywords match as **left word-boundary
prefixes** (`\b<stem>`, free suffix) so RU/FI stems survive inflection ("провер" →
проверь/проверка, "tarkist" → tarkista/tarkistaisitko) without mid-word noise
("etsi" never fires inside "metsissä"). Keyword sets are multilingual — **EN + RU +
FI**; the catalog dimension matches entry keywords (English, per the task_catalog
contract) expanded through the scorer-owned `CATALOG_KEYWORD_TRANSLATIONS` RU/FI
stem map. Weights (sum 1.0): reasoning .22, catalog .18, multi-step .16, agentic
.14, simple .12, token .10, format .08.

**First agreement matrix** (2026-06-11, 86 turns = 34 replay-fixture + 52 real DB
turns incl. 12 live-shadow rows from playground session #18 — full report:
`.validation/Johnny-trt.50/05-agreement-matrix.md`, regenerate with
`agreement_matrix_trt50.py` next to it): the catalog dimension fired on **32% of
actually-delegated turns vs 4% of silent ones** (8× lift; the miss-rate is mostly
llama3.2:3b *over*-delegating greetings/reasoning asks that genuinely carry no
catalog vocabulary — the disagreement is the finding). Heuristic-SIMPLE turns split
16 silent / 14 speak / 12 delegate / 1 status across the live router → the trt.51
fast-path go condition (≥ ~98% SIMPLE agreement) is **not met** on the current
3B triage model; fast-path stays no-go. 25/86 turns ambiguous (the safe-default
band absorbs mid-length keyword-less turns); COMPLEX is unpopulated (no long
multi-clause asks in the dataset yet). All 12 session-18 live payloads byte-match
the offline recompute — the persisted rows are an offline-reproducible dataset.

**Shadow contract.** The scorer runs synchronously in `RouterGate` before the triage
LLM is awaited (zero added latency; computed outside the `router_llm` timing span so
the trt.19 timing rows stay baseline-comparable) and its verdict
`{score, tier, confidence, top_signals}` is persisted under the
`complexity_shadow` key inside `agent_decisions.raw_output`, next to the router's
own `action` — **no behavioral effect, no migration, replay parity untouched by
construction** (the replay diff reads only verdict fields, never `raw_output`).
A scorer failure logs and skips the key; the turn is untouched. The deliverable is
an *agreement matrix* (heuristic tier × router action over harness fixtures + ≥50
real turns) — the labeled dataset that gates every behavioral use (§6).

**ClawRouter evaluation summary** (why pattern-port, not dependency): ClawRouter is an
MIT TypeScript OpenAI-compatible proxy whose model dispatch is welded to BlockRun's
commercial x402/USDC payment gateway and their 55-model catalog; it assumes every
request gets answered (no silent/social gate) and has no async delegation framework.
Adopted: the rule-based scorer, boundary-distance confidence, runtime fallback chains,
decision telemetry. Rejected: running the proxy (wrong economics/topology), a fourth
per-agent "answer-tier" slot (delegation + micro-reply cover it), LLM-fallback
classification (Johnny's triage *is* the LLM layer), dedup/session pinning (sessions
are already sticky per agent).

## 5. Name addressing (trt.52)

Per-agent settings: **`address_required`** (checkbox, default **off**) and
**`name_aliases`** (list; absorbs STT mishears — "Johnny" → "Joni"/"John" — and RU/FI
declensions/vocatives). Both live in the Behavior section of the agent edit page.

The check is **deterministic and pre-LLM** (an extension of the §4 module): normalize
the final transcript, fuzzy-match the agent's name + aliases.

- **Checkbox ON (hard gate):** if the turn doesn't mention the agent *and* the agent
  is not in a **recently-engaged follow-up window** (it was addressed or spoke within
  the last N turns/seconds — so "Johnny, weather?" → answer → "and tomorrow?" still
  works), the turn terminates `no_reply(not_addressed)` **without any triage LLM
  call**. In a busy meeting this makes unaddressed chatter cost nothing — no tokens,
  no router latency — while INV-1 still holds (the gate emits the terminal; the
  activity log shows it).
- **Checkbox OFF (default):** a name mention only feeds the heuristic scorer as a
  strong speak-prior dimension (shadow-first, per §4 discipline). Default-off keeps
  replay parity untouched.
- **Multi-agent:** a *peer* agent's name matching (and not mine) is a strong
  silent-prior. This is the deterministic addressed-to-whom signal that turn
  arbitration (trt.47) consumes first: a by-name match wins the turn claim outright;
  LLM-based peer selectivity applies only to unaddressed turns.

## 6. Deferred behavioral uses (trt.51 — go/no-go from §4's data)

None of these ship until the shadow agreement matrix justifies them; each is
separately replay-gated:

| Behavior | What it does | Go condition |
|---|---|---|
| (a) Playground fast-path | high-confidence SIMPLE turns skip the triage LLM, go straight to the streaming answer LLM (saves the full triage round-trip). **Meetings never fast-path** — silent\|speak is social. | shadow agreement on SIMPLE turns ≥ ~98% vs router speak-decisions on the playground surface |
| (b) Prompt prior | one additive line of router-prompt context ("signal: likely-complex (calendar keywords)") | measurably improves delegate precision/recall; requires a deliberate replay-fixture refresh |
| (c) Micro-reply gate | the trt.17-noted inline micro-reply (triage returns a short `speak_text` for trivial turns — one model call total) is allowed only on heuristic high-confidence SIMPLE | folds into the trt.17/trt.51 evaluation |

## 7. Invariants and budgets

- **INV-1** — exactly one terminal per turn. A delegated turn's terminal is its
  **ack**; async results re-enter as session-scoped speech (approval-reply
  precedent), never as turn terminals. The `not_addressed` hard gate emits its own
  terminal. **INV-2** — what was spoken is what was recorded (`AgentSpoke` with the
  ack text); trt.54 extends the `final_text` stamp + history entry to **all**
  `say()`-path speech (ack, status, correction) so the decisions panel and the chat
  can never silently diverge.
- **Replay verdict parity** — router schema changes are additive; old model outputs
  parse byte-for-byte identically (`johnny-replay --mode invariants` is a CI gate).
  The shadow scorer and the default-off addressing gate are parity-safe by
  construction; behaviors in §6 each require a deliberate, noted fixture refresh.
- **Budgets** — triage call hard budget 8 s (shipped, trt.19:
  `DEFAULT_ROUTER_LLM_TIMEOUT_S` — a ceiling that drops the turn with
  `no_reply(stage_error)`, not a latency target; the triage cost is visible per turn
  as the `router_llm` row in `session_timings` / `triage_ms` in the harness); felt
  latency targets live in [LATENCY.md](LATENCY.md) (speech-end → first audible byte,
  p50/p95: 300/500 ms all-local, 250/450 mixed, 200/400 all-cloud). A delegated
  turn's felt latency = triage call + ack TTS only.

## 8. Status table

| Piece | Bead | Status (2026-06-11) |
|---|---|---|
| Router schema: `action` + `task` (parity-safe) | Johnny-trt.16 | **shipped** (2026-06-11) — `ROUTER_ACTIONS` enum + nullable `task {kind, args, ack}` in `_ROUTER_SCHEMA`; `RouterDecision.action`/`task_request` (`task_request` non-None iff `action='delegate'`); old outputs parse identically, malformed tasks degrade to speak/silent |
| Gate branching + ack terminal | Johnny-trt.17 | **shipped** (2026-06-11) — `RouterGate.run_turn` branches on `decision.action` after the mode checks (suggest_only/approval_required/listen_only and the rate limiter unchanged): `delegate` → `TaskCoordinator.begin` (row-before-ack) + `session.say(ack)` whose SpeechHandle completion owns the turn terminal (`replied` / `no_reply(barge_in)`; coordinator/persist/say failure → nothing spoken + `no_reply(stage_error)`); `status` → fixed Phase-3 stub line via the same say machinery. No answer-LLM hop on either; `AgentSpoke` carries the ack text (INV-2); task results are session-scoped speech later, never turn terminals (INV-1) |
| `agent_tasks` + TaskCoordinator + stub executor | Johnny-trt.18 | **shipped** (2026-06-11) — `agent_tasks` table + migration 0023; `SqlAlchemyTaskSink`; stdlib `TaskCoordinator` (row durable at `begin` return, best-effort `TaskQueued` + `johnny.tasks.wake` ping, aclose marks `cancelled`); Phase-3 `stub_executor` fails every kind fast with speech-ready text; wired for all SPEAKING_MODES via `_build_sync_persistence` |
| Triage budget + task catalog + observability | Johnny-trt.19 | **shipped** (2026-06-11) — `DEFAULT_ROUTER_LLM_TIMEOUT_S` 30 → 8 s (budget framing; gate mirror + drift-guard/value tests); `TaskCatalogEntry (kind, one_liner, keywords[])` in `johnny/agent/task_catalog.py` with Phase-3 stubs (`calendar.upcoming_events`, `gmail.search`) rendered into the router prompt **only when a TaskCoordinator is wired** (keywords stay scorer-only, feeding trt.50); gate emits a per-decided-turn `router_llm` PipelineTiming (`details.action`) → `session_timings`; latency harness reports it directly as `triage_ms` (the derived `router_ms` gap stays for baseline comparability); small-router-model + 8 s budget tip on the OpenAI-compatible provider. Phase 4 (trt.23) swaps the catalog *source* to the skill loader; the entry shape is the contract |
| Heuristic complexity scorer (shadow) | Johnny-trt.50 | **shipped** (2026-06-11) — pure-stdlib `johnny/agent/complexity.py` (ClawRouter pattern port, MIT attribution + per-constant provenance; 7 voice dimensions incl. the dynamic catalog delegate-prior; EN+RU+FI stem sets, left-boundary prefix matching); `RouterGate.run_turn` scores before the triage await and stashes the 4-key verdict under `raw_output.complexity_shadow` (one debug log line; scorer failure → key absent, turn untouched); first 86-turn agreement matrix in `.validation/Johnny-trt.50/05-agreement-matrix.md` (summary in §4) — catalog dim fired 32% on delegated vs 4% on silent turns, trt.51 fast-path **no-go** on the 3B router; the SIMPLE×delegate cell (12 turns, greetings delegated) is trt.53's quantified evidence |
| Delegate restraint + contextual LLM-authored acks | Johnny-trt.53 | **shipped** (2026-06-11) — schema: `task.ack` required + canned example removed + restraint in the `action` description (parser untouched, old outputs parse identically); catalog header rewritten (only listed kinds, answerable-from-context ⇒ speak, unsure ⇒ speak, ack authored per turn in the user's language); gate: ackless delegate degrades to SPEAK with the `ack_fallback` marker in `raw_output` + warning (`DEFAULT_DELEGATE_ACK` now a logged defensive last resort); no dead promises: failed task settles re-enter as the spoken `say()` correction via the coordinator's failure-report seam (auto-attached at gate construction; after-row, no terminal, no AgentSpoke until trt.54); delegate/fallback-ack rates derivable from decision rows (§2). Replay fixtures untouched (all old-format, no `action` fields) |
| Decision-pipeline observability (full chain incl. spoken text) | Johnny-trt.54 | **shipped** (2026-06-11) — `AgentSpoke` carries `kind` (`reply\|ack\|status\|correction`) + durable int `turn_id`: the subscriber stamps `final_text` on the exact turn's decision row (recency scan kept only as the legacy fallback) and a `correction` inserts an **unlinked** utterance row (the trt.53 walk-back lands in chat/history verbatim, stamps nothing); a delegate verdict's `task.ack` snapshots into `decision_recommended_text` (say-path divergences audit as `override_actor=router_gate`); the decision event's `input_window` gains `transcript_window` (+ instructions/threshold) so the timeline's "Heard you" works on reload AND `/sessions/{id}/replay` reconstructs agent sessions (was 0 replayable turns); session-page timeline reworked to the full chain — heard → shadow verdict (trt.50) → decided action + reason + `router_llm` timing → context → answer-model (say-path turns say "no answer hop") → router-authored ack → linked `agent_tasks` row → guards (incl. `ack_fallback` chip) → final (recommended vs final) → spoke (exact text + audio; missing `final_text` on a replied turn flags the INV-2 gap); session detail API exposes `tasks` |
| Interrupted speech keeps its partial (chat/history/decision row) | Johnny-trt.58 | **shipped** (2026-06-11) — gate `SpeechCaptionBuffer` fed by a `tts_node` sink tee; every interrupt branch (reply/ack/status/correction) that flushed ≥1 caption emits `AgentSpoke(interrupted=true)` with the cut-time caption text AFTER the unchanged `no_reply(barge_in)` terminal; utterance row flagged `interrupted` (migration 0024), `final_text` carries the partial audited as `override_actor=user`; playground chat + session history + timeline render an "interrupted" marker; cut-before-first-flush still records nothing |
| Phase 3 capstone (parity + INV-1 + delegated turn) | Johnny-trt.21 | **done** (2026-06-11) — replay invariants 5/5 fixtures + live-session replay green (session #27 incl. a real delegated turn: one terminal, `agent_tasks` row, no answer hop, failed settle → spoken correction); full suite 3872 passed; delegate rate 12.5 %, fallback-ack rate 0 %. Two measured misses vs the aspirational bars, both triage-model-bound and filed: ack first audio ≈ speech-end +3.4 s (triage = 91 % of it), and the Phase-3 router schema costs **+568 ms p50 / +666 ms p95** on the 3B router's plain speak turns vs the Phase-2 capstone (per-call, isolated from context growth; `.validation/Johnny-trt.21/`) — the trt.51 fast-path / trt.41–42 triage-model-slot data |
| Executor, tools/skills, task events | Johnny-trt.22–26, .35 | planned (Phase 4) |
| Capability-aware catalog (availability + honest declines) | Johnny-trt.55 | planned (Phase 4) |
| Meeting lifecycle states (dismissible bot, no auto-rejoin) | Johnny-trt.56 | planned (Phase 4) |
| Internal tools (`meeting.leave`, `session.end` by voice) | Johnny-trt.57 | planned (Phase 4) |
| Speech queue + re-entry + status query | Johnny-trt.27–30 | planned (Phase 5) |
| Per-agent model role slots (schema) | Johnny-trt.41 | planned (Phase 6) |
| Role-based provider resolution + runtime fallback | Johnny-trt.42 | planned (Phase 6) |
| Agent edit page (Triage/Answer/Reasoning pickers) | Johnny-trt.44 | planned (Phase 6) |
| Name-addressing gate (`address_required` + aliases) | Johnny-trt.52 | planned (Phase 6) |
| Multi-agent turn arbitration (consumes addressing) | Johnny-trt.47 | planned (Phase 6) |
| Deferred: fast-path / prompt prior / micro-reply | Johnny-trt.51 | deferred spike |
| Deferred: speculative-parallel router | Johnny-trt.20 | deferred spike |

Keeping this file current is acceptance criteria on trt.50, trt.51, trt.52, trt.53,
trt.54, trt.55, trt.57 and part of the trt.34 docs capstone (cross-link with
PIPELINE.md, flip statuses to shipped).
