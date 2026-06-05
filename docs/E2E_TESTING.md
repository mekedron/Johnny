# End-to-end testing for provider management (Johnny-upg)

This harness drives the SvelteKit `/providers` page end-to-end against a
live Docker Compose stack. It exercises every supported backend (STT,
LLM, TTS) using the API keys present in `.env`, produces a per-run
JSON report, and exits non-zero on any FAIL so it can gate CI.

## Quick start

```bash
# 1. Bring the stack up
docker compose up -d

# 2. Populate .env (already done if you ran `johnny-setup`)
#    The harness reads OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY,
#    DEEPGRAM_API_KEY, ELEVENLABS_API_KEY, plus optionally OLLAMA_BASE_URL.
#    Blank values cause the corresponding provider to be SKIPped, never FAILed.

# 3. Run the API-level matrix
cd backend
set -a; source ../.env; set +a
uv run python -m tests.e2e.providers_ui --force
```

A successful run prints a PASS/SKIP/FAIL table and writes
`tests/e2e/artifacts/<UTC-timestamp>/report.json`. The script returns
exit code `0` when there are no FAILs (SKIPs are not failures).

## What the harness covers

For every entry in the matrix below the harness performs:

1. `POST /providers` with credentials + options
2. `GET /providers` — assert the row is visible with the right
   `display_name`, `kind`, and `credential_keys`
3. `POST /providers/{id}/test` — assert `ok=true`
4. `POST /providers/{id}/activate` — assert the row is the unique
   active provider for its kind
5. `DELETE /providers/{id}` — assert the row is gone

Plus three cross-cutting checks per kind:

- **Switch**: with two providers of the same kind, activate B and
  assert A is no longer active.
- **Invalid key**: a deliberately broken `api_key` must produce
  `ok=false` on `/test`.
- **Duplicate display_name**: the second create must return 409.

### Provider matrix

| Kind | Backend                       | Driven by                  | SKIPs when                          |
| ---- | ----------------------------- | -------------------------- | ----------------------------------- |
| STT  | `deepgram`                    | `DEEPGRAM_API_KEY`         | env var blank                       |
| STT  | `openai-realtime`             | `OPENAI_API_KEY`           | env var blank                       |
| STT  | `faster-whisper`              | `whisper_models` volume    | no `.bin` file under the volume     |
| LLM  | `openai`                      | `OPENAI_API_KEY`           | env var blank                       |
| LLM  | `anthropic`                   | `ANTHROPIC_API_KEY`        | env var blank                       |
| LLM  | `gemini`                      | `GOOGLE_API_KEY`           | env var blank                       |
| LLM  | `openai-compatible` (Ollama)  | localhost:11434 reachable  | port closed / Ollama not running    |
| TTS  | `elevenlabs`                  | `ELEVENLABS_API_KEY`       | env var blank                       |
| TTS  | `openai`                      | `OPENAI_API_KEY`           | env var blank                       |
| TTS  | `piper`                       | `piper_models` volume      | no voice file under the volume      |

## Artifact directory layout

```
tests/e2e/artifacts/<UTC-timestamp>/
    report.json              # CI-readable PASS/SKIP/FAIL with per-step detail
    ui_run.json              # (optional) agent-driven UI walk metadata
    screenshots/             # populated by the agent UI walk
        01-empty-state.png
        02-form-empty.png
        ...
```

`report.json` is the contract for CI. It mirrors the on-screen table
plus a `steps[]` array for each provider (each step has `name`, `ok`,
`detail`) and a top-level `totals` map.

## Running through pytest

The same matrix runs as a `pytest -m e2e_ui` selection so the harness
can live in CI without a CLI wrapper:

```bash
cd backend
set -a; source ../.env; set +a
uv run pytest -m e2e_ui -q
```

Each provider plan becomes one parametrised test (e.g.
`test_llm_provider_lifecycle[llm-anthropic]`). Tests SKIP via
`pytest.skip(reason)` instead of FAIL when the required env / asset is
missing — the `e2e_ui` marker keeps these tests out of the default
`pytest` run.

Default `pytest` (without `-m e2e_ui`) does **not** run these tests,
so unit-test feedback loops remain fast.

## Agent-driven UI walk (chrome-devtools-mcp)

The API matrix is the source of truth; the UI walk is the human-readable
sibling that proves the SvelteKit page itself still works. We do not
auto-run the UI walk in CI because chrome-devtools-mcp lives in the
agent's tool surface, not in pytest.

To re-run the UI walk interactively:

1. Confirm Compose is up and seeded (`uv run johnny-smoke`).
2. Ask the agent: *"Drive the providers UI for plan `<plan_id>` via
   chrome-devtools-mcp and save artifacts under
   `tests/e2e/artifacts/<stamp>/screenshots/`."*
3. The agent follows the actions described in
   `backend/tests/e2e/providers_ui/ui_driver.py` — open page, open
   modal, fill form, submit, test, activate, delete — capturing one
   screenshot per step.
4. After each step the agent verifies API state via `GET /providers`
   so the report reflects both halves of the assertion.

The `ui_driver.py` module returns ordered `UIAction` lists. Agents
should treat them as the canonical procedure and re-derive concrete
selectors via `mcp__chrome-devtools__take_snapshot` — selectors keyed
on accessible name (e.g. "Add provider", "Test", "Delete") survive
markup churn much better than CSS or XPath.

### Required browser dialog handling

The Delete button calls `window.confirm()`. Patch it before clicking:

```javascript
// via mcp__chrome-devtools__evaluate_script
() => { window.confirm = () => true; }
```

## Adding a new provider

1. Add a `ProviderPlan` row to
   `backend/tests/e2e/providers_ui/plans.py`. Pick a stable
   `display_name` with the `e2e-` prefix so the cleanup pass picks it
   up.
2. Set `credential_env` / `options_env` / `local_asset` / `probe_url`
   so the SKIP gate fires cleanly when prerequisites are absent.
3. Pick `static_options` that exercise the cheapest fast path
   (e.g. `gpt-4o-mini` for OpenAI LLM, `tts-1` for OpenAI TTS).
4. Re-run `uv run python -m tests.e2e.providers_ui --force` to confirm
   the new row PASSes against your local stack before merging.

## Known FAIL signals worth investigating

- `openai-realtime` STT FAIL with *"Realtime Beta API is no longer
  supported"* — adapter still targets the deprecated `realtime=v1`
  endpoint. Tracked separately.
- `elevenlabs` TTS FAIL with *"HTTP 401: Invalid API key"* — usually a
  mis-pasted key in `.env` (it's easy to swap with `GOOGLE_API_KEY`).
  Generate a fresh key in the ElevenLabs dashboard.
- Any LLM 404 *"model not found"* — model name in the plan needs
  refreshing against the provider's current catalog.
