# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Regenerate uv.lock without running uv on the host (Docker-only rule)
The backend pins deps in `backend/pyproject.toml` + `backend/uv.lock`; the
Dockerfile runs `uv sync --frozen`, which FAILS if the lock is stale after a
pyproject edit. Regenerate the lock in a throwaway container so it writes back
to the host source tree (never run `uv` on the host):
```bash
docker run --rm -v "$PWD/backend":/w -w /w -e UV_LINK_MODE=copy \
  python:3.12-slim sh -c "pip install uv==0.11.19 && uv lock"
# verify consistency: ...&& uv lock --check  (exit 0)
```
Use the SAME uv version the Dockerfile pins (0.11.19) so the lock format matches.

### Run quality gates against the `--no-dev` prod image (non-destructive)
`backend/Dockerfile` builds with `uv sync --no-dev`, so the api/worker image has
NO pytest/ruff/mypy (a *running* container may still have them from an older
build — don't be fooled). To lint/type/test the baked image WITHOUT a full
re-sync that would prune livekit/torch, add the tools on top of `/opt/venv`:
```bash
docker compose run --rm --no-deps -v "$PWD/backend":/workspace -w /workspace api sh -c '
  uv pip install --python /opt/venv/bin/python pytest pytest-asyncio ruff mypy aiosqlite types-PyYAML
  ruff check johnny/agent tests/agent; pytest tests/agent -v; mypy johnny/agent tests/agent'
```
`tests/` is in `.dockerignore` (excluded from the prod image) — bind-mount
`./backend:/workspace` to make tests collectable. `docker compose exec api pytest`
only works when the running image happens to carry dev deps.

### Bake LiveKit Agents models at image-build time (offline-clean)
`python -m livekit.agents download-files` auto-discovers installed
`livekit-plugins-*` packages and fetches their model artifacts — NO agent
entrypoint needed. Set `HF_HOME`/`TORCH_HOME` (Dockerfile ENV) to a path OUTSIDE
`/workspace` and `/opt/venv` (e.g. `/opt/livekit-models/...`) so neither the
`run-dev.sh` source bind mount nor `uv sync` shadows the baked weights. Verify
offline with `-e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1` and re-running
download-files (exits 0 with no fetch). `MultilingualModel()` itself can't be
constructed outside a job context, so prove the bake via the offline re-run, not
by instantiating it in a bare `python -c`.

### johnny.agent import-safety
`johnny/agent/__init__.py` does NOT import `livekit` — only
`johnny.agent.session` (which subclasses `livekit.agents.Agent`) pulls the SDK
in, mirroring `voice_pipeline/livekit_transport.py`'s lazy discipline. Keeps
`import johnny.agent` collectable in any pytest env; guard SDK-backed tests with
`pytest.importorskip("livekit.agents")`. livekit `STT`/`LLM`/`TTS` are
`Generic[TEvent]` and `AgentSession` is `Generic[Userdata_T]` → annotate as
`[Any]` for strict mypy. A `[[tool.mypy.overrides]] module="livekit.*"
ignore_missing_imports=true` lets the dev/CI mypy (no `agent` extra) pass.

---

## 2026-06-08 - Johnny-jue

Phase 0 of the LiveKit Agents migration epic (Johnny-7g5): backend deps +
Dockerfile model bake + the `johnny/agent/` package skeleton.

**Implemented**
- `backend/pyproject.toml`: new `agent` optional extra pinning
  `livekit-agents[silero,turn-detector]==1.5.17` (the exact version the operator
  validated in the cloned `agent-starter-python/`). Self-hosted, so it does NOT
  pull LiveKit Cloud Inference or the `ai-coustics` plugin from the starter.
  Added a `livekit.*` mypy override (ignore_missing_imports).
- `backend/Dockerfile`: `--extra agent` on BOTH `uv sync` layers; `HF_HOME` +
  `TORCH_HOME` set to `/opt/livekit-models/...` (outside `/workspace` + `/opt/venv`);
  `RUN python -m livekit.agents download-files` after the deps layer to bake the
  Silero VAD + multilingual turn-detector models offline.
- `backend/uv.lock`: regenerated in a container (added livekit-agents/-silero/
  -turn-detector/-api/-rtc + deps; resolver downgraded protobuf 7.35.0→6.33.6).
- `backend/johnny/agent/`: `__init__.py` (import-safe, no livekit), `session.py`
  (`JohnnyAgent(Agent)` + `build_agent_session()` harness wiring silero VAD +
  MultilingualModel + `load_vad()`), `adapters/__init__.py` (Phase-1 placeholder).
- `backend/tests/agent/test_agent_package.py`: import smoke tests.

**Validated** (see `.validation/Johnny-jue/results.md`)
- live api: `import livekit.agents, livekit.plugins.silero` → exit 0; `/health` 200;
  worker healthy; all gemini/openai/s2s/deepgram providers import (protobuf
  downgrade safe). Models baked + load offline (HF_HUB_OFFLINE re-run, exit 0).
  ruff + mypy(strict) clean; `pytest tests/agent` → 3 passed.

**Learnings / gotchas**
- The `--no-dev` prod image has no pytest/ruff/mypy even though a *running*
  container may (stale image). Don't trust `docker compose exec api pytest`;
  add tools via `uv pip install` onto `/opt/venv` (see Codebase Patterns).
- `tests/` is `.dockerignore`d — bind-mount `./backend:/workspace` to collect.
- `download-files` needs no agent entrypoint; it discovers installed plugins.
  `MultilingualModel()` requires a job context, so prove the bake via the offline
  download-files re-run, not by instantiating the model in a bare `python -c`.
- Did NOT run `./stop.sh` (it's `down -v` and would wipe the operator's postgres
  volume / configured provider creds). Recreated api/worker in place; verified the
  clean-install model-bake independently on the freshly built image.

---

