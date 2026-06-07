# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Provider field schema lives in TWO validators that must stay in sync.** A
  `FieldDef` (backend `app/providers/schema.py`) is validated server-side by
  `app/providers/schema_validation.py::validate_payload` (→ HTTP 422) AND
  client-side by `frontend/src/lib/providers.ts::validateClient` (blocks the
  form *before* the request is sent, `+page.svelte` onSave). A SELECT-option
  rule added to one half but not the other produces "passes the API but the
  form won't submit" (or vice-versa). When you change SELECT semantics, change
  both, and mirror any new `FieldDef` flag into the TS `FieldDef` interface +
  `FieldDef.to_dict()` (the flag only reaches the frontend if `to_dict` emits it).
- **`dynamic_options=True` on a SELECT** = the dropdown is sourced from a live
  provider catalog (LLM model list), so the build-time `options` tuple is only
  an offline fallback. Both validators SKIP `value ∈ options` for such fields
  (only a length cap remains). Precedent for "flag on FieldDef that relaxes a
  rule + changes rendering" is `voice_catalog`.
- **The cloud-LLM model dropdown in `+page.svelte` is special-cased by
  `field.name === 'model' && kind === 'llm'`**, not by a schema flag. It renders
  the live `llmModelList` (from `GET /providers/{id}/llm_models` or
  `POST /providers/preview/llm_models`) and shows the saved value as a
  `"(saved)"` option when it's not in the live list — which is how an off-list
  model can be selected/re-saved even with no live catalog.

---

## 2026-06-08 - Johnny-ckz.29

Fixed: the model SELECT validator rejected any model not in the hardcoded
`options` tuple, so a freshly-released model returned by a provider's live
catalog couldn't be saved ("Model must be one of: ...").

- **What:** added a `dynamic_options: bool` flag to `FieldDef`. When set, both
  validators skip the `value ∈ options` membership check (a 200-char length cap
  is the only remaining constraint); the `options` tuple stays the offline
  fallback. Applied to the model field of gemini / openai / anthropic (the three
  cloud LLMs whose dropdown is sourced from a live catalog). openai-compatible's
  model is a free TEXT field, so it was never affected.
- **Files changed:**
  - `backend/app/providers/schema.py` — `FieldDef.dynamic_options` + serialize in `to_dict()`
  - `backend/app/providers/schema_validation.py` — `_check_option` skips membership for dynamic SELECTs; `_DYNAMIC_SELECT_MAX_LEN = 200` cap
  - `backend/app/providers/{gemini,openai,anthropic}_llm.py` — `dynamic_options=True` on the model field
  - `frontend/src/lib/providers.ts` — `dynamic_options?` on the TS `FieldDef`; `validateClient` skips membership when set
  - `backend/tests/providers/test_schema.py` — new tests for skip / enforce / length-cap / serialization; updated the OpenAI test that previously asserted model rejection
  - `frontend/src/lib/providersValidation.test.ts` — new `validateClient` unit test (static rejects, dynamic accepts)
- **Validation:**
  - backend: `pytest tests/providers tests/api/test_providers.py` green (4 unrelated pre-existing failures: 2 live-network S2S integration tests, 2 wizard tests needing a docker CLI in-container)
  - frontend: `pnpm check` 0 errors
  - live API: POST gemini w/ `gemini-3.5-pro-preview` → 201 (was 422); 250-char model → 422 "must be at most 200 characters"; static-select enforcement intact (unit + `_SchemaAwareLLM` API test)
  - **browser (chrome-devtools MCP):** edited a saved Gemini row whose model is off-list, clicked Save → `PATCH /providers/21` **200**, no "must be one of" error, button → "Saved". Screenshot: `.validation/Johnny-ckz.29/01-gemini-offlist-model-saved.png`
- **Learnings:**
  - The frontend `validateClient` blocks submit BEFORE the network call, so a backend-only fix would not have fixed the user-visible bug — the membership rule lived in two places.
  - `bd`/curl hitting `:8000/providers` unauthenticated returns `[]`; the browser is the authenticated source of truth for the providers list. POST/DELETE still work unauthenticated, so create/cleanup via curl is fine, but don't trust an unauthenticated GET for "what rows exist".
  - The served provider slug is `openai-compatible` (hyphen); using the underscore form on POST silently falls through to the legacy unvalidated path (201 + empty options) instead of 422 — a slug-mismatch trap, not a validation bug.
  - Unblocks Johnny-a9e: once OpenAI/Anthropic/Ollama live catalogs are wired, no further validator change is needed — the flag already relaxes them.

---

