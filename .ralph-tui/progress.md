# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Per-session prompt fragments reach the LLM through TWO runners × TWO pipeline modes
A new piece of per-session system-prompt text (like the personality persona in
Johnny-oly.8) must be threaded through every layer or it silently misses a
surface:
- **Resolver** → add the field to `PersonalityResolution`
  (`app/services/personality_resolver.py`) and populate it where the personality
  is applied.
- **Carriers** → `BrowserPipelineSpec` (`app/services/browser_pipeline_runner.py`)
  for the in-process runner AND `LaunchContext` (`app/services/session_scheduler.py`)
  → `JOHNNY_*` env var (`app/services/docker_launcher.py::_build_environment`) for
  the meet-worker.
- **Configs** → `PipelineConfig` (split) AND `UnifiedPipelineConfig` (unified) —
  both `johnny/voice_pipeline/`.
- **Runners** → map the carrier onto the config in BOTH
  `app/services/browser_pipeline_runner.py` (in-process) and
  `johnny/meet_worker/pipeline_runner.py` (container; reads the env var).
- **Render** → split mode builds the system string in
  `pipeline.py::_router_messages`/`_answer_messages` (guard with
  `if self.config.<field>:` so empty = today's exact prompt); unified mode hands
  one `instructions` string to the S2S provider, so compose via a property like
  `UnifiedPipelineConfig.system_instructions`.

### Personality is the IDENTITY layer, mode/template is the JOB layer
`build_personality_system_prompt(personality)` →
`[personality: <name>]\n<description>` is rendered FIRST in the split system
message (before mode/instructions/context). Keep it OUT of `instructions` (the
JOB layer) so audits/tests can tell persona from meeting brief.

### Running backend tests in the dockerized api (no pytest in the prod image)
`docker compose exec api uv run --group dev pytest <paths>` — pytest lives in the
`[dependency-groups] dev` group, not the baked image. Frontend gates are only
`pnpm typecheck` + `pnpm lint` (no `test` script / vitest in the container).

### run.sh / run-dev.sh port-5173 sweep can kill Docker Desktop
When the dockerized frontend is already up, the host port-5173 sweep in `run.sh`
matches Docker's own proxy (`com.docker.backend`) and kills it, taking the daemon
down. To bring up the dev stack without the sweep:
`COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml docker compose up -d`.

---


## 2026-06-08 - Johnny-oly.8
- **What:** The personality `description` is now THE character text, injected verbatim into the LLM system prompt at session start. This is an operator-driven REVERSAL of the oly.1 PRD / oly.3 stance that personalities carry no persona text ("description NOT injected"). One freeform field — no structured columns, no JSONB inner schema.
- **Core deliverable:** `build_personality_system_prompt(personality, *, include_preamble=True) -> str` (pure, deterministic) → `[personality: <name>]\n<description>`; empty description → preamble alone; `include_preamble=False` → description alone; surrounding whitespace normalised for a stable prompt hash.
- **Files changed (backend):**
  - `app/services/personality_resolver.py` — new pure function; `PersonalityResolution.personality_prompt` field; populated in `apply_personality`.
  - `johnny/voice_pipeline/pipeline.py` — `PipelineConfig.personality_prompt`; rendered FIRST (identity layer) in `_router_messages` + `_answer_messages` (guarded so empty = unchanged prompt).
  - `johnny/voice_pipeline/unified_pipeline.py` — `UnifiedPipelineConfig.personality_prompt` + `system_instructions` property; used at S2S `open_session` and the utterance-sink audit prompt.
  - `app/services/browser_pipeline_runner.py` — `BrowserPipelineSpec.personality_prompt`; mapped onto both split + unified configs.
  - `app/api/browser_sessions.py` — set `spec.personality_prompt = resolution.personality_prompt` in both `_build_spec_from_event` + `_build_spec_playground`.
  - `app/services/session_scheduler.py` — `LaunchContext.personality_prompt`; captured from the resolution in `launch_session_for_meeting`.
  - `app/services/docker_launcher.py` — `JOHNNY_PERSONALITY_PROMPT` env var.
  - `johnny/meet_worker/pipeline_runner.py` — `PERSONALITY_PROMPT_ENV`; read + threaded into all 3 config constructions (unified + 2 split).
- **Files changed (tests):**
  - `tests/services/test_personality_resolver.py` — 8 unit tests for the pure function + resolution carrier.
  - `tests/voice_pipeline/test_pipeline.py` — 2 tests: sentinel reaches router+answer system messages (identity-before-job), and empty prompt = unchanged.
  - `tests/integration/test_personality_e2e.py` — flipped the stale "description NOT in prompt" assertion → asserts `spec.personality_prompt == "[personality: Alice]\n<desc>"`; rewrote the docstring premise.
  - `tests/services/test_docker_launcher.py` — `JOHNNY_PERSONALITY_PROMPT` env assertion + `_make_ctx` param.
  - `tests/e2e_browser/test_personalities_browser.py` — oly.8 chrome-devtools MCP validation addendum.
- **Files changed (frontend):** `frontend/src/routes/personalities/+page.svelte` — multi-line placeholder example; helper text reframed ("injected verbatim as Johnny's system prompt"); rows 3→10 + `resize-y` + larger max-height; counter `toLocaleString()`; `>4000`-char token-cost hint; page-header copy mentions "character (system prompt)".
- **Docs:** README — new "Personalities" section (single-field shape sentence + pointer to `/personalities`).
- **Quality gates:** backend pytest (resolver/pipeline/e2e/launcher/scheduler/browser_sessions/meet_worker/unified — all green); my code is ruff- and mypy-clean (the only ruff/mypy findings are PRE-EXISTING UP041 + dispatch/stub-TTS typing, outside this diff). Frontend `pnpm typecheck` 0 errors, `pnpm lint` clean.
- **Browser validation (chrome-devtools MCP, live dev stack):** editor placeholder/helper/counter; create w/ sentinel `xx-marker-12345-xx` (POST 201); playground picker + session-start (201) with "Character: Sarah CBT oly8" badge; token hint at 4,559 chars; delete (204). Live-chain proof: `select_personality`+`apply_personality` on the real saved row → `_router_messages`/`_answer_messages` carry the sentinel, identity before "Meeting instructions:". Screenshots: `.validation/Johnny-oly.8/01..04-*.png`.
- **Learnings:**
  - The default bootstrap "Johnny" still has the administrative seed description ("Default personality (inherits the globally active providers)."), so every default session now injects that line. Persona content for the default is **oly.9**'s scope (cyberpunk Silverhand vibe) — oly.8 only wires the mechanism, so the seed text was intentionally left alone.
  - `run-dev.sh`/`run.sh`'s host-port-5173 sweep killed Docker Desktop's backend (`com.docker.backend` holds 5173 when the dockerized frontend is up). Recovered with `open -a Docker`; bring the dev stack up via `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml docker compose up -d` to skip the sweep.
  - The stale `test_personality_e2e.py` asserting "description NOT injected" was a landmine — a sub-task's tests can encode a now-reversed decision; read them before assuming green = correct.
---
