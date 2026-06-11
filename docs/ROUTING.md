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
| output-format markers ("json", "table") | structured-output prior |

Starting calibration, taken from ClawRouter and re-fit on Johnny data: tier boundaries
`SIMPLE < 0.0 ≤ MEDIUM < 0.3 ≤ COMPLEX < 0.5 ≤ REASONING`, sigmoid steepness 12,
ambiguity threshold 0.7, ambiguous → safe default tier. Keyword sets are multilingual
— **EN + RU + FI** minimum.

**Shadow contract.** The scorer runs synchronously in `RouterGate` before the triage
LLM is awaited (zero added latency) and its verdict
`{score, tier, confidence, top_signals}` is persisted inside the existing
`agent_decisions` JSON columns — **no behavioral effect, no migration, replay parity
untouched by construction**. The deliverable is an *agreement matrix* (heuristic tier
× router action over harness fixtures + ≥50 real turns) — the labeled dataset that
gates every behavioral use (§6).

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
  ack text).
- **Replay verdict parity** — router schema changes are additive; old model outputs
  parse byte-for-byte identically (`johnny-replay --mode invariants` is a CI gate).
  The shadow scorer and the default-off addressing gate are parity-safe by
  construction; behaviors in §6 each require a deliberate, noted fixture refresh.
- **Budgets** — triage call hard budget ~8 s (hang-guard, not target; trt.19); felt
  latency targets live in [LATENCY.md](LATENCY.md) (speech-end → first audible byte,
  p50/p95: 300/500 ms all-local, 250/450 mixed, 200/400 all-cloud). A delegated
  turn's felt latency = triage call + ack TTS only.

## 8. Status table

| Piece | Bead | Status (2026-06-11) |
|---|---|---|
| Router schema: `action` + `task` (parity-safe) | Johnny-trt.16 | **shipped** (2026-06-11) — `ROUTER_ACTIONS` enum + nullable `task {kind, args, ack}` in `_ROUTER_SCHEMA`; `RouterDecision.action`/`task_request` (`task_request` non-None iff `action='delegate'`); old outputs parse identically, malformed tasks degrade to speak/silent |
| Gate branching + ack terminal | Johnny-trt.17 | **shipped** (2026-06-11) — `RouterGate.run_turn` branches on `decision.action` after the mode checks (suggest_only/approval_required/listen_only and the rate limiter unchanged): `delegate` → `TaskCoordinator.begin` (row-before-ack) + `session.say(ack)` whose SpeechHandle completion owns the turn terminal (`replied` / `no_reply(barge_in)`; coordinator/persist/say failure → nothing spoken + `no_reply(stage_error)`); `status` → fixed Phase-3 stub line via the same say machinery. No answer-LLM hop on either; `AgentSpoke` carries the ack text (INV-2); task results are session-scoped speech later, never turn terminals (INV-1) |
| `agent_tasks` + TaskCoordinator + stub executor | Johnny-trt.18 | **shipped** (2026-06-11) — `agent_tasks` table + migration 0023; `SqlAlchemyTaskSink`; stdlib `TaskCoordinator` (row durable at `begin` return, best-effort `TaskQueued` + `johnny.tasks.wake` ping, aclose marks `cancelled`); Phase-3 `stub_executor` fails every kind fast with speech-ready text; wired for all SPEAKING_MODES via `_build_sync_persistence` |
| Triage budget + task catalog + observability | Johnny-trt.19 | planned (Phase 3) |
| Heuristic complexity scorer (shadow) | Johnny-trt.50 | planned (Phase 3) |
| Phase 3 capstone (parity + INV-1 + delegated turn) | Johnny-trt.21 | planned (Phase 3) |
| Executor, tools/skills, task events | Johnny-trt.22–26, .35 | planned (Phase 4) |
| Speech queue + re-entry + status query | Johnny-trt.27–30 | planned (Phase 5) |
| Per-agent model role slots (schema) | Johnny-trt.41 | planned (Phase 6) |
| Role-based provider resolution + runtime fallback | Johnny-trt.42 | planned (Phase 6) |
| Agent edit page (Triage/Answer/Reasoning pickers) | Johnny-trt.44 | planned (Phase 6) |
| Name-addressing gate (`address_required` + aliases) | Johnny-trt.52 | planned (Phase 6) |
| Multi-agent turn arbitration (consumes addressing) | Johnny-trt.47 | planned (Phase 6) |
| Deferred: fast-path / prompt prior / micro-reply | Johnny-trt.51 | deferred spike |
| Deferred: speculative-parallel router | Johnny-trt.20 | deferred spike |

Keeping this file current is acceptance criteria on trt.50, trt.51, trt.52 and part
of the trt.34 docs capstone (cross-link with PIPELINE.md, flip statuses to shipped).
