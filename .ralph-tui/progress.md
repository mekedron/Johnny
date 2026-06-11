# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Unit-testing the rune-based PlaygroundController without the svelte
  compiler (trt.40)**: the vitest config deliberately skips the svelte
  plugins, so `.svelte.ts` rune modules normally can't be imported — but
  `$state(v)` initializers only run at `new`, so installing
  `(globalThis as { $state?: unknown }).$state = (v) => v` before
  instantiation makes class fields plain non-reactive properties, which is
  enough for lifecycle/state assertions (see
  `playgroundController.test.ts`; vi.mock `svelte` (tick), sessionEvents,
  browserSessions, browserAudio, sessionDetail; node ≥21 has a global
  `navigator` but no `mediaDevices`, so `supportsMic()` is false and audio
  wiring self-skips). Gotcha: node:assert/strict `deepEqual(x, [])` has an
  `asserts actual is T` signature that narrows `x` to `never[]` for the
  REST of the test — use `.length === 0` instead.
- **Playground per-session UI scoping (trt.40)**: every WS-subscription
  callback (onEvent/onOpen/onClose/onError) is pinned to the sessionId it
  subscribed for and checked against `liveSession.id` — a deliberately
  closed socket still fires onClose/onError asynchronously, and a late
  frame from an ended session must not repopulate, tear down, or flip the
  connection banner of the next session. Per-session UI state
  (transcript incl. both live captions, lastDecisionAt/lastSpokenAt,
  isSpeaking, micLevel) resets ONLY in start()/reattach() via
  `resetPerSessionUi()` — endSession/teardownLive intentionally keep final
  lines visible for post-session review, and user controls (volume, mutes,
  barge-in, composer draft) survive across sessions.
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
- **Streaming-STT turn semantics (trt.12)**: with the mlx-sidecar Parakeet
  streaming path, the user-final exists BEFORE Silero END_OF_SPEECH
  (sidecar endpoints at 0.36 s < the 0.40/0.50 s session floors), so
  turn-commit is VAD-bound and pauses in [0.40, ~0.52) s now COMMIT turns
  that batch STT's ~120 ms final tail used to re-glue accidentally.
  Multiple STT-segment finals can accumulate inside one turn; judge turn
  integrity by `agent_decisions` (INV-1), not by transcript_chunks row
  counts (a row per final, not per turn). `JOHNNY_PARAKEET_FORCE_BATCH=1`
  pins the old batch path (per-runtime `batch_only` property otherwise).
- **parakeet-mlx streaming quality levers**: `depth=2` is what buys
  batch-parity WER across chunk boundaries (d1 ≈ 0.09–0.14 WER); FEWER
  decode boundaries beat more (480 ms chunks clean, 400 ms produced word
  slips); a fresh streaming context fed pure silence HALLUCINATES ('Yeah.'
  from 500 ms of zeros) — never decode leading silence, drop trailing
  silence past a pre-flush point. `StreamingParakeet.__enter__/__exit__`
  flips the encoder attention mode process-wide → hold a model-mode lock
  for the segment lifetime vs any batch transcribe on the same model.
- **zsh + docker compose exec env vars**: `ENV="-e FOO=1"; docker compose
  exec $ENV ...` silently sets a var named " FOO" (zsh doesn't word-split
  unquoted vars) — compose accepts it and your flag never lands. Spell
  `-e FOO=1` inline and verify with `os.environ.get` before trusting an
  A/B arm.
- **chrome-devtools MCP after a Chrome restart: pass `pageId` explicitly** —
  after pkill + `start-chrome.sh` re-launch, `evaluate_script` fails with
  "No page found" on the implicit selected page while list_pages/snapshot/
  click still work; `select_page` + an explicit `pageId` on every
  `evaluate_script` call fixes it.
- **Screenshotting a live caption (or any sub-second UI state)**: the
  agent-loop roundtrip between tool calls is ~10 s, far slower than the
  state's lifetime — do the wait AND the freeze inside ONE evaluate_script.
  For captions: in-page click the mic-mute toggle the moment the caption
  appears — mute stops mic frames entirely (browserAudio pump returns
  without sending, not silence), so the streaming endpointer never sees the
  silence needed to finalize and the caption stays rendered until unmute.
  Judge correctness by an untampered DOM-sampling trace (state-change log at
  ~120 ms cadence); use the freeze only for the visual.
- **Interim live captions (trt.13)**: streaming-STT interims reach the UI via
  `session.on("user_input_transcribed")` → `TranscriptInterim`
  (`transcript_interim`, ephemeral — subscriber ignores it, no DB row) → ws
  wire name `transcript_partial`. Batch/StreamAdapter STT produces no
  interims, so the caption simply never appears there. Noise-gated interims
  never reach the SDK (stt_node drops them) so captions inherit the noise
  filter. Client must clear the caption on user final AND
  `transcript_filtered` (a noise-dropped turn emits no final) AND teardown,
  and treat an empty hypothesis as a clear.
- **Semantic-EOU mechanics on the browser session (1qr)**: the in-process
  en model only changes behavior at pauses ≥ the Silero floor — its
  "incomplete" verdict escalates the commit to endpointing `max_delay`
  (resumed speech cancels it; an utterance held this way accumulates
  multiple STT finals into ONE decision — that hold is itself proof the
  detector engaged, since `max_delay` is inert under
  `turn_detection="vad"`). The model leans hard on terminal punctuation
  (probe: punctuated complete sentences p 0.85–0.94, mid-sentence cuts
  p ≤ 0.005, complete-but-unpunctuated p 0.005 → HOLD) and the streaming
  sidecar HALLUCINATES terminal punctuation at its ~0.36 s segment edges
  ("Jenny, can you?") — that combination is why the 0.20 s floor drop was
  reverted (0.35 s-edge hesitations split); any future floor drop must fix
  the sidecar edge-punctuation artifact first. Engagement gate =
  STT-language-normalizes-to-en (same factory keys as the per-transcript
  stamp) + `JOHNNY_BROWSER_FORCE_VAD_TURNS` unset. The ~400 MB runner is a
  process singleton behind a shielded lazy load (a cold predict's 3 s
  timeout cancels mid-load without aborting or duplicating it).
- **fullPage screenshots mid-session reset playground control state**: the
  viewport resize remounts the controls component — an in-page-clicked mic
  mute was silently undone (~35 s later the fake-mic loop resumed feeding
  STT). Use viewport screenshots while session state matters, or end the
  session before capturing fullPage.
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
- **Live bot-reply captions (trt.39)**: `tts_node` emits an ephemeral
  `AgentSpeechInterim` per `iter_sentences` flush (wire
  `agent_speech_partial`, sequence-numbered per reply from 0; subscriber
  ignores it). Turn id resolves from `gate.active_reply` ONCE at
  sequence 0 and is cached for the reply's tail — `active_reply` is
  "most recently BOUND", not "now playing", so a rapid next-turn bind
  mid-reply would otherwise re-attribute tail sentences; ungated
  speeches (say()/approval) carry `turn_id=None`. UI contract: seq 0
  replaces any stale bubble; `agent_spoke` clears + appends the
  authoritative line; a NON-replied `turn_terminal` clears only a
  turn-matched (or unpinned) bubble. Wire ORDER for a replied turn is
  `turn_terminal` BEFORE `agent_spoke` (`_on_reply_done` emits the
  ledger terminal, then does take_reply file I/O, then publishes
  AgentSpoke) — so never clear the bubble on a `replied` terminal or it
  flickers. An interrupted reply emits NO AgentSpoke at all; its
  `no_reply(barge_in)` terminal is what removes ghost text. With an
  unmuted laptop mic + speakers, the bot's own audio echoes into STT
  and trt.9 client barge-in cuts replies ~1 s in — mute the mic for any
  scripted full-reply validation.

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

## 2026-06-11 - Johnny-trt.12
- Parakeet cache-aware streaming shipped on the MLX sidecar runtime (the
  spike picked it over in-container NeMo: parakeet-mlx 0.5.2 has a native
  StreamingParakeet API; the NeMo TDT checkpoint is offline-only and
  CPU-bound — documented as follow-up, coreml likewise). Sidecar grew
  `WS /transcribe_stream` (server-side RMS endpointer: 240 ms pre-roll,
  480 ms decode cadence, 200 ms pre-flush, 360 ms finalize + per-segment
  context reset, 30 s force-final; model-mode lock vs batch), provider
  grew `_transcribe_streaming_via_sidecar` (config→PCM→finalize over WS,
  interim+final TranscriptEvents, STREAMING_* constants as source of
  truth) + `batch_only` property + `JOHNNY_PARAKEET_FORCE_BATCH` env +
  internal `options["streaming"]` knob (stt_stream.py dictation pins it
  False — its replay loop would go quadratic; native streaming there is
  trt.13). "parakeet" removed from BATCH_ONLY_STT_PROVIDER_NAMES
  (per-runtime self-declaration via the existing batch_only attribute);
  drift-guard + classification tests updated. Harness grew
  `stt_final_after_vad_end_ms` (user_input_transcribed wall stamp vs VAD
  edge) for the Phase-2 acceptance metric.
- Verified: 3509 backend tests pass in-image (the 3 failures are
  pre-existing env issues: invalid OPENAI_API_KEY live tests + wizard
  image checks, reproduced on clean HEAD); 11 sidecar endpointer tests;
  ruff lint+format clean. Real-model: final excess after earliest trip
  p50 4 ms / max 10 ms; WER vs batch 0.035 (punctuation + clip-edge only,
  complete clauses word-identical); harness A/B (BABA, 12-turn runs)
  stt_final_after_vad_end p50 **+140 ms batch → −99 ms streaming** — every
  streaming final preceded the VAD commit, turn-commit now VAD-bound.
  chrome-devtools session 86 (fake-mic trt.5 WAV): full conversation on
  streaming Parakeet+Ollama+Piper, transcripts correct over two loop
  passes, 18 decisions INV-1 clean, console clean. Artifacts:
  .validation/Johnny-trt.12/ (00-decision-note.md, spike scripts, 3 WS
  validation runs, 5 harness JSONs, screenshot, DB dump).
- Files changed: sidecars/parakeet-mlx/server.py (+test_endpointer.py,
  README.md), backend/app/providers/parakeet_stt.py,
  backend/app/api/stt_stream.py,
  backend/johnny/agent/adapters/johnny_stt.py,
  backend/johnny/agent/latency_harness.py,
  backend/tests/providers/test_parakeet_stt.py,
  backend/tests/agent/test_stt_stream_adapter.py, docs/LATENCY.md,
  .ralph-tui/progress.md.
- **Learnings:**
  - Patterns discovered: streaming-STT turn semantics ([0.40, 0.52) s
    pauses now commit turns — batch slowness was an accidental re-glue),
    parakeet-mlx quality levers (depth=2, fewer boundaries, never decode
    leading silence), zsh compose-exec env-var trap — all added to
    Codebase Patterns above.
  - Gotchas: LiveKit emits NO stt PipelineTiming row for direct streaming
    STTs (usage-only metrics) — wall-clock `user_input_transcribed` is the
    measurable seam; an abandoned inner async generator only closes at GC
    (outer `transcribe_stream` must aclose() it explicitly or the WS
    lingers past barge-in teardown); a session keeps ONE RecognizeStream
    alive across turns in VAD mode (one WS per session, not per turn);
    `_instantiate_preview` filters options through split_values so
    non-schema knobs can't leak in from the UI — inject post-split where
    needed.
---

## 2026-06-11 - Johnny-trt.13
- Live interim captions in the playground shipped end-to-end:
  `TranscriptInterim` event type (`transcript_interim`, ephemeral — the
  status subscriber deliberately ignores it) added to the voice-pipeline
  union; `InterimTranscriptForwarder` in observability.py bridges the
  session's sync `user_input_transcribed` events to async bus publishes
  (MetricsTranslator pattern: ensure_future + strong task refs + aclose
  drain), skipping finals (they re-arm the dup guard), empty hypotheses and
  consecutive duplicates; `BrowserAgentSession` builds it in `build()`,
  registers it in `start()` (browser sessions ONLY — Meet/room path
  untouched), drains it in `aclose()`; ws.py maps the wire name to
  `transcript_partial`, which the playground + session-detail UIs already
  render (dashed "· partial" caption).
- Client caption lifecycle fixed (the partial handler existed but nothing
  ever cleared it): caption cleared on user `transcript_final` (streaming
  STT can emit several finals per turn — each clears, later interims
  reopen), on `transcript_filtered` (noise-dropped turn emits no final —
  added the event to sessionEvents.ts types), on teardown/endSession, and an
  empty hypothesis clears instead of rendering an empty line. Transcript-line
  transitions extracted to pure `transcriptLines.ts` (the controller's
  `$state` runes don't compile under this repo's vitest — pure module +
  delegation instead); same filtered/empty clears added to the session
  detail page's partial slot.
- Verified: 124 tests in touched backend files + full suite 3603 passed
  (9 failures pre-existing env-dependent live-API/wizard tests, reasons
  checked); frontend svelte-check 0/0, vitest 102/102 (8 new), eslint clean
  on touched files; ruff lint clean, additions format-clean. Live
  chrome-devtools run (session 88, dev stack, streaming Parakeet + Ollama
  llama3.2:3b + Piper, trt.5 fake-mic WAV): untampered 120 ms DOM trace
  shows the caption opening, growing in place at the ~480 ms sidecar decode
  cadence, and being removed in the same render its segment final appends —
  across multiple segments and a mic-mute freeze/unmute cycle (frozen
  caption finalized byte-identical in 244 ms); screenshot with the visible
  "You · partial" badge; 28 transcript_chunks rows all complete finals (zero
  interim pollution); decisions INV-1 clean for all normal turns; console
  clean. Artifacts: .validation/Johnny-trt.13/.
- Files changed: backend/johnny/voice_pipeline/events.py,
  backend/johnny/agent/observability.py,
  backend/johnny/agent/browser_session.py, backend/app/api/ws.py,
  backend/tests/{voice_pipeline/test_events.py,agent/test_observability.py,
  agent/test_browser_session.py,api/test_ws.py},
  frontend/src/lib/playground/{playgroundSession.svelte.ts,
  transcriptLines.ts (new),playgroundSession.test.ts (new)},
  frontend/src/lib/sessionEvents.ts,
  frontend/src/routes/sessions/[id]/+page.svelte, .ralph-tui/progress.md.
- **Learnings:**
  - Patterns discovered: chrome-devtools pageId-after-restart gotcha, the
    mic-mute caption-freeze screenshot method, and the interim-caption event
    semantics — all added to Codebase Patterns above.
  - Gotchas: ALL pipeline providers were inactive at session start (the
    playground "Session failed: no active stt provider" toast) — activate
    via POST /providers/{id}/activate and check the Ollama MODEL TAG the
    provider row pins (llama3.2:3b existed; the gpt-oss row pointed at an
    empty Docker Model Runner on :12434); 5 backend files were already
    non-format-clean at HEAD — verify with `git show HEAD:<f> | ruff format
    --check -` before blaming (or "fixing") your own diff; hard End-session
    mid-turn can double-write the turn's decision row (terminal-first
    ordering race, pre-existing — filed Johnny-9p4).
---

## 2026-06-11 - Johnny-1qr
- En-only in-process semantic turn detector shipped on the browser session.
  New `johnny/agent/turn_detector.py`: `InProcessInferenceExecutor`
  (InferenceExecutor protocol over the registered `_EUORunnerEn`;
  thread-offloaded shielded lazy load — a cold predict's 3 s timeout can't
  abort/duplicate the ~3.3 s load; threading-lock-serialized `run()`;
  `warm_up()` never raises) + `InProcessEnglishModel` (exposes the
  executor kwarg `EnglishModel` hides) + the
  `JOHNNY_BROWSER_FORCE_VAD_TURNS` kill-switch + en-language normalization.
  Factory grew `stt_language_from_provider_config` (same `_STT_LANGUAGE_KEYS`
  as the per-transcript stamp, so build gate and per-turn gate can't drift).
  `BrowserAgentSession.build` gained `semantic_eou: bool|None` (None=auto
  language gate — production; False=baseline arm; True=require-or-raise),
  engages the detector with `browser_semantic_endpointing()` =
  `{min_delay: 0.40, max_delay: 1.5}` over the SAME shared 0.40 s Silero,
  exposes `semantic_eou_active`/`turn_detection_label`, and `warm_up()`
  pre-loads the EOU runner alongside providers. Harness grew
  `--semantic-eou {auto,on,off}` (on stamps language=en into the stub STT
  options + requires engagement) + `turn_detection` label in
  report/JSON. Meet/room path untouched.
- **The bead's 0.20 s floor drop was built, measured, and reverted inside
  validation**: stub A/B at floor 0.20 measured felt p50 798→610 ms
  (−188 ms; vad_end 402→202) — but the live varied-pause run hit the abort
  criterion: 0.35 s-edge hesitations split (sidecar ~0.36 s finalize +
  terminal-punctuation hallucination at segment edges reads as complete to
  the model). Shipped config keeps the trt.6 floor; the detector is a pure
  quality upgrade: >0.40 s mid-thought pauses are HELD to 1.5 s instead of
  hard-cut (live session 97: held_02/03 = ONE decision each carrying both
  STT segments; confident-commit control split in two; trt.5 hesitations
  all single; 7 utterances → exactly 9 INV-1-clean decisions; console
  clean), at ~+12 ms e2e p50 bounce cost (felt-neutrality A/B 24/24 both
  arms).
- Abort criteria: RSS +413.7 MB in-image (< 500 line; load 3.31 s, warm
  predict p50 1.7 ms); varied-pause regression green in the shipped config.
- Verified: 3652 passed in-image (7 failures = the documented pre-existing
  invalid-OPENAI_API_KEY live tests + wizard image checks), 38 new
  turn-detector tests + reworked browser-session/harness tests; ruff
  lint+format clean on all 8 touched files (the one `ruff check` error in
  johnny/agent/ is browser_audio_io.py E501, pre-existing at HEAD).
- Files changed: backend/johnny/agent/turn_detector.py (new),
  backend/johnny/agent/browser_session.py,
  backend/johnny/agent/adapters/factory.py, backend/johnny/agent/session.py,
  backend/johnny/agent/latency_harness.py,
  backend/tests/agent/test_turn_detector.py (new),
  backend/tests/agent/test_browser_session.py,
  backend/tests/agent/test_latency_harness.py, docs/LATENCY.md,
  .ralph-tui/progress.md. Artifacts: .validation/Johnny-1qr/ (00-notes.md
  maps them; both floor configs' runs kept).
- **Learnings:**
  - Patterns discovered: semantic-EOU mechanics + punctuation-led verdicts +
    the floor-drop blocker, and the fullPage-screenshot state-reset gotcha —
    both added to Codebase Patterns above.
  - Gotchas: the playground auto-falls-back unified→split with a one-shot
    failed session row when pipeline_mode is stale (session 93/94 pair);
    `input_window` on agent_decisions is mode-only — correlate decisions to
    utterances via `created_at - bot_sessions.started_at` vs transcript
    `start_offset_ms`; bundled medium/long harness fixtures contain internal
    ≥0.2 s quiet runs (single-utterance only guaranteed at the 0.40 floor);
    the stub STT maps ANY segment to a fixed complete-question transcript,
    so semantic+low-floor stub runs split at fixture-internal pauses by
    design — use shorts-only there.
---

## 2026-06-11 - Johnny-trt.39
- Live bot-reply captions shipped: Johnny's reply text now streams into the
  playground chat + session live view sentence-by-sentence WHILE he speaks,
  reconciling to the authoritative AgentSpoke text on the turn terminal
  (trt.13 TranscriptInterim pattern mirrored on the bot side).
- Backend: new `AgentSpeechInterim` event (`text`, per-reply 0-based
  `sequence`, `turn_id` = same int the turn's TurnTerminal carries, None for
  ungated speeches) in the voice-pipeline union;
  `AgentSpeechInterimForwarder` in observability.py (sync fire-and-forget
  publish tasks + aclose drain, MetricsTranslator bridge pattern; resolves
  the turn from `gate.active_reply` once per reply at sequence 0);
  `JohnnyAgent.tts_node` calls the injected `speech_interim_sink` per
  flushed sentence (defensive wrapper; no emission on the no-TTS degrade
  path); `build_agent_runtime` wires forwarder→agent and carries it on
  `AgentRuntime` (drained in aclose) — both browser and Meet/room paths get
  it; ws.py maps wire name `agent_speech_partial`. Subscriber untouched
  (unknown types already pass through un-persisted).
- Frontend: `AgentSpeechPartialEvent` in sessionEvents.ts; pure bubble
  transitions in transcriptLines.ts (`upsertBotPartialLine` — seq-0 opens/
  replaces, tail appends with dup-drop, turnId pinned by first sentence;
  `clearBotPartialLine`; `clearBotPartialLineForTurn` — turn-matched or
  unpinned only); playground controller grows the bubble on
  `agent_speech_partial`, clears on `agent_spoke` (before appending the
  final) and on non-replied `turn_terminal`, clears both captions at
  teardown; LiveSession.svelte bot partial gets testid `bot-partial-line`;
  session detail page renders a "Johnny … speaking…" provisional row with
  the same lifecycle (+ clears on ended/failed status).
- Verified: backend 1970 passed/3 skipped (tests/agent+voice_pipeline+api+
  services; 14 new across events/observability/johnny_agent/job_session/
  ws); ruff lint clean, my files format-clean (5 touched files were already
  dirty-at-HEAD, additions clean); frontend vitest 113/113 (11 new bubble
  tests), svelte-check 0/0, eslint clean. Live chrome-devtools run (session
  101, dev stack, Parakeet+Ollama+Piper): 120 ms DOM traces show the bubble
  opening ~3.9 s after submit and growing in 5 per-sentence steps with text
  ~36 s ahead of the audio, the agent_spoke reconciliation replacing bubble
  with final in one sample, a mid-audio Stop clearing 780 ch of flushed
  sentences in ~101 ms with ZERO ghost lines (turn 17 = no_reply/barge_in,
  no utterance row), and the same lifecycle on /sessions/101; 6 utterance
  rows = 6 completed replies (final text only); 0 turns missing terminals;
  console clean. Artifacts: .validation/Johnny-trt.39/ (00-notes.md maps
  them).
- Files changed: backend/johnny/voice_pipeline/events.py,
  backend/johnny/agent/{observability.py,session.py,job_session.py},
  backend/app/api/ws.py, backend/tests/voice_pipeline/test_events.py,
  backend/tests/api/test_ws.py, backend/tests/agent/{test_observability.py,
  test_johnny_agent.py,test_job_session.py}, frontend/src/lib/
  sessionEvents.ts, frontend/src/lib/playground/{transcriptLines.ts,
  playgroundSession.svelte.ts,playgroundSession.test.ts},
  frontend/src/lib/components/playground/LiveSession.svelte,
  frontend/src/routes/sessions/[id]/+page.svelte, .ralph-tui/progress.md.
- **Learnings:**
  - Patterns discovered: the bot-interim event semantics (sequence-0 turn
    resolution, turn_terminal-BEFORE-agent_spoke wire order, interrupted
    replies emit no AgentSpoke) + the unmuted-mic speaker-echo barge-in
    trap — added to Codebase Patterns above.
  - Gotchas: `gate.active_reply` is set at speech BIND (speech_created),
    not playout start, and `_on_reply_done` only clears it if the ids still
    match — so "active" can already be the NEXT queued reply while the
    current one is still synthesizing; resolve correlation once at the
    reply's first flush, never per-sentence. zsh: `echo "==="` in a
    compound command errors ("== not found") — quote or avoid bare `===`
    separators in Bash tool calls.
---

## 2026-06-11 - Johnny-trt.40
- Fixed the operator-reported stale-history bug: Start → dictate/chat → End
  → Start again now opens an EMPTY playground chat window. Root cause:
  `endSession()`/`teardownLive()` deliberately keep final lines visible for
  post-session review, and `start()` never wiped them — plus event handlers
  weren't keyed to the session they subscribed for.
- Controller changes (frontend/src/lib/playground/playgroundSession.svelte.ts):
  (1) new `resetPerSessionUi()` (transcript incl. both trt.13/trt.39 live
  captions, lastDecisionAt, lastSpokenAt, isSpeaking, micLevel) called from
  `start()` on POST success and from `reattach()` after its validation
  early-returns (a REJECTED reattach leaves a reviewed window alone; seeding
  then fills the session's own history); user controls + composer draft
  deliberately survive. (2) `isActiveSession(id)` guard: `handleSessionEvent`
  takes the bound sessionId and drops frames from any non-active
  subscription (delayed finals, trailing pipeline events, and a stale
  `session_status_change(ended)` that would otherwise TEAR DOWN the new
  session); onOpen/onClose/onError are equally pinned so a deliberately
  closed old socket (which still fires onClose asynchronously) can't flip
  the fresh session's banner to "reconnecting". (3) Audited teardown: both
  end paths + destroy() close the subscription; `ReconnectingSubscription`
  drops post-close frames — the id guard covers the remaining async
  socket-lifecycle races.
- Tests: 4 new controller-level tests (playgroundController.test.ts) run the
  REAL controller under plain vitest via a globalThis `$state` identity shim
  (no svelte compiler): full repro start→chat→end→start asserts the wipe +
  closed old subscription; late stale final/bot-sentence/ended-status via the
  old subscription don't touch (or kill) session 2; reconnect cycle
  mid-session never resets; stale socket close can't flip connection after
  1200 ms debounce (fake timers). vitest 117/117, svelte-check 0/0, eslint
  clean on touched files (repo-wide lint has one PRE-EXISTING
  settings/+page.svelte `PermissionName` no-undef, file unchanged vs HEAD).
- Browser validation (chrome-devtools, dev stack, Parakeet+Ollama+Piper,
  sessions 102/103): exact repro driven live — #102 chat (user line + reply,
  trt.39 bubble seen reconciling), End, Start → #103 window empty across 10
  DOM samples / 5 s (`everHadLines=false`, badge `idle`), #103's own chat
  round-trips, /sessions/102 history intact, console clean, DB rows ended +
  correctly per-session. Artifacts: .validation/Johnny-trt.40/ (00-notes.md
  maps them).
- Files changed: frontend/src/lib/playground/playgroundSession.svelte.ts,
  frontend/src/lib/playground/playgroundController.test.ts (new),
  .ralph-tui/progress.md.
- **Learnings:**
  - Patterns discovered: the `$state`-shim controller-testing recipe and the
    per-session scoping rules — added to Codebase Patterns above.
  - Gotchas: node:assert/strict `deepEqual(x, [])` narrows `x` to `never[]`
    via its asserts-signature (use `.length`); `npx prettier` pulls an
    unpinned version and false-flags files — this frontend has NO prettier,
    `pnpm lint` is just `eslint .`.
---
