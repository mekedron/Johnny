# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Per-session provider + mode resolution (the real seam)
- **Production provider resolution is `build_provider_payload` (`backend/app/services/provider_payload.py:46-81`)**, which reads every `provider_credentials` row where `is_active IS TRUE`. `load_active_providers` (`backend/app/providers/loader.py:42`) is **dead in production** — it appears only in tests + docstrings. Don't be misled by docstrings that name it.
- Live callsites of `build_provider_payload`: `browser_sessions.py:518` (`_build_spec_from_event`), `:603` (`_build_spec_playground`), `session_scheduler.py:388` (scheduled meet-worker launch). Per-session overrides are layered by `_resolve_provider_overrides` (`browser_sessions.py:429-486`) **without mutating DB rows**.
- The **meet-worker is DB-free**: the API serialises the resolved payload into `JOHNNY_PROVIDER_CONFIG` env and the worker rebuilds via `ProviderRegistry.instantiate`. So any per-session provider override MUST be applied API-side before serialisation, never inside the worker.
- Mode resolution: event `payload.mode or meeting.mode or "free_auto_speak"` (`browser_sessions.py:529-533`); playground `payload.mode or BotMode.FREE_AUTO_SPEAK.value` (`:598`). **`meeting_configs.mode` is NOT NULL** (`models.py:288`) — a real meeting always has a mode. There is no global "default mode" row.

### System-prompt assembly
- No DB-stored global "Johnny" prompt. Base persona strings are **hardcoded** in `pipeline.py:2080-2084` (router) / `2172-2175` (answer). Per-session text is set into `PipelineConfig` by the two spec builders (`browser_sessions.py:489/587`) and concatenated in `_router_messages` / `_answer_messages`. The only seam to inject session-scoped prompt text is `PipelineConfig.instructions` in those builders.

### Schema / migration conventions
- Integer autoincrement PKs everywhere (no UUIDs). `TimestampMixin` (`models.py:130-143`) for `created_at/updated_at`.
- Enums: `enum.StrEnum` + `SAEnum(..., native_enum=False, length=N, values_callable=_str_enum_values)` (`models.py:112-127`). Migrations store them as VARCHAR + CHECK via the `_in_list()` helper (see `0009_pipeline_settings.py`), never native PG enums (keeps SQLite tests working).
- Singleton / single-active rows use a **partial unique index** (`0002_provider_active_unique.py:21-27`; `models.py:537-543`): `Index(name, col, unique=True, postgresql_where=text(...), sqlite_where=text(...))`.
- Bootstrap seed inside the migration via `op.execute("INSERT ... WHERE NOT EXISTS ...")` (idempotent), as `0009` does for `pipeline_settings`. Migrations run at boot (`app/db/bootstrap.py:bootstrap`, called from `main.py:48` lifespan) BEFORE `seed_providers_from_file` (`main.py:62-65`).
- Boot-time drift guard `check_model_db_drift` (`bootstrap.py:78-106`) aborts if an ORM model has no matching migration — model + migration must land together.

### Frontend conventions
- List + **right-side-panel modal** pattern: `frontend/src/routes/providers/+page.svelte` (modal `:1439-1571`). Clone it for new operator-config resources. The **templates page** (`frontend/src/routes/templates/+page.svelte`) is a *simpler* clone target for a plain CRUD list + side-panel + delete-confirm dialog than the giant providers page — use it as the skeleton, graft in only the bits you need from providers.
- Typed API client per resource: `frontend/src/lib/providers.ts` / `sessions.ts` / `templates.ts` each export their **own** copy of the `request<T>()` + `extractDetail()` wrapper (the convention is duplication-per-resource, NOT a shared module — follow it). Register backend routers in `backend/app/main.py:125-139`.
- **`VoicePicker` (`$lib/components/settings/VoicePicker.svelte`) for a SAVED provider row**: pass `providerId={row.id}` and it fetches `GET /providers/{id}/voices` directly (no draft-preview path needed). Props: `kind="tts"`, `providerName`, `providerId`, `values={row.options}`, `value` (current voice id), `onSelect`. Omit `onInstall`/`onRemove` unless you're in the Piper draft flow. The selected voice is just a string id — store it wherever your resource wants.
- **Personalities have no voice column**: the pinned TTS voice lives in `personality.metadata.tts_options.voice_id` (PRD §8.6 — the resolver will merge `metadata.tts_options` into the TTS payload `options`). Read/write via `readVoiceId()` / `writeVoiceId()` in `$lib/personalities.ts` (they preserve unrelated metadata keys).
- **SvelteKit reserves the `+` filename prefix** — `+page.test.ts` (or any `+`-prefixed non-route file) is illegal and **breaks `svelte-kit sync`** ("Files prefixed with + are reserved"). Co-locate route tests as `page.test.ts` (no `+`) instead; SvelteKit ignores non-`+` files in `routes/`.
- **No standing frontend test runner** (zero `pnpm test`; `@types/node` present, Node 20 in the container). For pure-logic unit tests, `node:test` + `node:assert/strict` type-check clean under svelte-check with **zero new deps**. To actually run a `.ts` suite locally: `tsc`-transpile to a temp dir (drop `$lib` type-only imports, rewrite alias imports to relative `.js`, set `--module esnext --moduleResolution bundler --typeRoots .../@types --types node`) then `node --test`. Adding vitest is blocked today by `ERR_PNPM_UNEXPECTED_STORE` against the live container's node_modules volume (tracked in Johnny-vzb).

### Running backend quality gates (Docker) — non-obvious
- The baked **prod** api image (`./run.sh`) is built `--no-dev` (no pytest/ruff/mypy) and its `Dockerfile` only `COPY`s `alembic app johnny` — **not `tests/`**, and WORKDIR is `/workspace` (not `/app`). So `docker compose exec api pytest` fails two ways: no pytest, no tests dir.
- Run gates without disturbing the running stack via a throwaway container that bind-mounts source + lets `uv run` install the `dev` group on the fly:
  `docker compose run --rm --no-deps -v "$(pwd)/backend:/workspace" -w /workspace api uv run pytest|mypy|ruff …`
- Tests use in-memory SQLite + `JOHNNY_DB_BOOTSTRAP=off` (top-level `tests/conftest.py`), so no Postgres needed. Enable `PRAGMA foreign_keys=ON` via a `connect` event if you need to exercise `ON DELETE SET NULL`.
- **Validate Postgres-specific migration DDL in isolation** (SQLite can't prove `TRUE` literals / JSONB defaults / `postgresql_where` partial indexes): `CREATE DATABASE johnny_mig_test`, run the real boot path against it with `-e DATABASE_URL=…/johnny_mig_test api uv run python -c "from app.db.bootstrap import bootstrap; bootstrap()"` (migrations + drift check), inspect with `psql`, `alembic downgrade -1`, then `DROP DATABASE`. Never touches the user's `johnny` DB.
- **mypy/ruff baseline is NOT clean**: ~83 pre-existing mypy errors across ~30 files (incl. many `tests/*`) and ~32 ruff errors. CI does not gate on full `uv run mypy`. "mypy/ruff clean" acceptance = *don't add new errors in your files*, not *repo is clean*.

---

## 2026-06-07 - Johnny-oly.1

- Wrote the design PRD for the personality library at `tasks/prd-personality-library.md` (pure design pass; no code). Traced the current provider-resolution + system-prompt-assembly pipeline end-to-end and specified how personalities slot in.
- Files changed:
  - `tasks/prd-personality-library.md` (new) — Context / Goals / Non-goals + §1–§8 (system-prompt trace, provider-resolution callsites, data model, override resolution rules, UI placement, migration plan, test plan, open questions) + Acceptance + Out of scope. Two Mermaid diagrams.
  - `.ralph-tui/progress.md` — this entry + Codebase Patterns above.
- **Learnings:**
  - The bead assumed `load_active_providers` is the provider-resolution point; it is actually **unused in production** (tests/docstrings only). The real seam is `build_provider_payload` (`provider_payload.py:46`) + `_resolve_provider_overrides` (`browser_sessions.py:429`), called at `browser_sessions.py:518/603` and `session_scheduler.py:388`. Documented the correction in the PRD rather than papering over it.
  - `meeting_configs.mode` is `NOT NULL`, so `personality.default_mode` can't cleanly "override" a calendar meeting's mode — it can only seed new meetings + fill the playground default. This shaped the recommended mode precedence (operator must ratify; PRD §4c/§8.4).
  - Designed the bootstrap "Johnny" personality with **NULL** provider FKs (= inherit global active) rather than copying today's active provider ids — copying would pin Johnny to a snapshot and silently diverge when the operator later activates a different provider. NULL also dodges the migration-vs-lifespan seed ordering problem (`main.py:48` migrations run before `:62-65` provider seeding).
  - A "personality" per the bead's column list is a provider+voice+mode bundle with **no prompt-text column** — orthogonal to the existing `ProfileTemplate` (which owns prompt/behavior). Flagged the relationship explicitly so downstream sub-tasks don't duplicate prompt storage.
  - No browser validation applies: documentation-only change, no runtime/UI surface (per the CLAUDE.md carve-out for changes that can't be browser-tested).

---

## 2026-06-08 - Johnny-oly.2

- Shipped the backend half of the personality library: schema + CRUD API + bootstrap migration. No behaviour change for an operator who never touches the page.
- Files changed:
  - `backend/app/db/models.py` — new `Personality` model (after `PipelineSettings`). Integer PK + `TimestampMixin`; `display_name` unique; `description` text; `is_default` bool with partial unique index `uq_personalities_single_default WHERE is_default`; `llm_provider_id`/`tts_provider_id` FK → `provider_credentials.id` `ON DELETE SET NULL`; `default_mode` nullable `_bot_mode_column()`; `extra_metadata` mapped to DB column `metadata` (`metadata` is reserved on the declarative `Base`).
  - `backend/alembic/versions/0014_personalities.py` (new, `down_revision="0013"`) — creates `personalities`, partial unique index, seeds one `Johnny` default with **NULL** FKs/mode (server defaults fill `metadata`/timestamps so the INSERT is Postgres+SQLite portable). Idempotent (table-exists guard + `WHERE NOT EXISTS`); downgrade drops index+table.
  - `backend/app/api/personalities.py` (new) — `APIRouter(prefix="/personalities")`: list (default-first), create, `clone` (`"<name> (copy)"`, disambiguated), get, patch, delete (409 on default), `set-default` (atomic deactivate-siblings-then-flip, mirrors `providers.activate_provider`). Plain Pydantic models (no encrypted creds → no `ProviderSchema` needed); FK kind-validation → 422; duplicate name → 409.
  - `backend/app/main.py` — import + `include_router(personalities_router)` beside providers.
  - `backend/tests/api/test_personalities.py` (new, 42 cases w/ the other files) — every endpoint happy/422/404/409, set-default atomicity under alternating POSTs, `ON DELETE SET NULL` (PRAGMA foreign_keys=ON), refuse-delete-default, clean `metadata` wire name.
  - `backend/tests/test_migration_0014.py` + `backend/tests/test_db_personalities.py` (new) — seed/idempotency/downgrade + single-default enforcement; model shape (columns, SET-NULL FKs, partial index).
- Verified: 42 new tests pass; ruff clean + mypy clean on the new files; full chain `0001→0014` applied to a throwaway Postgres `johnny_mig_test` via the real `bootstrap()` path (drift check passed), seed/partial-index/second-default-rejected confirmed, `downgrade -1` dropped only `personalities`, then dropped the DB. User's `johnny` DB never touched.
- **Scope boundary:** left `meeting_configs.personality_id` OUT of 0014 even though PRD §6b bundles it — bead .2's section B is explicitly the `personalities` table only, and the selection-precedence column is the resolver's concern (Johnny-oly.3 can add it in 0015). Kept .2's blast radius to one table.
- **Learnings:**
  - The set-default atomicity invariant rides entirely on the partial unique index + deactivate-siblings-first ordering (same as `activate_provider`); confirmed on Postgres that a direct second `is_default=TRUE` INSERT is rejected by `uq_personalities_single_default`.
  - `extra_metadata`↔column `metadata` aliasing: Read uses `serialization_alias="metadata"` (and `from_attributes` reads the ORM attr by field name — DON'T give it validation_alias `metadata`, or Pydantic would `getattr(obj,"metadata")` = SQLAlchemy's MetaData object). Create/Update use `alias="metadata"` + `populate_by_name=True`.
  - Browser-validation carve-out applies: this sub-task has **no UI surface** (the `/personalities` page is .4/.5); the new REST endpoints aren't wired into any rendered page yet. Covered by TestClient integration tests + the isolated-Postgres migration run instead.
  - See the new "Running backend quality gates (Docker)" pattern at the top — the prod image has no pytest and no `tests/`; `docker compose run --rm … uv run …` is the way.

---

## 2026-06-08 - Johnny-oly.4

- Shipped the `/personalities` management page (frontend half): list / create / clone / edit / delete / set-default, wired to the .oly.2 CRUD API, with the Johnny-1ge.8 `VoicePicker` for per-personality voice selection.
- Files changed:
  - `frontend/src/lib/personalities.ts` (new) — typed client (own `request<T>()`/`extractDetail()` copy) for list/get/create/update/clone/delete/set-default; `readVoiceId`/`writeVoiceId` metadata helpers (voice lives at `metadata.tts_options.voice_id`, unrelated keys preserved); pure `validatePersonalityForm()` + `DISPLAY_NAME_MAX` extracted for unit testing.
  - `frontend/src/routes/personalities/+page.svelte` (new) — cloned the **templates** page skeleton (list cards, right side-panel modal, delete-confirm dialog, `Escape` handling) + grafted the providers page's `VoicePicker` usage. Default badge + bordered card; per-row Edit/Clone/Set-default/Delete (set-default + delete disabled on the default row with tooltips). Editor: name (unique-validated, maxlength 128), auto-resizing monospace description w/ char counter + example placeholder, LLM select (active rows + "(inactive)" fallback for an edited row pointing at a deactivated provider), TTS select that resets the voice on change, conditional `VoicePicker`, default-mode select with "Use meeting/playground default" blank.
  - `frontend/src/routes/+layout.svelte` — nav entry "Personalities" inserted between Templates and Providers.
  - `frontend/src/routes/personalities/page.test.ts` (new) — 29 `node:test` unit tests (every display-name path incl. boundary/self-edit/case-sensitivity, the voice/tts coupling error, and full `readVoiceId`/`writeVoiceId` round-trips). **Proven passing 29/29** via a one-off `tsc`-transpile + `node --test`.
- Verified: `pnpm typecheck` 0 errors / 0 warnings, `pnpm lint` clean. **Full chrome-devtools MCP browser run** (artifacts in `.validation/Johnny-oly.4/`): rendered the page, created "Friendly Customer Support" from scratch (LLM "Google Gemini · gemini-2.5-flash", TTS "Local Piper · ar_JO-kareem-low" via the VoicePicker `Use` button, "Suggest only" badge), cloned Johnny → editor opened on the copy → renamed/re-LLM'd/re-moded → saved, set the clone as default (badge moved + list reordered + delete-default protection followed the default), reverted to Johnny, hit the live duplicate-name validation, `Escape`-closed the modal, deleted via the confirm dialog. Zero console errors throughout. Cleaned up the test personalities so the operator's DB is back to just bootstrap Johnny.
- **Decisions / deviations (each ratified by a hard constraint):**
  - **Filename `page.test.ts`, not the bead's `+page.test.ts`** — SvelteKit reserves the `+` prefix and `+page.test.ts` literally breaks `svelte-kit sync`. Co-located, same directory, legal name.
  - **`node:test` instead of vitest** — `pnpm add -D vitest` fails against the live container's node_modules volume (`ERR_PNPM_UNEXPECTED_STORE`), and forcing a reinstall would re-resolve every dep against bleeding-edge Vite 8 with 2 active sessions live. `node:test` needs zero new deps and keeps the typecheck/lint gate green. Standing-runner wiring filed as **Johnny-vzb** (P3).
  - **Empty-state CTA is "New personality", not literally "Start from Johnny"** — the bootstrap Johnny default can't be deleted, so the list is never empty *while a Johnny exists to clone*; the acceptance's "empty state offers Start from Johnny" is an unreachable premise. The per-row **Clone** on the Johnny card is the always-available "start from Johnny" path.
- **Learnings:**
  - `VoicePicker` for a *saved* provider only needs `providerId` — it fetches `GET /providers/{id}/voices` itself; no draft `values`/preview plumbing required (still passed `values={row.options}` to satisfy the prop). Selecting flips `Use`→`Selected (disabled)` and the picker is self-contained (owns its preview audio).
  - The provider-FK selects must tolerate an edited personality pointing at a now-inactive/deleted provider — append a synthetic "(inactive)"/"(unavailable)" option so the selection isn't silently dropped (`selectOptions()`); the backend only validates FK *kind*, not active-state.
  - A `.ts` module that runs both in Vite and under plain `node` must guard `import.meta.env?.` — bare `import.meta.env.X` throws under `node --test` (env is undefined). One `?.` made `personalities.ts` loadable by the test harness without touching its Vite behaviour.
  - See the expanded **Frontend conventions** block at the top (templates-as-skeleton, per-resource `request<T>` duplication, VoicePicker-for-saved-row, metadata voice storage, the `+`-prefix trap, and the zero-dep `node:test` run recipe).

---

