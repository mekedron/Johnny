# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Scripted fake-mic playground runs interact with trt.9 client auto
  barge-in**: launch the shared Chrome with `CHROME_EXTRA_FLAGS="--use-fake-device-for-media-stream
  --use-fake-ui-for-media-stream --use-file-for-fake-audio-capture=<wav>
  --disable-features=AudioServiceSandbox"` (pkill + re-run
  `./scripts/start-chrome.sh`; restore by pkill + re-run with env unset).
  The WAV loops per getUserMedia stream — mute the mic before the loop
  point or loop rows pollute the run. With replies enabled, any utterance
  starting while the bot still speaks fires client barge-in and the turn
  terminalizes `no_reply(barge_in)` (outcome `suppressed`) — judge VAD/turn
  integrity by `transcript_chunks` rows, not by decision outcomes, or use
  gaps longer than the longest reply.
- **Gate/ledger terminal evidence lives in `agent_decisions`
  (`terminal_state`, `no_reply_reason` columns)** — docker logs swallow the
  gate's INFO breadcrumbs (the known module-local-handler gap), so query
  the table for per-turn verdict/terminal forensics.
- **Turn-commit timing mechanics (livekit-agents 1.5.17)**: the commit is
  `max(VAD min_silence, endpointing min_delay anchored at last-speech,
  STT-final)` — the EOU/endpointing bounce task only STARTS once a final
  transcript exists and VAD END_OF_SPEECH fired, so (a) a semantic EOU
  model can never commit BELOW the Silero floor, and (b) endpointing
  `min_delay` cuts felt latency only when STT finals land faster than
  `min_delay - vad_floor` after VAD fire (batch Parakeet ~123 ms finals →
  STT-bound; stub 80 ms → ~37 ms win measured). `max_delay` is inert with
  `turn_detection="vad"` (only a turn-detector verdict escalates to it).
- **In-process EOU model RSS (trt.6 spike)**: multilingual ONNX = 396 MB
  disk / +884 MB RSS in-process (wontfixed, > ~500 MB line; no Finnish
  support either); en-only = 66 MB disk / +406 MB RSS / 1.4 ms inference
  (viable, filed Johnny-1qr). `EOUModelBase.__init__` already accepts
  `inference_executor=`; runner IO is JSON bytes in/out. Numbers:
  `.validation/Johnny-trt.6/00-spike-note.md`.
- **Local-provider harness runs have a ±100–150 ms p50 LLM-noise floor**:
  router/llm p50 drifts that much between *identical-config* 24-turn runs
  (Ollama run noise + replied-count-driven context growth — runs that
  reply more carry more chat history at every later turn), so felt-e2e
  phase deltas below that floor are unresolvable on the local stack. Use
  the stub harness for controlled felt deltas (its run-to-run noise is
  ~4 ms — cross-day cells agreed 834.8 vs 838.9) plus per-stage
  deterministic metrics (e.g. vad_end) on local runs; counterbalance run
  order (ABBA) and `ollama stop` before each run when comparing local
  arms. Worked example: `.validation/Johnny-trt.11/00-capstone-notes.md`.

---

## 2026-06-10 - Johnny-trt.5
- Endpointing knobs. Verification + completion pass: the implementation was
  already on HEAD (committed under the trt.9 commit b6e33f65 by a prior
  cut-off iteration) — `load_vad(min_silence_duration=...)` passthrough,
  `build_agent_session(endpointing=...)` → factored `build_turn_handling`
  (omits the key when None so LiveKit's 0.5/3.0 defaults stand; room path
  passes nothing), browser-scoped `load_browser_vad()` with
  `BROWSER_VAD_MIN_SILENCE_DURATION_S = 0.40` riding `_shared_vad()`, and
  harness `--vad-min-silence-s` for A/B. Worker/Meet path verified
  untouched (worker.py prewarm calls bare `load_vad()`, no endpointing).
- Verified this iteration: 23/23 unit tests in-image
  (test_session_endpointing.py + browser-session VAD pins + harness label
  test); ruff lint+format clean on all 6 touched files; prior iteration's
  A/B artifacts check out (warm vad_end p50 562.4 → 401.3 ms = **161 ms
  earlier**, within the ~150±50 ms acceptance; 24/24 turns replied both
  floors; 20-turn varied-pause script all single-utterance at 0.40 s).
- Completed the missing test-plan item: chrome-devtools playground run
  (session 82, prod stack, Parakeet+Ollama+Piper) with the prepared
  fake_mic_pauses.wav (8 utterances, mid-sentence hesitations 0.20–0.35 s,
  several at the 0.35 s edge): 8 first-pass utterances → exactly 8 user
  transcript rows, every hesitation rode out, zero premature commits
  (loop replay row #9 also single). All 9 turns should_speak=true and
  replied; the 8 first-pass replies were cut by the NEXT utterance via
  trt.9 auto barge-in (no_reply_reason=barge_in — by design); final turn
  spoken to completion after mic mute. Console clean. Screenshot +
  DB-row dump under .validation/Johnny-trt.5/ (05-*.png, 06-*.txt).
- Files changed this iteration: docs/LATENCY.md (measured A/B numbers added
  to the endpointing-knobs paragraph), .ralph-tui/progress.md.
- **Learnings:**
  - Patterns discovered: fake-mic WAV loops per gUM stream + auto barge-in
    cuts long replies on scripted runs (see Codebase Patterns);
    agent_decisions.terminal_state/no_reply_reason are the queryable gate
    forensics (docker logs swallow gate INFO lines).
  - Gotchas: STT may hyphenate words ("stand-ups") — don't key automation
    exit conditions on exact substrings of expected transcripts; the
    engine had folded this bead's code into the trt.9 commit, so always
    check `git log -S` for the symbols before re-implementing a bead.
---

## 2026-06-11 - Johnny-trt.6
- Semantic turn detector on the browser session: spike ran FIRST per the
  bead and hit its own abort line — in-process `_EUORunnerMultilingual`
  costs +884 MB RSS in the api container (ONNX 396 MB on disk; arena-off
  still ~1.05 GB total; warm inference 12 ms so CPU was never the issue).
  Closed the semantic path as wontfix-with-findings and shipped the
  codified fallback: **retuned VAD-only endpointing**. Bonus findings:
  the multilingual revision (v0.4.1-intl) has NO Finnish among its 14
  languages (per-turn `supports_language` would skip it for fi configs),
  and the en-only model fits the line (+406 MB / 1.4 ms) — filed as
  opt-in proposal Johnny-1qr under the epic.
- Retune shipped: `BROWSER_ENDPOINTING_MIN_DELAY_S = 0.40` +
  `browser_endpointing()` in browser_session.py, forwarded by
  `BrowserAgentSession.build` (new `endpointing=` override kwarg, browser
  default when None); harness grew `--endpointing-min-delay-s` /
  `endpointing_min_delay_s` + `endpointing_label` in result/report/JSON.
  Meet/room path untouched (still passes no endpointing; pins keep 0.5/3.0
  SDK defaults).
- Verified: 704/704 in-image tests (4 new: endpointing pin equal to VAD
  floor, build default + explicit-override forwarding, harness label);
  ruff lint+format clean. Harness A/B 24-turn stub (warm p50):
  first_audio_wall 838.9 → 801 ms, router 102 → 66 ms (~37 ms felt; the
  remaining headroom is STT-final-bound — local Parakeet ~123 ms finals
  make the retune felt-neutral today, the win lands with Phase-2
  streaming STT). Live chrome-devtools playground run (session 83, dev
  stack, Parakeet+Ollama+Piper, trt.5 fake-mic WAV with 0.20–0.35 s
  hesitations): 8 utterances → exactly 8 user transcript rows, zero
  premature commits/splits; all 8 decisions should_speak=true,
  terminalized no_reply(barge_in) by design (next utterance cut each
  reply — judged by transcript_chunks per the pattern note). Console
  clean. Artifacts under .validation/Johnny-trt.6/ (00-spike-note.md,
  01/02 A/B reports + JSONs, 03 screenshot, 04 DB rows).
- Files changed: backend/johnny/agent/browser_session.py,
  backend/johnny/agent/latency_harness.py,
  backend/tests/agent/test_browser_session.py,
  backend/tests/agent/test_latency_harness.py, docs/LATENCY.md,
  .ralph-tui/progress.md.
- **Learnings:**
  - Patterns discovered: turn-commit = max(VAD floor, min_delay,
    STT-final) and the EOU-can't-beat-the-VAD-floor consequence (added to
    Codebase Patterns); EOU model RSS table (added to Codebase Patterns).
  - Gotchas: `--use-file-for-fake-audio-capture` keeps feeding the looped
    WAV into the still-open gUM stream after the last utterance — mute
    must land before head-silence + first-utterance replay or the replay
    barge-ins the final reply (judge by transcript rows, not reply
    completion); `ruff format` collapses multi-line assignments — run it
    before claiming format-clean; the harness label tests pin exact
    f-string output ("min_delay=0.4s" via %g), keep new labels %g-formatted.
---

## 2026-06-11 - Johnny-trt.11
- Phase-1 capstone gate: re-measured the harness with all Phase-1 changes
  active and ran the varied-pause + barge-in regressions; LATENCY.md got a
  "Phase-1 capstone" section with the phase-over-phase deltas. Headline:
  controlled stub felt p50 958.6 → 801.0 ms (**−157.6 ms, −16 %**, meets
  the ≥100 ms bar); local stack turn-commit (vad_end) p50 563 → 404 ms
  (**−159.4 ms deterministic**, pooled n=31/36 over four
  ABBA-counterbalanced 24-turn runs); cold turn 4413/3619 → 1661/1725 ms
  (**−2.3 s**, prewarm — first turn is now the fastest). Warm felt total on
  local providers is statistically flat (+58 ms) because router+llm run
  noise (±100–150 ms p50 on identical code) exceeds the knob effect —
  documented attribution in LATENCY.md (Phase-2/3 territory).
- Varied-pause regression: 20/20 hesitation fixtures + 24/24 bundled
  fixtures = exactly one Silero utterance each at the 0.40 s floor (zero
  premature turn-cuts). Client barge-in false-positive check (live session
  84, oscillator fake mic): 67.6 s of bot speech with sub-threshold noise
  (rms 0.028 above the rms threshold, peak below) → zero gate fires, both
  long replies completed; positive control fired +34 ms (decision 400
  suppressed/barge_in, INV-1 clean); prewarm verified live (0/612/660 ms
  concurrent); console clean. All Phase-1 siblings were already closed
  (trt.10 with a real-buffer finding → Johnny-dkj, allowed by the AC).
- Files changed: docs/LATENCY.md, .ralph-tui/progress.md (docs-only — no
  code, so no test/lint gates apply). Artifacts:
  .validation/Johnny-trt.11/ (4 harness runs + JSONs, fixture
  verifications, screenshots, decision dump, compare_runs.py,
  fake_mic_oscillator.js, 00-capstone-notes.md).
- **Learnings:**
  - Patterns discovered: the local-harness LLM-noise floor + ABBA
    counterbalancing + stub-harness-for-felt-deltas (added to Codebase
    Patterns above).
  - Gotchas: pooled warm percentiles across runs are confounded by
    replied-turn-set composition (router declines differ run to run), and
    matched-fixture comparisons are still biased by context-growth when
    reply counts differ — pair them with a deterministic per-stage metric
    before drawing conclusions; `bd list --label phase-1` only shows OPEN
    issues (closed siblings vanish — verify via dependencies/`bd show`);
    the playground "counting" prompt yields only ~30 s of speech per reply
    with the concise persona — accumulate speaking time across replies for
    a 60 s false-positive soak instead of fighting the persona.
---
