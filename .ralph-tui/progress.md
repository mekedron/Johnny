# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

*Add reusable patterns discovered during development here.*

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

