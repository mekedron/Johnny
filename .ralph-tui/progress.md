# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Wizard subprocess shape**: every step that shells out (`docker`, `ollama`,
  `curl`) returns a small typed dataclass (`DownloadResult`, `ComposeResult`,
  `PrereqResult`) with `ok`, `detail`, and optional artifact name. Step
  functions surface those into a `StepResult` for the final report. Pattern
  lives in `backend/johnny/wizard/{models,compose,prereqs,steps}.py`.
- **Prompter protocol for testable UX**: `Prompter` is a tiny `Protocol` with
  `ask_text / ask_secret / ask_confirm / ask_choice`. `RichPrompter` is the
  interactive impl; `NonInteractivePrompter` reads from a flat dict (loaded
  from YAML) and uses `set_key()` to scope each prompt to a YAML key. Tests
  inject a `_ScriptedPrompter` instead — see
  `backend/tests/wizard/test_steps.py`.
- **Strict-mypy submodule imports**: `mypy --strict` rejects
  `module.submodule.X` via re-export unless the submodule is in `__all__`.
  In tests, import the submodule directly (`from johnny.wizard import
  prereqs`) and patch via the bare name. For stdlib indirection (e.g.
  `models.shutil.which`), use `patch("johnny.wizard.models.shutil.which")`
  with the full dotted path string instead of `patch.object`.
- **Typed exception subclasses for HTTP mapping**: when one error path
  needs a different HTTP status / detail shape than a sibling, subclass
  the broader exception so existing `except BroaderError:` catches keep
  working, and order the FastAPI `except` blocks subclass-first.
  Example: `TokenUndecryptableError(GoogleApiClientError)` carries
  structured `account_id` / `email`; `calendar.py` catches it first,
  returns 409 with `{code: "account_needs_reauth", …}`, then falls back
  to the generic 502 mapper.
- **`token_health` pattern for credential rows**: store a server-only
  `token_health: Literal["ok", "needs_reauth"]` on read-models, computed
  by attempting a no-op Fernet decrypt of the existing ciphertext column.
  Zero round-trips, fast enough for list endpoints. UI keys off the
  literal to render Reconnect affordances and skip doomed fetches
  client-side. See `backend/app/api/auth.py:_account_read` and
  `frontend/src/routes/calendar/+page.svelte` for the pattern.

---

## 2026-06-06 - Johnny-mxx
- Wrote `docs/SETUP_LOCAL.md` — single copy-paste guide for running Johnny
  100% locally end-to-end: prereqs, .env + FERNET_KEY, Google OAuth desktop
  client steps with direct console URLs, `docker compose up`, meet-worker
  build + selfcheck, faster-whisper model picker with HuggingFace links and
  pre-warm command, Ollama and vLLM LLM setup with exact pulls and
  base_urls, Piper `en_US-amy-medium` direct .onnx / .onnx.json download
  via the `piper_models` volume, Silero VAD confirmation, optional LiveKit
  dev server, Providers UI walkthrough with verbatim options matching each
  adapter (`model_size=base.en`, `voice_id=en_US-amy-medium`,
  `base_url=http://host.docker.internal:11434/v1`), first-run smoke test,
  troubleshooting for PulseAudio, missing model files, OAuth
  `redirect_uri_mismatch`, container OOM, host.docker.internal on Linux.
- Files changed: `docs/SETUP_LOCAL.md` (new), `.ralph-tui/progress.md`.
- **Learnings:**
  - Option keys in provider records map 1:1 to the adapter `options` dict
    (`backend/app/providers/{faster_whisper_stt,piper_tts,openai_compatible_llm}.py`),
    so the UI walkthrough can quote them verbatim.
  - The meet-worker `whisper_models` / `piper_models` named volumes are
    declared in `docker-compose.yml` and mounted at the same path the
    adapters default to (`/var/lib/johnny/{whisper,piper}-models`) — that
    means model pre-warming can be a one-shot `docker run` against the
    named volume without touching Compose.
  - Silero VAD ships in the meet-worker via the meet-worker base image's
    deps; the model itself is pulled lazily by torch on first call. No
    manual download needed.
---

## 2026-06-06 - Johnny-61y
- Built `backend/johnny/wizard/` package with the interactive setup
  wizard: prereq detection (Docker, Compose, uv, pnpm, Ollama, GPU,
  disk), `.env` + `FERNET_KEY` generation, Google OAuth walkthrough
  (opens Cloud Console in the browser, writes client ID/secret to
  `.env`), `docker compose up` + meet-worker build, per-kind cloud /
  local provider selection with model downloads (faster-whisper into
  `johnny_whisper_models` volume, Piper voice into `johnny_piper_models`
  volume, Ollama via host CLI), provider registration via
  `POST /providers` + activate, smoke tests via `POST /providers/{id}/test`,
  open UI. Modules: `cli.py`, `steps.py`, `prompts.py`, `prereqs.py`,
  `env_file.py`, `providers.py` (wizard-side catalog), `models.py`
  (downloads), `compose.py`, `api_client.py`.
- CLI exposed both ways: `uv run johnny-setup` (script entrypoint)
  and `uv run python -m johnny.wizard`. Re-runnable: each step detects
  existing state and offers to skip. `--non-interactive answers.yaml`
  reads canned answers; example at `docs/wizard-answers.example.yaml`.
- New deps: `rich`, `click`, `pyyaml`, `python-dotenv`, `types-PyYAML`
  (dev). Added `[project.scripts] johnny-setup = "johnny.wizard.cli:main"`.
- Updated `README.md` and `docs/SETUP_LOCAL.md` to point at the wizard
  as the recommended path; manual guide stays as the fallback reference.
- Tests: `tests/wizard/` adds 136 tests across env_file, prereqs,
  prompts, providers catalog, api_client, compose, models, steps, and
  the CLI. All 1287 backend tests pass, `ruff check` clean, `mypy` clean.
- Files changed: `backend/johnny/wizard/{__init__,__main__,cli,steps,prompts,prereqs,env_file,providers,models,compose,api_client}.py`,
  `backend/tests/wizard/test_*.py`, `backend/pyproject.toml`,
  `docs/SETUP_LOCAL.md`, `docs/wizard-answers.example.yaml`, `README.md`,
  `.ralph-tui/progress.md`.
- **Learnings:**
  - The wizard runs on the host *before* the Compose stack is up for
    `.env` setup, then *after* `compose up` for provider registration
    (which needs `/providers` reachable). Splitting the steps that way
    avoids needing a "pre-registered" provider config in `.env`.
  - Whisper and Piper downloads can run against the named Docker
    volumes the meet-worker mounts (`johnny_whisper_models`,
    `johnny_piper_models`) via one-shot `docker run` containers — no
    Compose changes, no host-side bind mounts.
  - `rich.prompt.Prompt.ask(choices=…)` is awkward for human-readable
    labels containing spaces/parentheses; we render a numbered menu
    via `Prompt.ask("Choice", default="1")` instead.
  - mypy `--strict` blocks `module.submodule.X` in tests unless the
    submodule is re-exported. Direct imports + `patch.object(module, …)`
    work; `patch("dotted.string")` is the escape hatch for stdlib
    indirection like `models.shutil.which`.
  - Ollama runs on the host, not in Compose, so the local-LLM provider
    record uses `base_url=http://host.docker.internal:11434/v1` to
    let the API container reach it. The wizard prompts for an
    `api_key` placeholder of `ollama` because the field is required by
    `POST /providers` even though Ollama ignores it.
---

## 2026-06-06 - Johnny-q1x
- Surfaced undecryptable Google refresh tokens as a recoverable state
  instead of a dead-end 502 in the Calendar view.
- Backend:
  - New `TokenUndecryptableError(GoogleApiClientError)` carrying
    structured `account_id` + `email`; raised from
    `GoogleApiClient._decrypt_refresh_token` and
    `revoke_account` when the stored ciphertext fails to decrypt.
  - `AccountRead` now exposes a `token_health: Literal["ok",
    "needs_reauth"]` field, computed via the new
    `can_decrypt_refresh_token(account, crypto)` helper. No Google
    round-trip — a single Fernet decrypt attempt against the existing
    column. Wired into list / get / patch endpoints via `_account_read`.
  - `GET /calendar/events` now catches the typed error first and
    returns HTTP 409 with `{code: "account_needs_reauth", account_id,
    email, message}` so the UI can branch. The generic 502 mapper
    still handles every other GoogleApiClient failure.
  - The calendar polling worker logs and *skips* accounts in this
    state rather than counting them as transient errors — the user has
    to act via the UI before the row can be polled again.
- Frontend:
  - `Account.token_health` field added; Settings page renders a
    "Token unreadable — reconnect" badge plus a primary **Reconnect**
    button that re-runs OAuth with the row's existing role / default
    flags (server-side upsert by email replaces the row in place).
  - Calendar page detects `token_health == "needs_reauth"` on the
    selected account, skips the `/calendar/events` fetch entirely, and
    renders an empty-state card with a deep link to
    `/settings#account-N`. Each account row gets `id="account-{id}"`
    so the hash navigates correctly.
- Docs:
  - `docs/SETUP_LOCAL.md` §3 now leads with a key-loss recovery
    callout pointing the user at Settings → Reconnect / Disconnect,
    plus a headless `curl` recovery snippet. The destructive
    "delete encrypted rows" suggestion is gone.
- Tests:
  - `tests/services/test_google_client.py` — 4 new tests covering the
    typed error and `can_decrypt_refresh_token` helper.
  - `tests/services/test_calendar_polling.py` — 1 new test confirming
    undecryptable accounts are skipped (zero errors, zero HTTP calls).
  - `tests/api/test_calendar.py` — 1 new test asserting the 409 shape.
  - `tests/api/test_auth.py` — 2 new tests confirming `token_health`
    on `GET /accounts` and `GET /accounts/{id}`.
  - 1297 backend tests pass (was 1289), `ruff` clean, `mypy` clean,
    `pnpm typecheck` and `pnpm lint` clean.
- Verified live via chrome-devtools MCP against the rebuilt Compose
  stack. Inserted a fake row whose ciphertext can't decrypt under the
  current `FERNET_KEY`; confirmed the badge / Reconnect button appear
  on /settings and the empty-state with the deep link appears on
  /calendar. Screenshots saved to
  `docs/screenshots/settings-reconnect-badge.png` and
  `docs/screenshots/calendar-reauth-empty.png`.
- Files changed:
  `backend/app/services/google_client.py`,
  `backend/app/services/calendar_polling.py`,
  `backend/app/api/auth.py`,
  `backend/app/api/calendar.py`,
  `backend/tests/services/test_google_client.py`,
  `backend/tests/services/test_calendar_polling.py`,
  `backend/tests/api/test_auth.py`,
  `backend/tests/api/test_calendar.py`,
  `frontend/src/lib/accounts.ts`,
  `frontend/src/routes/settings/+page.svelte`,
  `frontend/src/routes/calendar/+page.svelte`,
  `docs/SETUP_LOCAL.md`,
  `docs/screenshots/settings-reconnect-badge.png` (new),
  `docs/screenshots/calendar-reauth-empty.png` (new),
  `.ralph-tui/progress.md`.
- **Learnings:**
  - The subclass-first ordering of FastAPI `except` blocks matters:
    `TokenUndecryptableError` extends `GoogleApiClientError`, so the
    409 branch has to come before the generic 502 mapper or the parent
    swallows it.
  - SvelteKit Svelte 5 `$derived` on a state-array `find()` is the
    right shape for "the selected account": the picker store is
    `selectedAccountId` (number id, survives reloads), and the
    derived `selectedAccount` recomputes when either the id or the
    accounts list changes — including after `loadAccounts()` reruns
    post-OAuth.
  - When verifying live, the `johnny-api` container needs a full
    rebuild (`docker compose build api`) since the image bakes the
    source; bind-mount dev workflow is not wired in
    `docker-compose.yml`. Plan ~30s for the rebuild.
---

