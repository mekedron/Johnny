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

### run.sh / stop.sh port-5173 sweep now SKIPS Docker's own proxy (was a footgun, now fixed)
Earlier the host port-5173 sweep killed Docker's own proxy (`com.docker.backend`)
and took the daemon down when the dockerized frontend was already up. Both
scripts now skip `com.docker.*|*vpnkit*|*docker-proxy*` (run.sh:57, stop.sh:57),
so a plain `./stop.sh && ./run.sh` clean-install cycle is SAFE even with the
stack up — it only kills a genuine host-side stray vite on 5173. (Bypass-the-
sweep alternative, if ever needed again:
`COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml docker compose up -d`.)

### The prod image (`./run.sh`) does NOT bake `tests/` — only alembic/app/johnny
`backend/Dockerfile` COPYs `alembic`, `app`, `johnny` (+ pyproject/uv.lock/
alembic.ini) but NOT `tests/`, so `docker compose exec api uv run --group dev
pytest tests/...` fails with "file or directory not found" on a `./run.sh`
(prod-shape) stack. Two ways to run backend tests:
- `./run-dev.sh` bind-mounts `./backend` → `/workspace`, exposing `tests/` live; OR
- on a prod stack, `docker compose cp ./backend/tests api:/workspace/tests`
  first (ephemeral — gone on next recreate), then `uv run --group dev pytest`.
Gotcha: `docker compose cp ./backend/<dir> api:/workspace/<dir>` of a DIR can
nest (`/workspace/<dir>/<dir>`), so a re-synced file looks unchanged; to
overwrite a single baked file use the explicit file→file form:
`docker compose cp ./backend/x/y.py api:/workspace/x/y.py`.

### Editing an already-applied SEED migration in place is correct here (clean-install model)
`./stop.sh` does `docker compose down -v` (wipes `postgres_data`), so every
validation cycle re-runs ALL migrations from zero on a fresh DB. That means a
change to seed text in an already-applied migration (e.g. the `INSERT INTO
personalities` in `0014`) is picked up by editing the migration IN PLACE — no
new forward-migration UPDATE needed. Alembic tracks only revision IDs (not file
hashes), so editing an applied migration's body is invisible on an existing DB
but takes effect on the next clean install — which is the only path this project
ships through. Multi-line / apostrophe-bearing seed text: keep the migration
portable (no PG `E'...'`), embed real `\n` (valid inside a single-quoted literal
on both PG + SQLite), and double apostrophes via `.replace("'", "''")` at INSERT.

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

## 2026-06-08 - Johnny-oly.9
- **What:** Rewrote the bootstrap "Johnny" default personality's seed `description`
  from the administrative placeholder ("Default personality (inherits the globally
  active providers).") into a cyberpunk Johnny-Silverhand-protege persona, per the
  operator directive. Since oly.8 injects the `description` verbatim into the session
  system prompt, this is now the voice the default bot speaks in out of the box.
- **Persona choices (for future agents — intent, not just text):** Voice = Night City
  rockerboy/anti-corpo swagger (chrome, "choom", "burn through the static", "Wake 'em
  up") but deliberately kept FUNCTIONAL as a meeting-assistant system prompt: an
  explicit Always/Never block steers it to "be genuinely useful / no yes-man / cut the
  corpo-speak" so the attitude never makes the bot useless or hostile. No profanity in
  the shipped default (real Silverhand swears) — a tasteful edge reads better as a
  default and the operator can crank it up via clone-and-edit. Length ~865 chars (well
  under the 4k token-cost hint). It is a STARTING point, not forever-locked.
- **Files changed:**
  - `backend/alembic/versions/0014_personalities.py` — new module constant
    `JOHNNY_DEFAULT_DESCRIPTION` (readable prose w/ natural apostrophes + `\n\n`
    paragraph breaks); seed INSERT now interpolates it via `.replace("'", "''")`
    (SQL-quote escaping) instead of the hardcoded placeholder; comment updated.
  - `backend/tests/test_migration_0014.py` — `test_upgrade_creates_table_and_seeds_johnny`
    now asserts `johnny.description == _load_migration_module().JOHNNY_DEFAULT_DESCRIPTION`,
    pinning the persona to the source-of-truth constant and exercising the SQL round-trip
    (doubled apostrophes + literal newlines) on SQLite.
  - `README.md` — "Personalities" section: the bootstrap default now described as
    inheriting global providers AND shipping with a built-in cyberpunk-rockerboy
    character (clone-and-edit for a tamer default); dropped the now-false "sees no
    change" tail (post-oly.8 the default DOES inject persona text).
- **Quality gates (all GREEN):** `ruff check` clean, `mypy` clean, `ruff format --check`
  clean on the migration (note: `ruff format` is NOT an enforced gate — README lists only
  pytest/ruff check/mypy — and the test file has a PRE-EXISTING `_MIGRATION_FILE` format
  deviation on HEAD that I left out of scope). pytest: 212 passed across
  migration_0014 + personality_resolver + voice_pipeline.
- **Browser validation (chrome-devtools MCP, live `./run.sh` stack):** `/personalities`
  card shows the "Johnny" Default row with the full persona + "Global default LLM/TTS";
  Edit dialog's Description textarea renders the multi-line persona (paragraph breaks
  intact) with the char counter at 865 and all three provider/mode selects on "Use global
  default". Screenshots: `.validation/Johnny-oly.9/01-default-card.png`,
  `02-edit-persona-textarea.png`.
- **Clean-install + injection proof:** validated via the canonical `./stop.sh && ./run.sh`
  (down -v → fresh migrate); `psql` confirmed the seeded row (NULL FKs/mode preserved,
  865-char description). Live runtime check: `build_personality_system_prompt(<seeded
  default>)` → `[personality: Johnny]\n<persona>` (identity header present, "Night City"
  + "choom" present, 2 paragraph breaks preserved) — the new text reaches the system
  prompt at session start with no regression to the oly.8 injection chain.
- **Learnings:**
  - Prod image doesn't bake `tests/`; clean-install re-runs all migrations so an
    in-place seed edit is the right move; the run.sh/stop.sh 5173 sweep is now
    daemon-safe. (All three promoted to Codebase Patterns at top.)
  - The migration test pinned `display_name`/flags/NULL-FKs but NOT the description, so
    the text was free to change without breaking it — I added the description assertion
    so a future accidental revert of the seed text fails loudly.
---
