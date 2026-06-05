# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Provider plan = source of truth for both API-level and agent-driven UI tests.** `backend/tests/e2e/providers_ui/plans.py` declares one `ProviderPlan` per (kind, backend) row. The Python runner (`runner.py`) and the chrome-devtools driver recipe (`ui_driver.py`) both consume the same plans so a new provider only needs one new row to be covered everywhere. Plans carry SKIP gates (`credential_env`, `options_env`, `local_asset`, `probe_url`) so missing keys / models never produce a FAIL.
- **`e2e_ui` pytest marker convention.** Opt-in end-to-end tests gate behind `pytest -m e2e_ui` (registered in `backend/pyproject.toml`). A session-scoped autouse fixture in `conftest.py` skips the whole selection with one actionable message when the API is unreachable instead of letting every test emit the same connection error.
- **Encrypted-credentials test rows.** When seeding `provider_credentials` rows for tests, use the public `POST /providers` endpoint — never write to the DB directly. The endpoint runs the Fernet encryption that production uses, so a test row exercises the same path as a UI-created row.

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

