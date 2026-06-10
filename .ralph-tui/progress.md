# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **pacat/parec default PulseAudio buffer attrs cost SECONDS — never spawn
  them without latency flags on a latency-sensitive path**: with no
  `--latency-msec`, a playback stream gets prebuf ≈ tlength ≈ 2 s AND the
  null sink runs at ~1.94 s sink latency (no ADJUST_LATENCY client), so
  audio is heard ~3.9 s after the write; sub-prebuf utterances (<~2 s)
  don't play until more audio arrives. `--latency-msec=50
  --process-time-msec=10` → ~57 ms. Two stream-shape dynamics: bursty
  writers self-heal during gaps (backlog drains), continuous real-time
  writers lock the start transient in as permanent standing latency
  (in-rate == out-rate forever) — bounding latency needs flags + a
  silence-gate on the writer. Measure with the harness pattern in
  `.validation/Johnny-trt.10/` (one-off `docker run` of
  johnny-meet-worker:latest; entrypoint provides the production sink
  topology; tone-onset detection via a parec monitor thread).
- **Run backend pytest / one-off scripts against the prod-shape stack without
  flipping to dev mode**: `docker compose run --rm -T --no-deps -v
  "$PWD/backend:/workspace" api <cmd>`. The prod image excludes `tests/` via
  `.dockerignore`; this mounts the host tree into a one-off container from the
  same image while the running stack stays untouched. The Dockerfile has no
  ENTRYPOINT (CMD only), so any command works directly. Also the way to run
  in-image generator scripts whose outputs must land on the host
  (`python - < script.py` writes into the bind mount).
- **Speech fixtures for the engine VAD must be single-utterance, and texts fed
  through piper must have internal sentence punctuation comma-ized**: piper
  inserts its sentence pause at `.?!`, and Silero reads that pause as a
  >0.55 s end-of-speech, splitting one pushed fixture into two engine turns.
  Quiet-run capping can NOT fix this (the model scores the pause as non-speech
  beyond the energy-quiet stretch). Gate every new fixture with
  `.validation/Johnny-trt.1/verify_latency_fixtures.py` (exactly one
  START/END pair through `johnny.agent.session.load_vad`).
- **The real router declines repeats** ("already answered"), not just
  non-bot-addressed text. Any local-providers run that cycles few utterance
  texts thins its replied-turn percentiles fast (observed: 3-fixture cycle →
  17/24 turns declined; 24 distinct texts → 19/24 replied). Latency/behavior
  fixtures that loop must be content-distinct per turn.
- **LLM-stage latency is a function of session shape, not a constant**: chat
  context accumulated turn-over-turn dominates router+answer cost on
  llama3.2:3b (harness in-run trend router 758→1763 ms, answer 220→902 ms in
  24 turns — same curve docs/LATENCY.md documents for the manual baseline).
  Compare latency runs only at matched turn counts.
- **Operator-visible INFO breadcrumbs need a module-local handler**: there is
  no app-wide logging config, so `logger.info` from `johnny.*` / most
  `app.*` modules is silently dropped in `docker logs api` (root logger
  defaults WARNING). Follow the piper_tts/parakeet_stt idiom — setLevel(INFO)
  + tagged StreamHandler + propagate=False, attached only when absent (see
  `johnny/agent/adapters/factory.py` for the latest copy). Without it a new
  timing/diagnostic line "works" in pytest but never reaches the operator.
- **Ruff gate is per-touched-file, not repo-wide**: HEAD carries ~32
  pre-existing `ruff check` errors and several never-formatted provider
  files. Verify cleanliness of a change by `git stash` → baseline run →
  `git stash pop` → diff the violation sets; whole-file reformats of
  pre-existing drift just bloat the diff.
- **Synthetic mic input for playground browser tests**: patch
  `navigator.mediaDevices.getUserMedia` (via evaluate_script before Start, or
  navigate_page initScript across reloads) to return an
  oscillator→gain→MediaStreamDestination stream — gain G gives exact
  rms=G/√2, peak=G for level-gate assertions, and Silero does NOT classify
  the sine tone as speech, so server-side VAD stays quiet while client-side
  level gates exercise (clean isolation of the two barge-in paths). Create
  the AudioContext lazily inside the patched gUM (post-click) so autoplay
  policy can't leave it suspended.
- **Frontend quality gates without flipping to dev mode**: the frontend
  analog of the compose-run pattern is `docker compose run --rm -T --no-deps
  -v "$PWD/frontend/src:/workspace/src" frontend pnpm test` (mount ONLY
  src/ + config files over the image's /workspace — mounting the whole
  frontend/ dir shadows the image's node_modules). Works for test /
  typecheck / lint; eslint has 1 pre-existing HEAD error
  (settings/+page.svelte PermissionName) — gate per-touched-file via
  git stash round-trip like the ruff gate.
- **The playground live-state badge can freeze on a stale value**: the
  controller's `liveState` getter depends on `Date.now()` grace windows,
  which Svelte reactivity never re-evaluates unless some $state changes;
  with a silent mic, `micLevel` stays identically 0 so `data-state` can
  show "speaking" minutes after audio stopped. Don't trust
  `[data-testid="live-state"]` as an audio-stopped assertion in browser
  tests — nudge reactivity (e.g. change mic level) or use a console
  breadcrumb instead.
- **Piper chunk_bytes is NOT a TTFB knob — measure before chasing
  buffer-size latency wins**: the piper CLI bursts essentially all PCM
  after spawn+ONNX-load (`piper.synth` total−ttfa ≈ 4 ms), so the
  subprocess runtime's `stdout.read(chunk_bytes)` never gates the first
  byte (measured 4096 vs 1024: Δp50 2.3 ms over 10 interleaved spawns);
  the persistent runtime never consults chunk_bytes on its first-byte
  path (`_synth_persistent` streams the lib's own chunks) and the
  http-sidecar slices an already-complete response. chunk_bytes only
  sets downstream frame granularity. Also: per-chunk `resample_pcm16`
  rounds each read independently, so total output length is the
  per-read rounded sum — tests asserting whole-buffer rounding break
  when the chunk size changes (see test_piper_satisfies_tts_contract).

---

## 2026-06-10 - Johnny-trt.1
- Verified + completed the scripted latency harness from the prior (cut-off)
  iteration: `johnny.agent.latency_harness` drives a real BrowserAgentSession
  through a fake BrowserAudioTransport (real-time-paced 16 kHz / 20 ms frames,
  real Silero VAD endpointing), derives per-stage timings from the
  Johnny-ckz.7 `PipelineTiming` events on an in-process bus, and prints
  cold-turn + warm p50/p95/min/max per stage (vad_end, stt, router, llm_ttft,
  llm_total, sentence_gap, tts_ttfb, first_audio_wall, e2e_vad_commit).
- Found the prior iteration's local-providers artifact truncated (no report)
  and its root cause: only 3 bundled fixtures cycling → the real router
  declined 17/24 turns as repeats. Regenerated the **full Johnny-cxu
  24-utterance set** as bundled fixtures (`latency_turn_{short,medium,long}{1..8}.pcm`,
  piper amy-medium, comma-ized internal punctuation, quiet-capped, edge-trimmed,
  16 kHz S16LE), VAD-verified all 24 in-image (1 START/END pair each), wired
  them as the harness default cycle with legacy short/medium/long aliases.
- Verification (all in the rebuilt prod-shape api image): pytest 7/7
  (`tests/agent/test_latency_harness.py` via compose-run bind mount), ruff
  lint+format clean, 20-turn stub run 20/20 replied (warm e2e_vad_commit
  401/414 ms p50/p95), 24-turn local run vs the configured baseline trio
  (Parakeet MLX + Ollama llama3.2:3b + Piper persistent): 24/24 terminals,
  19 replied, warm stt 119/156, router 910/1513, answer 562/821, tts_ttfb
  78/185, felt e2e 2356/3186 ms. Sanity-gate analysis written into
  docs/LATENCY.md: hardware stages match baseline (STT +3 % p50); LLM stages
  sit at the small-context left edge of the baseline's own context-growth
  curve (baseline percentiles span ~29 replied turns over ~2 WAV loops);
  matched-index turns leave ~40 % residual attributed to the baseline's live
  Chrome/recorder environment. Harness is for phase-over-phase deltas at
  matched turn counts.
- Files changed: backend/johnny/agent/latency_harness.py (new),
  backend/tests/agent/test_latency_harness.py (new),
  backend/johnny/agent/fixtures/latency_turn_*.pcm (24 new, 3 legacy removed),
  backend/johnny/agent/fixtures/README.md, backend/johnny/agent/adapters/johnny_stt.py
  (batch_only attr opt-in for the stub STT), docs/LATENCY.md.
  Artifacts: .validation/Johnny-trt.1/ (01–06 + generator/verifier scripts).
- **Learnings:**
  - Patterns discovered: see Codebase Patterns above (compose-run bind-mount
    pattern; single-VAD-utterance fixture contract; router repeat-declines;
    context-size-dependent LLM latency).
  - Gotchas encountered: the api prod image had been rebuilt by the prior
    iteration, so in-container files already matched the working tree
    (md5-verified before trusting any artifact); `provider_credentials` (not
    provider_configs) is the providers table; the cold turn cleanly isolates
    the two prewarm targets (Ollama cold prompt cache → router 2253 ms, piper
    voice spawn → tts_ttfb 559 ms) — exactly the Phase-1 prewarm split.
---

## 2026-06-10 - Johnny-trt.4
- Phase-0 capstone: pure verification pass, no code/doc changes needed — the
  siblings had already folded everything in. Verified all three acceptance
  criteria: (1) docs/LATENCY.md carries the Johnny-cxu 28+-turn p50/p95
  baseline table + bottleneck attribution (two LLM calls own ~95 % of felt
  latency; zero "measured informally" text remains) plus the trt.1 harness
  section with its sanity-gate numbers; (2) docs/PIPELINE.md §9 carries the
  livekit-agents 1.5.17 findings (say()/SpeechHandle done-callback semantics
  incl. barge-in, roomless user_state_changed with observed transition
  timeline + caveats); (3) all four siblings closed (Johnny-cxu, trt.1,
  trt.2, trt.3 — bd show verified). Parity-fixture status documented in
  docs/REPLAY_HARNESS.md + backend/tests/fixtures/sessions/README.md
  (delegation-calendar / delegation-smalltalk marked as the Phase-3
  verdict-parity baseline with the do-not-regenerate warning).
- Re-ran all Phase-0 gating suites in the prod-shape api image via the
  compose-run bind mount (one invocation, ~38 s): test_latency_harness.py +
  test_sdk_surface_smoke.py + test_replay_harness.py +
  test_replay_harness_agent.py → 28 passed, 4 skipped. Confirmed all
  doc-referenced artifacts exist (latency_harness.py, sdk_surface_smoke.py,
  24 latency_turn fixtures, sdk_smoke_speech.pcm) and doc-referenced beads
  (Johnny-trt.14, Johnny-dny, Johnny-5vb) are open as the docs claim.
- Files changed: none (verification-only capstone; bead closed with full
  verification trace in the close reason).
- Phase 1 is now unblocked: Johnny-trt.5, trt.7, trt.8, trt.9, trt.10 all
  show in bd ready.
- **Learnings:**
  - The 4 pytest skips in test_replay_harness.py are intentional, not silent
    gaps: split fixtures (14/, 3/, delegation-*) skip there because
    test_replay_harness_agent.py replays them on the agent engine — check
    skips with `-rs` before treating a replay run as green.
  - All four Phase-0 suites run in a single compose-run container in ~38 s —
    cheap enough to be the standard pre-capstone gate for every later phase
    (the epic requires re-measurement + green gates at each capstone).
---

## 2026-06-10 - Johnny-trt.8
- Provider prewarm at session start. Added `async warm_up()` to
  `_ProviderBase` (default no-op, contract: idempotent, raise-on-failure for
  the caller to log). Implemented: faster-whisper (`_ensure_model` weight
  load), Piper persistent runtime (tiny "Ok." synth pays the ~650 ms voice
  ONNX load into the process cache; subprocess/http-sidecar runtimes stay
  no-ops — cold start is structural / sidecar warms itself), openai-compatible
  LLM (raw 1-token `max_tokens: 1` ping → Ollama GGUF load; hosted `OpenAILLM`
  overrides back to no-op: nothing to load + reasoning models reject
  `max_tokens`). `SessionAdapters` now carries the raw providers
  (`stt_provider`/`llm_provider`/`tts_provider`, default None);
  `warm_up_session_providers` (factory.py) gathers hooks concurrently with
  per-provider timing logs, never raises; `AgentRuntime.warm_up` +
  `BrowserAgentSession.warm_up` delegate; the browser runner fires it as a
  fire-and-forget task (module-level strong-ref set) right after
  `BrowserAgentSession.build` — the ready signal never waits (pinned by a
  held-open-warm-up test). Harness: `--prewarm` awaits warm-up before turn 1;
  `prewarm` in report header + JSON.
- Measured (6-turn `--providers local` runs, Ollama force-unloaded before
  each, artifacts `.validation/Johnny-trt.8/`): cold-turn e2e_vad_commit
  3903 ms → 1007 ms (router 2953→606, tts_ttfb 576→57); prewarmed turn 1 is
  the fastest turn (warm p50 1546) — acceptance "within 100 ms of steady
  state" met on the favorable side. Warm-up wall cost 1453 ms, all concurrent
  (Ollama 1452 ∥ piper 652 ∥ parakeet no-op 0). Browser-validated on the
  rebuilt prod image (session 75): three warm_up log lines at start, first
  typed turn replied with warm piper ttfa 273 ms, console clean, screenshots.
- Files changed: backend/app/providers/{base,faster_whisper_stt,piper_tts,
  openai_compatible_llm,openai_llm}.py, backend/app/services/
  browser_pipeline_runner.py, backend/johnny/agent/adapters/{factory,__init__}.py,
  backend/johnny/agent/{job_session,browser_session,latency_harness}.py,
  docs/LATENCY.md (prewarm section + measured table + OLLAMA_KEEP_ALIVE),
  .env.example (host-side OLLAMA_KEEP_ALIVE note), tests: providers
  {base,faster_whisper,piper,openai_compatible,openai_llm}, agent
  {adapter_factory,browser_session,latency_harness}, services
  {browser_pipeline_runner}.
- **Learnings:**
  - INFO logs from `johnny.*` / most `app.*` loggers are dropped in
    `docker logs api` (no app-wide logging config; root defaults WARNING).
    The established idiom for operator-visible breadcrumbs is the
    piper_tts/parakeet_stt module-local StreamHandler attach — new
    operator-facing timing lines need it or they silently vanish.
  - `OpenAILLM` subclasses `OpenAICompatibleLLM`: any behavior added to the
    compatible adapter leaks to hosted OpenAI unless overridden (the warm-up
    ping would burn quota and trip reasoning models' `max_completion_tokens`
    requirement).
  - Ollama keep_alive default (5 m) makes "cold" measurement runs
    reproducible via `curl /api/generate -d '{"model":X,"keep_alive":0}'`
    to force-unload between runs; `/api/ps` confirms residency.
  - The repo is not globally ruff-clean (32 pre-existing errors on HEAD);
    the realistic gate is lint/format-clean on touched files, with
    `git stash` round-trip to attribute drift to HEAD vs the change.
---

## 2026-06-10 - Johnny-trt.7
- Piper `DEFAULT_CHUNK_BYTES` 4096 → 1024: constant (+ constraint comment),
  class docstring, schema default (flows from the constant at FieldDef
  `default=DEFAULT_CHUNK_BYTES`), and the providers-modal tip rewritten as
  "chunk_bytes sets frame granularity" — documents the new default, the 4x
  syscall trade-off, the measured-no-TTFB-effect finding, and that rows
  saved with an explicit 4096 keep their stored value.
- Measured the candidate before believing it: subprocess-runtime TTFB A/B
  (10 interleaved spawns, real voice, in-image) gave Δp50 = **2.3 ms**, not
  the predicted ~70 ms — piper CLI delivers all PCM in one burst after
  spawn+load (total−ttfa ≈ 4 ms), so read size never gates first byte. An
  8-turn `--providers local` harness A/B (DB row temporarily flipped to
  runtime=subprocess 4096 vs 1024, then restored byte-identically from
  snapshot) agreed: tts_ttfb p50 807 vs 822 ms (spawn jitter). Persistent
  runtime (the configured stack) never consults chunk_bytes on its
  first-byte path. docs/LATENCY.md updated in both spots (warm-first-byte
  attribution + candidate #4 marked shipped-and-falsified with numbers).
  What the change does ship: first-frame granularity 2972 → 744 B at
  16 kHz (~93 → ~23 ms of audio per frame) for downstream pacing.
- Tests: pinned default==1024, schema default==1024, explicit 4096 honored;
  fixed test_piper_satisfies_tts_contract to the per-read resample-rounding
  expectation (old exact equality was a 4096-chunk rounding coincidence).
  99 piper + 123 providers-API tests green in-image; ruff check clean,
  format drift on both files is pre-existing HEAD drift (stash-verified).
- Browser-validated on the rebuilt prod api image (chrome-devtools):
  existing Local Piper modal shows stored 4096 honored + the new tip;
  Add provider → TTS → Local Piper shows default 1024; console clean.
  Artifacts: .validation/Johnny-trt.7/ (00 row backup, 01 micro-bench
  results, 02/03 harness JSONs, 04–06 screenshots, ttfb_microbench.py).
- Files changed: backend/app/providers/piper_tts.py,
  backend/tests/providers/test_piper_tts.py, docs/LATENCY.md.
- **Learnings:**
  - Patterns discovered: piper chunk_bytes is not a TTFB knob (see new
    Codebase Patterns bullet — CLI bursts PCM; persistent path ignores it;
    per-chunk resample rounding makes exact-length tests chunk-dependent).
  - Gotchas: the acceptance's "~70 ms faster" came from LATENCY.md's
    candidate math, which the harness/micro-bench falsified — when a bead
    bakes in a predicted number, measure first and update the docs to kill
    the premise rather than forcing the number; the active piper DB row
    stores an explicit chunk_bytes=4096, so the configured stack's behavior
    is unchanged by design (explicit values win over the new default).
---

## 2026-06-10 - Johnny-trt.9
- Client-side auto barge-in in the playground. `browserAudio.ts` grew a pure
  consecutive-frame speech gate (`createBargeInGate`: RMS >= 0.02 AND
  peak >= 0.08 for 2+ consecutive 20 ms frames, any miss resets;
  `pcm16FrameLevels` computes normalized rms/peak per S16LE frame; both
  exported for vitest). The capture-worklet onmessage runs the gate on every
  frame while `autoBargeIn && speaking`; a fire logs a `[barge-in]`
  console.info breadcrumb and calls `requestInterrupt()` — synchronous local
  cut + `{"type":"stop"}` to the server, vs ~300-500 ms for the server-side
  path alone. Gate resets on speaking transitions, mic-mute toggles, and
  enable/disable so no stale frame-run carries over. New
  `autoBargeIn` option + `setAutoBargeIn`/`getAutoBargeIn` on the handle.
  Controller: `autoBargeIn` $state seeded from localStorage in the FIELD
  INITIALIZER (not loadMetadata — reattach can wire audio before metadata
  loads), `toggleAutoBargeIn()` persists + propagates live. LiveSession:
  "Voice barge-in" checkbox in the voice-controls grid, default on,
  data-testid=toggle-auto-barge-in.
- Verified: vitest 95/95 (17 new gate/levels tests incl. a 3000-frame
  sub-threshold soak), svelte-check 0/0, eslint clean on touched files
  (stash-verified). Live chrome-devtools run on the rebuilt prod image
  (session 81): fake-mic oscillator; fire at 58.5 ms / 63.4 ms from voice
  onset (measured rms 0.351/0.353 ≈ theoretical 0.354 for the 0.5-amp
  sine); server kill confirmed by absent agent_spoke/utterance row + Phase C
  proving Silero ignores the sine tone (so the kill came via the client
  stop); 60+ s open-mic bot speech incl. ~30 s rms-above/peak-below noise →
  zero self-interrupts; toggle off → zero client fires + full reply;
  persistence across reloads both ways. Artifacts: .validation/Johnny-trt.9/.
- Files changed: frontend/src/lib/browserAudio.ts,
  frontend/src/lib/browserAudio.test.ts (new),
  frontend/src/lib/playground/playgroundSession.svelte.ts,
  frontend/src/lib/components/playground/LiveSession.svelte.
- **Learnings:**
  - Patterns discovered: see Codebase Patterns above (synthetic-mic gUM
    patch with exact-level oscillator + Silero-blind sine isolation;
    frontend compose-run gate with src-only mount; stale live-state badge).
  - Gotchas: `app.api.browser_sessions`' "client stop control received"
    INFO never reaches docker logs (the known module-local-handler gap), so
    server-side stop evidence must come from behavior (no agent_spoke / no
    utterance row / no further synth) — or add the handler idiom if a later
    bead needs the log line; `liveState` shows 'speaking' ~1.5 s past the
    cut by design (lastSpokenAt grace), so the <=60 ms acceptance is
    asserted via the console-breadcrumb timestamp + synchronous-cut code
    path, not the badge.
---

## 2026-06-10 - Johnny-trt.10
- Meet audio-bridge buffering audit (docs/LATENCY.md candidate #6, TTS
  direction). Code audit: every Python hop forwards frame-at-a-time —
  LiveKitTransport downlink queue (drop-oldest, livekit_transport.py:300),
  MeetRoomBridge._pump_room_to_meet (:588), MeetAudioBridge._write_frame
  (per-frame write+flush, Popen bufsize=0). No batching in the bridge code.
- Empirical audit in johnny-meet-worker:latest driving the REAL
  MeetAudioBridge: a real buffer exists in the pacat/PulseAudio stage
  (`_spawn_playback_process` passes no --latency flags). Bursty shape:
  onset 3943.8/3947.3 ms (start / after 3 s gap — prebuf re-arms), 300 ms
  utterance never plays within 8 s, tail drain ~2.87 s. Continuous shape:
  3.7-3.8 s standing on every utterance. Fix variant
  (--latency-msec=50 --process-time-msec=10): 56.8 ms onsets bursty, sink
  latency 1.94 s → 11.6 ms, continuous still locks in 0.5-1.0 s start
  transient → fix = flags + downlink silence-gating. Capture-direction
  poking: 0.95-1.5 s under synthetic pacat writers, parec flags changed
  nothing, raw monitor ≈ remap source — NOT production-representative
  (real writer is Chromium, a low-latency PA client); re-measure against
  the browser only if Meet turn latency stays high after the fix.
- Filed fix bead Johnny-dkj (P1, depends on Johnny-trt.10): latency flags
  + silence-gate + harness re-verify + real-Meet before/after. Updated
  docs/LATENCY.md candidate #6 with the measured verdict.
- Files changed: docs/LATENCY.md (candidate #6 audit result). Audit
  harness + raw results under gitignored .validation/Johnny-trt.10/
  (00-audit-summary.md, 5 measurement scripts, 01-06 transcripts,
  results*.json). No runtime code changed; no UI surface → browser
  validation not applicable (stated per CLAUDE.md exemption).
- **Learnings:**
  - Patterns discovered: pacat/parec PA default buffering + the
    continuous-stream standing-latency trap (new Codebase Patterns
    bullet); one-off `docker run` of the meet-worker image gives the
    full production PulseAudio topology for audio measurements.
  - Gotchas: pacat writes never block (PA maxlength ≈ 4 MB), so the
    bridge's asyncio queue stays empty and the multi-second backlog is
    invisible to Python-side instrumentation — you MUST measure on the
    sink monitor; ADJUST_LATENCY convergence makes the continuous-shape
    standing latency vary run-to-run (0.5-1.5 s) with sink state, so
    A/B only steady-state numbers; LATENCY.md candidate numbering
    drifted (bead says #5, doc says #6).
---
