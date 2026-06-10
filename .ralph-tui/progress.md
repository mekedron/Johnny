# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

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
