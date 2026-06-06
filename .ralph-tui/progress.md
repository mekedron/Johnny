# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Provider settings state model (Johnny-stt.7)

`frontend/src/routes/providers/+page.svelte` keys all form / selection
state by an opaque **DraftKey**, not by `(kind, provider_name)`. A DraftKey
is either:
- `instance-<id>` — refers to an existing `ProviderCredential` row
- `new-<provider_name>` — refers to an unsaved draft

This model lets users have N instances of the same `(kind, provider_name)` —
each with its own form values, errors, and display name — without state
collisions. The catalog left panel renders TWO sections per tab:
"Configured" (one card per existing row) and "Add a new …" (one card per
registered provider kind, acting as "+ <name>" creators). Saving a "new-X"
draft swaps the selection to `instance-<newly-created-id>` so the user sees
their freshly-saved row in the right panel.

### Backend unique constraints on ProviderCredential

The DB constraint is `UniqueConstraint("kind", "provider_name", "display_name")`
— it allows MANY rows per `(kind, provider_name)` as long as each has a
distinct `display_name`. The active-default partial unique index on
`(kind) WHERE is_active` enforces exactly one active row per kind (the
"default") regardless of how many instances exist.

When raising HTTP 409 on the duplicate path, include the offending kind +
display_name in the message so the user knows what to change — the
previous generic "conflicts with another provider with the same
kind/name/display" was unhelpful and helped trigger the Johnny-stt.7
regression report.

### Docker frontend has no source bind-mount

`docker-compose.yml`'s `frontend` service builds an image with the source
baked in — there is NO `volumes: - ./frontend:/workspace` bind mount. When
iterating on `frontend/src/**/*.svelte` against the running stack, either
rebuild (`docker compose up -d --build frontend`) or copy the file into the
running container with `docker cp <host-path> johnny-frontend-1:/workspace/<container-path>`
so Vite's HMR picks it up. Vite hot-reloads on file change once the file is
inside the container — no container restart needed.

---

## 2026-06-06 - Johnny-stt.7
- **Backend:** `backend/app/api/providers.py` — improved HTTP 409 error
  messages in `create_provider()` and `update_provider()` to name the
  offending kind + display_name and explain the multi-instance contract.
- **Backend tests:** `backend/tests/api/test_providers.py` — added three
  regression tests:
  - `test_create_multiple_instances_same_kind_and_name`: confirms 3 Ollama
    instances with distinct display names all save with 201.
  - `test_create_second_instance_with_duplicate_display_name_returns_409`:
    locks the legitimate 409 surface (display_name reuse).
  - `test_each_instance_selectable_independently`: confirms separate config
    per instance row.
- **Frontend:** `frontend/src/routes/providers/+page.svelte` — major
  rewrite of state model and left-panel layout. Form/selection/test state
  now keyed by `DraftKey` (`instance-<id>` or `new-<providerName>`)
  instead of `(kind, provider_name)`. Left panel renders "Configured (N)"
  + "Add a new …" sections. STT and generic test state now keyed by row id
  (previously `provider_name`). The `suggestDisplayName` helper proposes a
  numbered suffix (`(2)`, `(3)`, …) when the catalog's default name is
  already taken — so adding multiple instances of the same kind doesn't
  immediately trigger 409 on save.
- **Validation:** chrome-devtools MCP — navigated to /providers, drove
  "+ OpenAI-compatible" twice to create "Ollama Llama 3 8B" and
  "Ollama Qwen 35B" (both saved with HTTP 201, both appeared in the
  Configured (5) list independently), confirmed the existing "Ollama
  Qwen No Reason Uncensored" row remained selectable with its own config,
  verified the playground LLM override dropdown listed all 5 instances by
  display name. Cleaned up the test instances after. Screenshot at
  `.ralph-tui/screenshots/johnny-stt7-providers-multi-instance.png`.
- **Learnings:**
  - The DB schema and API already supported N instances per (kind,
    provider_name) — the constraint is on the (kind, provider_name,
    display_name) triple. The bug was purely in the frontend: it modelled
    the UI as "one config per provider_name" and offered no "+ Add" path,
    so users were trapped at N=1 even though the backend would have
    accepted N>1.
  - Svelte 5 only allows `{@const}` as an immediate child of control-flow
    blocks (`{#if}`, `{#each}`, `{:else}`, etc.), NOT inside a regular
    HTML element. Hoist constants up into the surrounding `{#if}` block.
  - Wrap row-state hydration in a single `ensureFormStateForInstance(kind,
    row)` helper that is idempotent — calling it from both `load()` (after
    every refresh) and `selectInstance()` keeps form drafts consistent
    even when the row list changes from under the user (e.g. after a
    delete + create).
---

