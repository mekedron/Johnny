# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Roomless AgentSession smokes** (`johnny/agent/console_smoke.py`, `johnny/agent/sdk_surface_smoke.py`):
  build via `build_agent_session(..., turn_detection="vad")` (no job context), set
  `session.input.audio` / `session.output.audio` **before** `session.start(agent=...)`.
  `BrowserAudioOutput`'s estimated-playout contract (`johnny/agent/browser_audio_io.py`) is the
  reusable blind-sink pattern — the reply `SpeechHandle` only completes after the sink fires
  `on_playback_finished`, so any custom output MUST emit it (flush→timer, clear_buffer→interrupted).
- **Silero VAD does NOT detect DSP-synthetic audio** (noise, formant/harmonic "vowels" — verified
  empirically, zero events). Anything that needs the real VAD to fire must push real speech.
  In-image fixture: `johnny/agent/fixtures/sdk_smoke_speech.pcm` (16 kHz mono S16LE) — lives under
  `johnny/` because `tests/` is excluded from the prod image by `.dockerignore`.
- **Prod-shape stack iteration loop** (no backend bind mount): `docker cp` edited files into
  `johnny-api-1:/workspace/...` to iterate fast, then ALWAYS finish with
  `docker compose build api && docker compose up -d api` and re-run from the baked image.
  One-off quality gates against the host tree without switching stacks (sanctioned in
  PIPELINE.md §8.3): `docker compose run --rm --no-deps -v $PWD/backend:/workspace api sh -c
  "cd /workspace && ruff check ... && mypy ... && pytest ..."` (ruff/mypy/pytest all ship in the image).
- **livekit-agents 1.5.17 verified surface** (full semantics: docs/PIPELINE.md §9): `say()` done-callback
  is playout-gated and still fires on interrupt (`interrupted=True`) — safe as INV-1 ack terminal;
  `user_state_changed` (speaking|listening|away) fires roomless from VAD alone, no transcript needed;
  away timer arms at session start and re-arms on every listening edge (ctor knob `user_away_timeout`,
  default 15 s, NOT exposed by `build_agent_session` — Phase 5 must add the passthrough if needed).

---

## 2026-06-10 - Johnny-trt.2
- [SPIKE] Verified livekit-agents 1.5.17 session surface in-image: `AgentSession.say()` →
  `SpeechHandle` lifecycle (done-callback playout-gated; fires on interrupt with `interrupted=True`;
  `allow_interruptions=False` guard; completes even with no audio sink) and roomless
  `user_state_changed` (listening→away@2.00s with timer armed at session start, away→speaking on
  Silero onset, speaking→listening on EOS, away re-arms on every listening edge; fires from VAD
  alone, no transcript). 12/12 checks PASS from the baked image
  (`docker compose exec api python -m johnny.agent.sdk_surface_smoke`). Phase-5 fallback floor
  tracker documented as NOT required.
- Files changed:
  - `backend/johnny/agent/sdk_surface_smoke.py` (new — the smoke, sibling of console_smoke)
  - `backend/johnny/agent/fixtures/sdk_smoke_speech.pcm` + `fixtures/README.md` (new — in-image
    real-speech sample, provenance documented)
  - `backend/tests/agent/test_sdk_surface_smoke.py` (new — pytest wrapper, 3 tests; 9 passed with
    console smoke suite)
  - `docs/PIPELINE.md` (new §9 "Verified livekit-agents 1.5.17 session surface" + contents entry)
- **Learnings:**
  - Silero VAD rejects DSP-synthetic audio entirely (probed before building: white noise AND
    formant-shaped AM harmonics → zero events; real speech → clean START/END). Real-speech fixture
    is mandatory for VAD-driven smokes; shipped under `johnny/agent/fixtures/` since `tests/` never
    reaches the prod image.
  - `say()` interrupt resolves the handle ~immediately (0.00s) — it does not wait for the sink's
    interrupted `playback_finished` report; both still occur and the done-callback always fires.
  - Away timer (`user_away_timeout`, ctor-only, default 15s) arms at session start when both sides
    are listening and re-arms on every listening edge; `_set_user_away_timer` reads `_opts` at
    arm time, so the smoke shortens it by mutating `session._opts.user_away_timeout` pre-start
    (production code must add a `build_agent_session` passthrough instead).
  - The api stack was running prod-shape (no backend bind mount) — iterated via `docker cp` into
    `/workspace`, final-verified via image rebuild; quality gates run dev-mounted via
    `docker compose run --rm --no-deps -v` (ruff+mypy+pytest are all baked into the image).
  - User-state timings are reproducible to ±0.01s across runs (real-time-paced 20ms frames against
    a drift-free deadline) — strict sequence assertions are safe.
---
