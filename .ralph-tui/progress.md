# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Provider plan = source of truth for both API-level and agent-driven UI tests.** `backend/tests/e2e/providers_ui/plans.py` declares one `ProviderPlan` per (kind, backend) row. The Python runner (`runner.py`) and the chrome-devtools driver recipe (`ui_driver.py`) both consume the same plans so a new provider only needs one new row to be covered everywhere. Plans carry SKIP gates (`credential_env`, `options_env`, `local_asset`, `probe_url`) so missing keys / models never produce a FAIL.
- **`e2e_ui` pytest marker convention.** Opt-in end-to-end tests gate behind `pytest -m e2e_ui` (registered in `backend/pyproject.toml`). A session-scoped autouse fixture in `conftest.py` skips the whole selection with one actionable message when the API is unreachable instead of letting every test emit the same connection error.
- **Encrypted-credentials test rows.** When seeding `provider_credentials` rows for tests, use the public `POST /providers` endpoint — never write to the DB directly. The endpoint runs the Fernet encryption that production uses, so a test row exercises the same path as a UI-created row.
- **Readiness-first phase layout for journey tests.** Multi-phase E2E runs (Johnny-pdf, Johnny-f7k) precreate `tests/e2e/artifacts/<timestamp>/phase-N/` directories up-front and produce a single `report.json` whose top-level `summary` maps every phase to PASS / PARTIAL / FAIL / BLOCKED. When a phase is blocked, the JSON also lists the specific blockers (id, title, impact, remedy, related_beads) so the next runner can act without re-deriving the gap. Mirror the same data in `REPORT.md` for humans. The layout keeps every run comparable across iterations even when most phases are blocked, and matches the `report.json` schema produced by Johnny-upg so post-run dashboards can union the two.
- **Adapter `field_schema()` is the single source of truth for provider configuration.** Each concrete provider adapter under `backend/app/providers/` declares a `field_schema()` classmethod returning `ProviderSchema(kind, provider_name, display_name, summary, signup_url, fields=(FieldDef…))`. The same schema feeds (a) the `GET /providers/schemas` endpoint that drives the SvelteKit `/providers` UI, (b) the wizard prompts via `johnny.wizard.providers.schema_for(choice)`, and (c) the server-side validator (`app.providers.schema_validation.validate_payload` / `split_values`) that turns missing required keys / wrong select options into HTTP 422 with `{loc, msg, type}` envelopes. `FieldDef.secret=True` routes a field into the encrypted credentials blob; the rest go into the plain options JSONB. Adding a new field anywhere in the system means editing exactly one adapter — UI and CLI follow automatically.

---

## 2026-06-05 — Johnny-pdf

Master functional validation readiness audit (Johnny-pdf).

**What was implemented**
- Phase-0 readiness audit captured at `tests/e2e/artifacts/2026-06-05T23-41-28Z/`: structured `report.json` with PASS / PARTIAL / FAIL / BLOCKED per phase, human-readable `REPORT.md`, UI baseline screenshots (calendar, providers, templates), and raw API snapshots for every endpoint that backs a Phase 0 criterion (auth_accounts, providers, templates, calendar_events, sessions_active, health, test_event_meeting_config, docker_ps).
- Empty `phase-1/` through `phase-10/` directories precreated so the next (unblocked) run drops screenshots into the standard layout without reorganizing.
- Hard-blocker list attached to the Johnny-pdf bead notes via `bd update --notes`. Six blockers cataloged with impact, remedy, and related beads.

**Files changed**
- `tests/e2e/artifacts/2026-06-05T23-41-28Z/report.json` — structured roll-up.
- `tests/e2e/artifacts/2026-06-05T23-41-28Z/REPORT.md` — human-readable summary.
- `tests/e2e/artifacts/2026-06-05T23-41-28Z/phase-0/*.png|*.json|*.txt` — readiness evidence.
- `tests/e2e/artifacts/2026-06-05T23-41-28Z/phase-{1..10}/` — empty placeholders for the next run.
- Johnny-pdf bead notes — full blocker catalog and re-run criteria.

**Learnings**
- Johnny-pdf cannot run unattended today: it depends on Johnny-ckz Part A (join-stuck bug fix) AND Part B (`uv run python -m johnny.e2e --mode=<mode>` — `backend/johnny/e2e` does not yet exist) AND active STT/LLM/TTS providers AND the observer account `nikita.rabykin@gmail.com` being connected AND either a human observer or an automated audio-injection path (second-Playwright-participant playing fixture WAV is the bead-recommended approach but not built).
- `bd dep add <task> <epic>` rejects task → epic edges ("tasks can only block other tasks, not epics"). Record the relationship in the bead's free-text NOTES instead — it still shows up in `bd show` output and survives across pulls.
- The Test event (id=11) was authored by the bot account (`nikita.rabykin@aikamatkat.fi`) rather than the user account (`nikita.rabykin@gmail.com`) called out in the bead convention. Either rewrite the convention or recreate the event under the user account once that account is connected.
- The bot account's `token_expires_at` was within ~3 minutes of the audit start — token refresh will be required before any real join attempt the moment the wall clock crosses that boundary; the Johnny-q1x token-health surface is the right place for a re-auth prompt.
- Phase 0 IS valuable even when subsequent phases are blocked: it produces a stable diff target that turns "is the environment ready?" from a half-hour click-through into a single `report.json` comparison.

---

## 2026-06-05 — Johnny-upg

End-to-end UI test harness for provider management (Johnny-upg).

**What was implemented**
- Declarative provider matrix at `backend/tests/e2e/providers_ui/plans.py` covering 10 (kind, backend) rows: STT (deepgram, openai-realtime, faster-whisper); LLM (openai, anthropic, gemini, openai-compatible/Ollama); TTS (elevenlabs, openai, piper).
- API-level runner (`runner.py`) that walks each plan through POST → GET → /test → /activate → /delete with assertions plus three cross-cutting checks per kind (active-switch, invalid key rejection, duplicate display_name rejection).
- Preflight checks (`preflight.py`) that turn missing env keys / local-volume assets / unreachable probe URLs into SKIPs with actionable reasons.
- Two re-runnable entrypoints over the same plans: CLI (`uv run python -m tests.e2e.providers_ui --force`) and pytest (`uv run pytest -m e2e_ui`). Both produce the same `report.json` schema.
- Agent-driven UI walk via chrome-devtools-mcp recorded in `tests/e2e/artifacts/2026-06-05T23-31-00Z/screenshots/`: full lifecycle (open page → modal → submit → test OK → activate → delete) for the OpenAI LLM plan, with API-state assertions after every UI step.
- `backend/tests/e2e/providers_ui/ui_driver.py` documents the chrome-devtools-mcp recipe so future agent runs follow the same procedure. The functions return `UIAction` descriptors so the procedure stays testable.
- Docs at `docs/E2E_TESTING.md` cover the quick start, the matrix, the artifact layout, the agent UI walk, and how to add a new provider.
- Filed follow-up beads for the two real regressions the harness caught: Johnny-466 (openai-realtime adapter targets deprecated Beta API) and Johnny-jrd (ELEVENLABS_API_KEY in `.env` is actually a Google API key).

**Files changed**
- `backend/pyproject.toml` — registered the `e2e_ui` marker.
- `backend/tests/e2e/__init__.py`, `backend/tests/e2e/providers_ui/__init__.py` — new test package.
- `backend/tests/e2e/providers_ui/{plans,api,preflight,runner,report,ui_driver,__main__}.py` — harness modules.
- `backend/tests/e2e/providers_ui/{conftest,test_stt,test_llm,test_tts,test_edges}.py` — pytest layer.
- `tests/e2e/artifacts/2026-06-05T23-31-00Z/screenshots/*.png` and `ui_run.json` — artifacts from the chrome-devtools UI walk.
- `tests/e2e/artifacts/2026-06-05T23-3?-*Z/report.json` — JSON reports from the CLI runs.
- `docs/E2E_TESTING.md` — operator-facing guide.

**Learnings**
- The active-per-kind invariant is enforced both at the DB level (partial unique index on `(kind) WHERE is_active`) and at the API layer (`activate_provider` first deactivates siblings). The harness checks both: it activates row A, then row B, and asserts the LLM list has exactly one `is_active=true` row after each step.
- The Delete button on `/providers` uses `window.confirm()`. Driving it through chrome-devtools-mcp requires `evaluate_script` to patch `window.confirm = () => true` before clicking — otherwise the dialog hangs the snapshot poller.
- The API container reaches Ollama via `host.docker.internal:11434`, not `localhost`. The harness preflight probes the host's `localhost:11434/api/tags` (cheap reachability check) but fills the provider form with `http://host.docker.internal:11434/v1` so the API container can connect.
- Modern provider model names rot quickly: `claude-3-5-haiku-20241022` 404s on newer Anthropic accounts; `gemini-1.5-flash` and `gemini-2.0-flash` are retired on v1beta. Current safe defaults: `claude-haiku-4-5`, `gemini-2.5-flash`, `gpt-4o-mini`, `tts-1`.
- `httpx.HTTPError` (from `raise_for_status`) is the right exception class for the duplicate-name assertion — not a generic `Exception`. The 409 response carries `detail` which the SvelteKit client surfaces as `Error.message`.

---


## 2026-06-06 — Johnny-mma

Refactored provider settings UI from generic Credentials/Options textareas into per-provider structured forms (Johnny-mma).

**What was implemented**
- `backend/app/providers/schema.py` — `FieldDef`, `FieldGroup`, `FieldOption`, `FieldType`, `ProviderSchema` dataclasses. Each FieldDef declares name, label, type (text/password/url/number/select/checkbox/textarea), secret flag (drives credentials vs options split), required, default, options, help_text, signup_url, env_key, and group (auth/model/advanced).
- `backend/app/providers/schema_validation.py` — schema-driven `validate_payload()` (required, type, select-option checks) and `split_values()` (routes secrets to credentials, non-secrets to options, coerces numbers/checkboxes).
- Added `field_schema()` classmethod to `_ProviderBase` and concrete implementations on all 10 adapters (DeepgramSTT, OpenAIRealtimeSTT, FasterWhisperSTT, OpenAILLM, AnthropicLLM, GeminiLLM, OpenAICompatibleLLM, ElevenLabsTTS, OpenAITTS, PiperTTS).
- New endpoint `GET /providers/schemas` returns all registered provider schemas grouped by kind.
- POST/PATCH `/providers` now accept either the new flat `values` dict or the legacy `credentials`+`options` buckets. When the adapter declares a schema the payload is validated; missing required / wrong select option / non-numeric for number / non-URL for URL surface as field-level 422 errors with `{loc, msg, type}`.
- Frontend `lib/providers.ts` — new types (`FieldDef`, `ProviderSchema`, …), `listSchemas()`, client-side `validateClient()`, `initialValues()`, `groupedFields()`, and `ValidationFailure` exception that carries `{field: message}` for inline rendering.
- Frontend `/providers` page fully rewritten: schemas drive both the Add modal AND the inline Edit form. Each provider has its own structured form (labels + required markers + help text + signup links + grouped by Auth/Model/Advanced). Password fields masked, select fields use dropdowns, URL fields show protocol hints. No textareas remain.
- Wizard catalog gets a new `schema_for(choice)` helper that walks back to the runtime adapter's schema; new consistency test ensures wizard `credential_keys` stay in sync with the adapter's declared secret fields.
- Tests: `tests/providers/test_schema.py` (52 assertions over all 10 adapters covering required/type/select/URL/checkbox validation and the credentials/options split); 7 new API tests on `tests/api/test_providers.py` covering the `/schemas` endpoint, structured-payload create, missing-required 422, unknown-select 422, legacy-bucket still validated, update revalidation, and schemaless-provider fallback.
- Visual verification artifacts at `tests/e2e/artifacts/Johnny-mma-2026-06-06/`: empty providers page, Add modal for Anthropic LLM (cloud), Add modal for Local Piper TTS (local), inline Edit form, populated providers list with both cloud and local rows.

**Files changed**
- Backend new: `app/providers/schema.py`, `app/providers/schema_validation.py`, `tests/providers/test_schema.py`.
- Backend modified: `app/providers/base.py`, `app/providers/__init__.py`, all 10 adapter modules, `app/api/providers.py`, `johnny/wizard/providers.py`, `tests/api/test_providers.py`, `tests/wizard/test_providers_catalog.py`.
- Frontend modified: `src/lib/providers.ts`, `src/routes/providers/+page.svelte`.

**Learnings**
- The credentials-vs-options split is more usefully encoded as a per-field `secret` boolean than as separate dict shapes. The same flat `values` dict the frontend sends becomes the right shape for the schema-aware validator AND for the credentials/options split — the legacy two-dict API still works via a merge step so test fixtures and the e2e harness keep passing untouched.
- `{@const}` in Svelte 5 must be the immediate child of `{#if}` / `{#each}` / `{:else}` / `{#snippet}` / `{:then}` / `{:catch}` / a fragment. Nesting it inside a `<form>` that lives inside `{#if}` is rejected. The clean fix is to use `$derived(...)` in the script block instead — that gives you the same lazy computation with no template-placement constraints.
- HTML `required` attribute kicks in BEFORE Svelte `onsubmit` runs. To exercise client-side schema validation (the `validateClient()` path), use `required={field.required && !editing}` so empty-on-create still triggers browser validation but empty-on-edit (where blanks mean "keep existing secret") doesn't block submission.
- Anthropic adapter's old default model `claude-3-5-haiku-20241022` 404s on newer accounts; the schema's select option list now leads with `claude-haiku-4-5` to match the Johnny-upg learning. Same pattern for Gemini (`gemini-2.5-flash` first).

---

## 2026-06-06 — Johnny-kgc

Closed the Johnny — Google Meet AI Meeting Bot epic. All 39 child user stories (US-001 through US-034 plus follow-up beads Johnny-61y, Johnny-f7k, Johnny-mma, Johnny-mxx, Johnny-q1x) were already closed in prior iterations. This iteration is the epic close-out itself — no new code, just verification that every child is `✓` and a progress-log entry.

**What was verified**
- `bd show Johnny-kgc` reports `39/39 complete (100%) — eligible for close`. Every US-NNN child carries the `✓` glyph.
- The parent epic Johnny-ckz (join-stuck bug + Test-event harness) is the only remaining work in the Johnny-kgc tree and stays open: it is not a child of Johnny-kgc, so closing the bot epic does not affect it.
- `bd list --status=open` shows only three items left in the entire project: Johnny-ckz (parent epic), Johnny-466 (openai-realtime adapter deprecated), Johnny-jrd (mislabeled ELEVENLABS_API_KEY). Both Johnny-466 and Johnny-jrd were filed by the Johnny-upg harness and have remedies recorded in their bead notes — they are downstream of Johnny-kgc, not part of it.

**Files changed**
- `.ralph-tui/progress.md` — this entry.

**Learnings**
- A 100%-complete epic still shows up under `bd list --status=in_progress` until somebody explicitly runs `bd close`; the "eligible for close" line at the bottom of `bd show` is the only hint. Periodically scan in-progress epics whose children are all `✓` to keep the dashboard honest — a forgotten parent epic distorts both `bd ready` and `bd stats`.
- Closing a parent epic does NOT cascade to its children — closing Johnny-kgc leaves Johnny-ckz (its own parent), Johnny-466, and Johnny-jrd untouched. That is the right semantic (those issues are independent in scope) but means epic-close is purely a bookkeeping action: the value is in the closed signal, not in any side-effect on related work.
- The Codebase Patterns block at the top of `progress.md` is the right place for *durable* discoveries (provider plan = SoT, e2e_ui marker, `field_schema()` SoT); per-iteration learnings (specific provider model rot, Svelte 5 `{@const}` placement) stay in the dated entries so the top stays scannable.

---
