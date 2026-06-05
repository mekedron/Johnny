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

