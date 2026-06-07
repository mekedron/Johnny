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
- List + **right-side-panel modal** pattern: `frontend/src/routes/providers/+page.svelte` (modal `:1439-1571`). Clone it for new operator-config resources.
- Typed API client per resource: `frontend/src/lib/providers.ts` / `sessions.ts` export a `request<T>()` wrapper. Register backend routers in `backend/app/main.py:125-139`.

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

