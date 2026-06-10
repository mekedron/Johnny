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
- **Replay-fixture authoring loop** (`tests/fixtures/sessions/README.md`): hand-author
  `router`/`answer` per turn, predict `recorded` from the gate semantics
  (`replied`→`spoken`; declined/low-confidence→`no_reply`/`suppressed`; sub-threshold speak
  verdicts keep `should_speak: true`; rate limit off by default), then verify with
  `johnny-replay --session-id <id> --mode regression` — MATCH proves the baseline equals current
  engine behaviour, and on mismatch it prints the replayed values (doubles as capture). Split
  fixtures run on the agent engine; `tests/` never reaches the prod image, so replay/pytest run
  via the §8.3 one-off (`docker compose run --rm --no-deps -v $PWD/backend:/workspace api ...`).
  Gotcha: `diff_against_recorded` only compares keys PRESENT in `recorded` — parity fixtures need
  the key-completeness guard (see `test_parity_baseline_fixtures_committed`).
- **Scripted real-voice playground runs (fake-mic method, Johnny-cxu)**: synthesize bot-addressed
  utterances with the in-image piper CLI (TTS speech DOES trip Silero VAD + Parakeet, unlike DSP
  synthetics), assemble one 48 kHz WAV (5 s head, 10 s gaps), relaunch the shared Chrome with
  `CHROME_EXTRA_FLAGS="--use-fake-device-for-media-stream --use-fake-ui-for-media-stream
  --use-file-for-fake-audio-capture=<wav> --disable-features=AudioServiceSandbox"
  ./scripts/start-chrome.sh` — **AudioServiceSandbox off is mandatory on macOS** or the fake mic
  reads no file and stays silent. WAV restarts per getUserMedia and loops at EOF. Then read
  `session_timings` per turn (stage start = `started_at_ms - duration_ms`; LiveKit stamps metric
  `timestamp` at stage END — verified in 1.5.17 source). Two instrument gotchas: STT rows attach
  to the PREVIOUS turn_id (no speech_id → `turn_index.last()` fires pre-registration, Johnny-5vb)
  — pair STT to replies by timestamp; and answer_llm ttft==total because openai_compatible has no
  real `stream_chat` (base-class buffers full chat(), Johnny-dny). Method + analyzer preserved in
  `.validation/Johnny-cxu/` and docs/LATENCY.md §"Scripted 20+-turn capture".

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

## 2026-06-10 - Johnny-trt.3
- [BUILD] Phase-3 verdict-parity baseline: two hand-authored split replay fixtures pinning the
  CURRENT engine's speak/no-speak verdicts for delegation-shaped speech, committed BEFORE the
  Phase-3 router-schema extension (Johnny-trt.16) so the drift gate exists first.
  - `delegation-calendar` (7 turns): delegation asks addressed to the bot ("can you check our
    calendar for upcoming meetings?", inbox check), status queries ("Johnny, are you still
    working on that?", "any update…"), small-talk pivots; turn 7's router payload omits the
    optional fields entirely (pins `_parse_router_response` defaults).
  - `delegation-smalltalk` (8 turns): the NEGATIVE half — delegation/status-SHAPED utterances
    addressed to humans ("Bob, can you check the calendar…", "are you guys still working on…")
    that the router declines, plus a retracted ask approved at confidence 0.55 that the gate's
    0.7 threshold suppresses (`no_reply/low_confidence`).
  - Drift guard in `tests/smoketest/test_replay_harness_agent.py`: zero-diff
    `diff_against_recorded` per fixture, suppression-reason pins (`router_declined` vs
    `low_confidence` stay distinct), recorded-block key-completeness guard, fixture-presence
    guard; teeth proven by an in-memory perturbation probe (caught as expected).
  - Verified in the api container: `johnny-replay --all --mode invariants` 5/5 PASS exit 0;
    regression mode MATCH for both new fixtures; 18 passed / 4 skipped pytest; ruff+format+mypy
    clean. No UI surface (fixtures/tests/docs only) — browser validation N/A.
- Files changed:
  - `backend/tests/fixtures/sessions/delegation-calendar/fixture.json` (new)
  - `backend/tests/fixtures/sessions/delegation-smalltalk/fixture.json` (new)
  - `backend/tests/fixtures/sessions/README.md` (new — corpus table + Phase-3 baseline contract)
  - `backend/tests/smoketest/test_replay_harness_agent.py` (drift-guard section; split-fixture
    floor 2→4)
  - `docs/REPLAY_HARNESS.md` (fixtures table now 5 rows + parity-baseline note; CI wiring names
    both test files)
- **Learnings:**
  - Replay-fixture authoring loop: hand-author `router`/`answer`, predict `recorded` from gate
    semantics, then VERIFY with `johnny-replay --session-id <id> --mode regression` — MATCH means
    the baseline is exactly what the engine does (it prints replayed values on mismatch, so it
    doubles as the capture step).
  - Gate outcome semantics for recorded blocks (`router_gate.py` + `observability.terminal_outcome`):
    `replied`→`spoken`; every declined/low-confidence path →`no_reply`/`suppressed`; a
    `should_speak=true` verdict below `confidence_threshold` records `should_speak: true` with a
    `no_reply` terminal (decision event fires before the threshold check). Rate limit is OFF by
    default (`DEFAULT_RATE_LIMIT_MAX_UTTERANCES=0`) so replays can't trip it.
  - `diff_against_recorded` compares ONLY keys present in the `recorded` block — a typo'd key
    silently exempts that field. Parity fixtures need a key-completeness guard (now in the test).
  - Split fixtures run on the agent engine (`replay_agent.run_agent_replay`; legacy split replay
    retired with Johnny-n22); `tests/` is excluded from the prod image, so in-container replay
    MUST use the PIPELINE.md §8.3 one-off: `docker compose run --rm --no-deps -v
    $PWD/backend:/workspace api sh -c "cd /workspace && johnny-replay --all --mode invariants"`.
---

## 2026-06-10 - Johnny-cxu
- Real 20+-turn p50/p95 latency baseline on the configured local stack (Parakeet MLX sidecar +
  Ollama llama3.2:3b + Local Piper), captured through the real /playground in the real browser
  via Chrome fake-mic WAV injection. Two runs with the identical 24-utterance sequence:
  session 72 (Piper `subprocess`, 28 turns) and session 73 (`persistent-subprocess`, 29 turns).
  - Headline numbers (p50/p95 ms): STT 116-123/251-350 · router+gate gap 2420-3385/4458-4726 ·
    answer-LLM ttft==total 3002-3068/4090-4099 · TTS first byte 855/914 (subprocess) vs
    **60/106 (persistent)** · e2e speech-end→first-audio ≈ 6.8 s p50 (target: 0.3 s).
  - Bottleneck attributed with code-level evidence: the two LLM calls own ~95% of felt latency;
    answer ttft==total because `openai_compatible_llm` never overrides `stream_chat` (base-class
    fallback buffers full chat()) → filed **Johnny-dny (P1)**. Router gap grows 1.2→4.8 s within
    one session (context accumulation) — felt latency tripled turn 2→33.
  - Before/after persistent piper: −795 ms TTS first byte per turn (−93%); matched early turns
    −930 ms e2e median; one-time ~550 ms voice load on first synth.
  - Instrument bug found + filed: STT timing rows attach to the previous turn_id (**Johnny-5vb**);
    analyzer re-pairs by timestamp. Derived e2e cross-checked against wall-clock api logs (±30 ms).
  - Second-order finding: ten consecutive two-sentence turns in session 73 produced ZERO audio —
    router+LLM exceeded the inter-fragment pause so native barge-in cancelled every reply
    pre-TTS (suppressed/barge_in, INV-1 terminals clean). The Johnny-trt premise observed live.
- Files changed:
  - `docs/LATENCY.md` (measured-baseline section replacing the "measured informally" note;
    scripted fake-mic methodology; re-ranked optimization candidates; stage-map refresh)
  - `backend/app/providers/{parakeet_stt,openai_compatible_llm,piper_tts}.py` ("Measured on this
    machine (2026-06-10)" ProviderTip first in each tips tuple; piper runtime tip aligned to
    measured numbers; one pre-existing ruff UP037 fix in parakeet_stt)
  - `scripts/start-chrome.sh` (opt-in `CHROME_EXTRA_FLAGS` passthrough + fake-mic doc comment)
  - `.validation/Johnny-cxu/` (gen_fake_mic.py, analyze_cxu.py, raw rows CSVs, run analyses,
    6 chrome-devtools screenshots incl. the three provider-modal tips + activity panel)
- Verification: ruff check + mypy clean on the three adapters; 210 provider tests pass;
  `docker compose build api && up -d api` rebaked the tips and the three modals were
  browser-validated from the baked image (screenshots 03-05). Provider config and Chrome flags
  restored to pre-run state (ElevenLabs active TTS; piper inactive `persistent-subprocess`).
  NOTE: `ruff format --check` fails on these provider files ON HEAD (pre-existing tool-version
  drift, whole-file rewrites) — not introduced and deliberately not reformatted here.
- **Learnings:**
  - Chrome's `--use-file-for-fake-audio-capture` silently produces silence on macOS unless
    `--disable-features=AudioServiceSandbox` is set (sandboxed audio service can't read the WAV).
  - LiveKit 1.5.17 metrics stamp `timestamp` at stage END (recognize/LLM-stream/TTS-stream
    completion) with `duration` covering the stage — session-relative stage starts are exactly
    `started_at_ms - duration_ms`, and cross-stage gaps reconstruct router cost without any
    extra instrumentation.
  - Identical fixture sequences make A/B runs turn-for-turn comparable — but session-wide
    percentiles are NOT comparable when run lengths differ, because per-session context growth
    dominates (run B's router p50 was ~1 s worse purely from running ~2 WAV loops). Match
    early turns for honest deltas.
---
