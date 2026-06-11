# Voice Latency — Targets, Methodology, Tuning

This doc captures what we know about Johnny's voice pipeline latency, the
target we are aiming for, how to measure it, and where to look first
when a turn feels slow.

It is the companion to the **Latency & tuning tips** sections you see
inside each provider's settings modal at `/providers`. Those tips
codify per-provider rules of thumb; this file holds the cross-provider
methodology.

## Targets

The user-felt latency budget — VAD-detected user-speech-end → first
audible byte out of the bot's mouth — is the number the user cares
about, not synthesis time alone.

| Stack                                              | p50 target | p95 target |
| -------------------------------------------------- | ---------- | ---------- |
| All-local (Parakeet/whisper + Ollama + Piper)      | 300 ms     | 500 ms     |
| Mixed local STT/TTS + cloud LLM                    | 250 ms     | 450 ms     |
| All-cloud (Deepgram + OpenAI + ElevenLabs Flash)   | 200 ms     | 400 ms     |

Why 300 ms / 500 ms for the local stack: research on conversation turn-
taking (Stivers et al.) puts cross-cultural human response gaps at
roughly 200 ms, with a long tail past 500 ms reading as "they're
thinking". Aim under 500 ms and the bot feels alive; over 1 s and it
feels broken.

**Measured TTS contribution (updated after the Johnny-1ge runtime epic).**
The TTS first-byte component of the local budget is now **60 ms p50 /
106 ms p95** in real measured turns with the **Piper
`persistent-subprocess`** runtime (was 855 ms p50 *every* turn on the
`subprocess` runtime — see the measured baseline below). So on the
all-local stack, TTS is no longer the dominant cost — the **answer LLM
and the router own ~95 % of the felt budget**, and the 2026-06-10
baseline puts hard numbers on that: the local stack currently sits at
seconds, not hundreds of ms, of felt latency (the Johnny-trt epic exists
to fix exactly this). Single-synthesis warm numbers per runtime: Piper
persistent ~40 ms · Piper http-sidecar ~90 ms · Kokoro http-sidecar
~425 ms · KittenTTS http-sidecar ~1.8 s (atomic synth). Full methodology +
cold numbers + the other providers/runtimes live in
[TTS_RUNTIMES.md](TTS_RUNTIMES.md#3-comparison-table).

## Latency map — the stages we measure

Each turn passes through these stages. The total is the only number
the user feels, but the breakdown is where bottleneck attribution
lives.

```
VAD end-of-speech  ──┐
                     │  endpointing delay (LiveKit agent engine:
                     │  min_endpointing_delay 0.5 s default; bakes in
                     │  the wait for natural mid-sentence pauses)
STT first-partial  ──┤  streaming paths only (Parakeet mlx-sidecar,
                     │  Deepgram); N/A on batch paths
STT final          ──┤  Parakeet MLX sidecar streaming: −109 ms p50 —
                     │  the final precedes the VAD commit (trt.15;
                     │  forced-batch path: +136 ms after it);
                     │  Deepgram: ~170 ms p50 after VAD commit (trt.14)
LLM first-token    ──┤  Router LLM (side call) + answer LLM. Hot path.
LLM total          ──┤  Answer LLM. ttft == total until Johnny-dny.
TTS first-byte     ──┤  Local default (Piper persistent): 60 ms p50
                     │  measured in real turns, incl. the chunk buffer
                     │  at 4096 bytes / 22050 Hz (855 ms p50 on the
                     │  subprocess runtime). See docs/TTS_RUNTIMES.md.
First audio frame  ──┘  to transport
```

The endpointing delay is intentional padding for natural mid-sentence
pauses (the retired split engine called it `end_of_speech_ms`, 800 ms —
see Johnny-arh; the agent engine uses LiveKit's endpointing instead).
Don't try to optimise it away — the user-felt budget is **from**
speech-end, not from when the last voiced frame arrived. What we have
to optimise is everything **after** the VAD says "done".

Two knobs set that padding on the agent engine (Johnny-trt.5), and they
**overlap rather than stack**: Silero's `min_silence_duration` (when the
VAD calls end-of-speech — batch STT starts transcribing here) and the
engine's endpointing `min_delay` (anchored at the *last detected speech*,
not at VAD-end; the turn commits at
`max(min_silence, min_delay, STT-final)` after the user stops). Browser
sessions load Silero at a **0.40 s** floor
(`BROWSER_VAD_MIN_SILENCE_DURATION_S`, single-speaker surface — see the
shipped section below); the Meet/room path keeps Silero's 0.55 s default
(multi-party padding, Johnny-arh). Measured (24-turn stub harness A/B,
warm percentiles, 2026-06-10): `vad_end` p50 562 → 401 ms / p95 600 →
443 ms — the 0.40 s floor commits the turn **~161 ms earlier**, and a
20-turn varied-pause script (mid-sentence hesitations of 0.20–0.35 s,
several at the 0.35 s edge) still reads as exactly one VAD utterance per
turn (zero premature commits; artifacts under `.validation/Johnny-trt.5/`).
The browser session also sets the
engine endpointing to `{"min_delay": 0.40}`
(`BROWSER_ENDPOINTING_MIN_DELAY_S`, Johnny-trt.6) — equal to its VAD
floor, so the engine adds zero padding on top of Silero and the turn
commits at `max(VAD floor, STT-final)`. Measured (24-turn stub harness
A/B, warm percentiles, 2026-06-11): `first_audio_wall` p50 839 → 801 ms /
`router` p50 102 → 66 ms — ~37 ms felt with the stub's 80 ms STT finals.
On the local Parakeet path (~123 ms finals) the commit is
STT-final-bound, so the retune is felt-neutral today; what it buys is
removing the engine's 0.5 s floor so streaming STT (Phase 2) and fast
cloud finals commit right at the VAD floor. `max_delay` stays at the SDK
default — with `turn_detection="vad"` nothing ever escalates to it. The
Meet/room path still passes no endpointing (0.5 / 3.0 defaults,
multi-party padding).

**Semantic turn detector: multilingual wontfixed (Johnny-trt.6); the
English-only model SHIPPED in-process (Johnny-1qr — see the shipped
section below).** The trt.6 plan was LiveKit's multilingual EOU model
in-process on the roomless browser session (an `InferenceExecutor` shim
around the registered `_EUORunnerMultilingual`) paired with
`{"min_delay": 0.2, "max_delay": 1.5}` — commit early when the model
calls the utterance complete, hold hesitant speech to 1.5 s. The spike
killed the multilingual model on the bead's own abort line (RSS >
~500 MB): the multilingual ONNX is 396 MB on disk and `initialize()`
costs **+884 MB RSS** in the api process (~1.0 GB total with imports;
`enable_cpu_mem_arena=False` doesn't help — still ~1.05 GB total). Warm
inference is ~12 ms, so CPU was never the issue. Two extra findings: the
multilingual revision (v0.4.1-intl) supports 14 languages **not including
Finnish** (the per-turn `supports_language` gate would skip it for fi
configs anyway), and the EOU bounce task only runs after VAD
END_OF_SPEECH — the model cannot commit *below* the Silero floor, so
"commit at ~0.2 s" additionally requires dropping the VAD floor, which
re-segments the batch-STT StreamAdapter. The **English-only** model fits
the line (+414 MB RSS re-measured in-image, 1.7 ms warm prediction) and
shipped as Johnny-1qr. Full spike numbers:
`.validation/Johnny-trt.6/00-spike-note.md`.

## How to measure on this machine

### Quick sniff test (no instrumentation)

1. Open `/playground` in the browser.
2. Start a session with the active providers (NVIDIA Parakeet, Ollama,
   ElevenLabs as of 2026-06-10 — check `/providers` for today's set).
3. Say one short phrase. Use the **bot icon ping** — the time between
   the transcript appearing and the first audio byte playing is your
   real-world time-to-first-audio. Eyeball five or ten turns of varied
   length.
4. Watch `docker compose logs -f worker` while talking — the
   per-utterance logs include the STT model size, classifier timeout
   firings, and noise-filter drops. Anything unexpected here is the
   first place to look.

### Scripted 20+-turn capture (the Johnny-cxu method)

This is how the measured baseline below was captured, end to end on the
real `/playground` path (real browser, real WebSocket, real providers):

1. **Build a fake-mic WAV**: synthesize ~24 bot-addressed utterances of
   varied length (2–9 s) with the in-image piper CLI (use a different
   voice than the bot's), concatenate at 48 kHz mono S16LE with a 5 s
   silent head and 10 s gaps. Generator pattern preserved in
   `.validation/Johnny-cxu/gen_fake_mic.py`.
2. **Restart the shared Chrome with the fake mic**:
   `CHROME_EXTRA_FLAGS="--use-fake-device-for-media-stream
   --use-fake-ui-for-media-stream
   --use-file-for-fake-audio-capture=/path/to.wav
   --disable-features=AudioServiceSandbox" ./scripts/start-chrome.sh`
   (pkill the running Chrome first; `AudioServiceSandbox` must be off on
   macOS or the audio service silently reads no file and the mic stays
   silent). The WAV restarts per getUserMedia stream and loops at EOF.
3. **Drive `/playground`**: autonomous mode, no personality override,
   persona pinned to one-sentence answers. Each WAV utterance becomes a
   real VAD→STT→router→LLM→TTS turn; the 10 s gaps let replies finish
   (long replies get barge-in cut by the next utterance — fine, the
   head-of-line timings are already emitted).
4. **Read per-stage rows** from `session_timings` (the Johnny-ckz.7
   instrument) and compute percentiles —
   `.validation/Johnny-cxu/analyze_cxu.py`. Stage starts are
   reconstructed as `started_at_ms - duration_ms` (LiveKit stamps
   metrics at stage END); the answer-LLM start minus the STT end is the
   router+gate gap; `tts_start + ttfb` minus the STT start is the felt
   end-to-end. Two instrument caveats, verified 2026-06-10: STT rows
   attach to the *previous* turn_id (Johnny-5vb), so pair STT rows to
   replies by timestamp, not turn_id; and `answer_llm`
   time-to-first-token equals total until Johnny-dny lands (the
   openai-compatible adapter has no true streaming).

Derived e2e was cross-checked against wall-clock `parakeet.transcribe` /
`piper.synth` api-log timestamps: agreement within ~30 ms.

### One-command scripted harness (Johnny-trt.1 — use this for re-measurement)

`johnny.agent.latency_harness` packages the Johnny-cxu method into a single
in-container command — no browser, no Chrome flags, no SQL: it drives a real
roomless `BrowserAgentSession` (the playground engine, every Phase-2 seam)
through a fake `BrowserAudioTransport`, pushing the bundled piper-synthesized
bot-addressed fixtures (`johnny/agent/fixtures/latency_turn_*.pcm` — the full
Johnny-cxu 24-utterance set, so a default-length local run repeats no question
and the real router has no repeat to decline) as real-time-paced
16 kHz / 20 ms PCM frames, and derives the same per-stage stats from the
Johnny-ckz.7 `PipelineTiming` rows collected on an in-process bus (same emit
half as `session_timings`; nothing is written to the DB).

```bash
# stub providers (CI-shaped, no network): ~3 min for 20 turns
docker compose exec api python -m johnny.agent.latency_harness --turns 20

# the configured local providers (whatever /providers has active):
docker compose exec api python -m johnny.agent.latency_harness \
    --turns 24 --providers local --json-out /tmp/latency.json

# size the session-start prewarm win (Johnny-trt.8): one run without and one
# with --prewarm, comparing each run's cold turn against its own warm turns.
# --prewarm awaits the providers' warm_up() before turn 1 (production fires
# the same warm-up concurrently with session start).
docker compose exec api python -m johnny.agent.latency_harness \
    --turns 6 --providers local --prewarm

# A/B a Silero end-of-speech floor (Johnny-trt.5): by default the harness
# runs the browser session's own VAD (0.40 s min-silence); --vad-min-silence-s
# loads a different floor (0.55 reproduces the pre-trt.5 Silero default).
docker compose exec api python -m johnny.agent.latency_harness \
    --turns 24 --vad-min-silence-s 0.55

# A/B the engine endpointing min_delay (Johnny-trt.6): by default the harness
# runs the browser session's own endpointing (min_delay 0.40 s);
# --endpointing-min-delay-s overrides it (0.5 reproduces the pre-trt.6
# LiveKit engine default).
docker compose exec api python -m johnny.agent.latency_harness \
    --turns 24 --endpointing-min-delay-s 0.5

# A/B the in-process semantic turn detector (Johnny-1qr): auto (default)
# follows the configured STT language exactly like production (the stub trio
# carries none → VAD-only; --providers local follows the operator's config);
# on stamps language=en into the STT options and REQUIRES the detector (pair
# with --prewarm so turn 1 skips the ~400 MB model load); off forces the
# VAD-only baseline arm.
docker compose exec api python -m johnny.agent.latency_harness \
    --turns 24 --prewarm --semantic-eou on
```

Output: per-stage p50/p95/min/max — `vad_end_ms` (speech-end → VAD commit,
wall-clock), `stt_ms`, `router_ms`, `llm_ttft_ms`, `llm_total_ms`,
`sentence_gap_ms`, `tts_ttfb_ms`, `first_audio_wall_ms` (speech-end → first
PCM frame on the transport, wall-clock — the felt e2e) and
`e2e_vad_commit_ms` (the baseline-comparable derived e2e: VAD commit → TTS
first byte). Turn 1 is always reported separately as the **cold start** (the
session is built fresh per run), with warm percentiles over turns 2..N — the
split the Phase-1 prewarm work measures against. Turns run strictly
sequentially (next utterance only after the previous reply's terminal), so
runs are barge-in-free by construction; per-session context still accumulates
turn over turn, exactly like a real session — match turn counts when
comparing runs. The pytest integration
(`tests/agent/test_latency_harness.py`) runs the stub mode end-to-end.

**Sanity gate vs the manual baseline (2026-06-10, same provider trio:
Parakeet MLX sidecar + Ollama `llama3.2:3b` + Piper persistent).** A 24-turn
`--providers local` run (19 replied, 5 router-declined, 0 timeouts; warm
percentiles over 18 turns) landed: STT 119/156 ms vs baseline 116/251
(p50 +3 %), TTS first byte 78/185 vs 60/106, VAD-commit wait ~563 ms p50 —
the hardware-bound stages match the baseline. The LLM-bound stages sit well
*below* the whole-session baseline percentiles (router 910/1513 vs
3385/4726; answer 562/821 vs 3068/4099; felt e2e 2356/3186 vs 6792/8664):
the baseline percentiles accumulated 29 replied turns over ~2 WAV loops of
chat context, while a fresh 24-turn harness session sits at the left edge of
the very context-growth curve the baseline documented — the harness's own
in-run trend reproduces it (router 758 → 1763 ms, answer 220 → 902 ms, e2e
1805 → 3340 ms across the session; same ordering, same dominant bottleneck:
the two LLM calls own ~85–90 % of the post-VAD budget). Matched-index turns
(turn 2: 1805 ms vs 3087 ms) leave a ~40 % environmental residual: the
manual baseline ran under a live Chrome/playground stack with the
reply-audio recorder on, the harness is headless with the recorder off. The
cold turn shows exactly what Phase-1 prewarm targets: router 2253 ms (cold
Ollama prompt cache) + TTS first byte 559 ms (piper voice spawn). Use the
harness for phase-over-phase deltas at matched turn counts — not to
reproduce the manual baseline's absolute numbers.

### Targeted measurement with chrome-devtools MCP

For repeatable numbers across changes, use chrome-devtools MCP
performance trace to capture the network and timing on the playground
WebSocket. The `mcp__chrome-devtools__performance_start_trace` /
`performance_stop_trace` pair will surface websocket frame timings,
which you can correlate with worker logs by timestamp.

### Manual stopwatch comparison

For provider A/B comparisons (e.g. Whisper base vs small), the cheapest
honest method is: keep everything else fixed, swap the one variable in
the providers UI, run 20 short fixed-content turns through the
playground, eyeball the worker logs for the time between
`transcript final` and `tts first frame`. Recording the audio with a
phone and inspecting the gap in Audacity gives you a few-ms precision
floor.

## Measured baseline — 2026-06-10, two 28+-turn runs (Johnny-cxu)

Captured with the scripted fake-mic method above on an M-series Mac
(16 GB), on the **configured local stack of that day**: NVIDIA Parakeet
0.6B TDT v3 (MLX sidecar) STT + Ollama `llama3.2:3b` LLM + Local Piper
`en_GB-northern_english_male-medium` TTS, `chunk_bytes` 4096, reply-audio
recorder ON (production shape). Note the config drift since this doc was
first written: there is no faster-whisper row configured anymore — the
local STT is Parakeet. Sessions 72 (Piper `subprocess`) and 73 (Piper
`persistent-subprocess`), 28 and 29 spoken turns analysed, zero rows
dropped. Ollama explicitly pre-warmed. All numbers are ms.

| Stage (per spoken turn)             | `subprocess` p50 / p95 | `persistent` p50 / p95 |
| ----------------------------------- | ---------------------- | ---------------------- |
| VAD end → STT final (Parakeet)      | 123 / 350              | 116 / 251              |
| STT final → answer-LLM start (router+gate) | 2 420 / 4 458   | 3 385 / 4 726 †        |
| Answer-LLM first token (= total ‡)  | 3 002 / 4 090          | 3 068 / 4 099          |
| TTS first byte (Piper)              | **855 / 914**          | **60 / 106**           |
| **End-to-end: speech-end → first audio to transport** | **6 769 / 9 249** | **6 792 / 8 664** |

† Not a persistent-runtime regression — the router gap grows with
accumulated chat context, and run 73 ran ~2 WAV loops (more turns of
history by the time the percentiles accumulate). See the context-growth
note below.
‡ `answer_llm` time-to-first-token **equals** total generation: the
openai-compatible adapter has no true `stream_chat`, so the base-class
fallback buffers the entire completion (Johnny-dny). "STT first partial"
is not measurable on this stack — batch Parakeet emits finals only
(streaming STT is Johnny-trt Phase 2).

### Where the time goes (evidence)

At the persistent-runtime p50, the 6.79 s felt latency decomposes as
STT 116 (1.7 %) + router gap 3 385 (50 %) + answer LLM 3 068 (45 %) +
TTS first byte 60 (0.9 %). **The two LLM calls own ~95 % of the felt
latency.** TTS and STT are solved problems on this machine until the
LLM side is fixed (Johnny-dny for answer streaming, Johnny-trt Phase 3
for router triage).

A representative activity-log trace (session 72, turn 9 — "capital of
France", `session_timings` rows; starts reconstructed as
`started_at_ms − duration_ms`):

```
stt         dur=128   audio=4660ms                      (utterance final)
answer_llm  start=+2152 after stt end   dur=2812  ttft=2812  ← buffered
tts seg 1   start=at llm end            dur=1051  ttfb=1026  (subprocess spawn)
tts seg 2-5 pipelined behind playout    ttfb≈891-926 each
tts seg 6   cancelled=true              (barge-in from next utterance)
e2e: speech-end → first audio = 6 118 ms
```

Same turn shape on session 73 (persistent runtime): first TTS segment
`ttfb=69`, e2e 5 120 ms — the spawn tax is gone from every segment.

### Before/after persistent piper (Johnny-6t5 → Johnny-1ge)

The two runs drove the **identical utterance sequence**, so early
matched turns isolate the runtime change cleanly (same context size,
same questions — turns 2–8, short questions, fresh session):

| | `subprocess` | `persistent-subprocess` | delta |
| --- | --- | --- | --- |
| TTS first byte, per turn p50  | 855 ms | 60 ms | **−795 ms (−93 %)** |
| e2e, matched early turns      | 4 007–6 331 ms | 3 087–4 159 ms | **≈ −930 ms median** |
| One-time cost | — | ~550 ms voice load on the first synth after activation | |

The ~60 ms warm first byte is the ONNX synth time of the first
library chunk, **not** the `chunk_bytes` read buffer: the persistent
runtime streams the piper library's own chunks and never consults
`chunk_bytes` on its first-byte path (Johnny-trt.7 measured the
earlier "~93 ms buffer" attribution and falsified it — see the
chunk_bytes candidate below).
The session-wide e2e p50 (table above) does *not* show the −930 ms
because the longer run's router growth ate it; the matched-turn
comparison is the honest read on what the runtime is worth.

### Latency grows with session context — the hidden second bottleneck

Within session 73, e2e went from **3.1 s on turn 2 to 9.2 s on turn 33**
(~3×). Both LLM calls slow down as chat history accumulates
(`llama3.2:3b` prompt processing scales with context): the router gap
went 1.2 s → 4.8 s and answer TTFT 1.4 s → 4.5 s across one session.
Felt latency is not a constant — it is a function of session length
until history windowing lands.

Second-order finding: once router+LLM exceed the pause between
utterance fragments, the in-flight reply is barge-in cancelled before
its first TTS segment — in session 73 ten consecutive two-sentence
turns produced **zero audio** (`agent_decisions` outcome `suppressed` /
`barge_in`, INV-1 terminals all clean). At current latencies Johnny
cannot keep up with a brisk multi-sentence speaker; this is the epic's
premise observed live.

Raw artifacts: `.validation/Johnny-cxu/` (fixture generator, analyzer,
per-run analysis, screenshots). Single-synthesis TTS-runtime
comparisons (incl. Kokoro / KittenTTS / http-sidecar cells) live in
[TTS_RUNTIMES.md](TTS_RUNTIMES.md#3-comparison-table).

On the **all-cloud** stack, latency is dominated by network RTT plus
first-byte time of each provider. EU/US round-trips of 30–80 ms
make a meaningful difference; pick the closest region you can.
Measured all-cloud p50/p95: see the dedicated baseline section below
(Johnny-trt.14, 2026-06-11).

## Measured all-cloud baseline — 2026-06-11 (Johnny-trt.14)

Captured with the trt.1 scripted harness (24 turns, `--providers local
--prewarm`) on the cloud trio: **Deepgram nova-2** STT (en-US, server
endpointing 300 ms, interims on) + **Anthropic `claude-haiku-4-5`** LLM +
**ElevenLabs Flash v2.5** TTS (`pcm_16000`). The LLM slot of the target
row says "OpenAI", but the configured `OPENAI_API_KEY` was dead (401), so
Haiku — the same speed class — stands in; the target is about the stack
*shape* (every leg a cloud round trip), not the vendor. Semantic EOU was
engaged (`semantic-eou(en)` — Deepgram's `en-US` normalizes to `en`),
endpointing 0.40/1.5 s, VAD floor 0.40 s: the production browser-session
defaults. 24/24 turns completed, 16 replied; the 8 `no_reply`s are 5
*reasoned* router declines on Deepgram-split fragments (see the
split-turn note below) plus replies barge-in-cut by the next fixture. All
numbers ms, warm = replied turns 2..24 (n=15).

| Stage (warm)                                   | p50   | p95   |
| ---------------------------------------------- | ----- | ----- |
| Speech-end → VAD commit (0.40 s floor)         | 404   | 433   |
| VAD commit → Deepgram final                    | 170   | 235   |
| Commit wait (speech-end → turn commit)         | 590   | 635   |
| Router + gate residual (derived, see note)     | 1 533 | 2 030 |
| Answer LLM (Haiku; TTFT == total, buffered)    | 727   | 1 121 |
| TTS first byte (ElevenLabs Flash)              | 177   | 187   |
| **Felt e2e: speech-end → first audio frame**   | **3 121** | **3 814** |
| Post-commit pipeline (turn commit → first audio) | 2 494 | 3 193 |

Cold turn (prewarmed session, first cloud connections): 4 126 ms felt —
the ~1 s premium over warm p50 is TLS/connection establishment across the
three providers.

**Verdict vs the 200/400 ms target: missed by ~8×, and the miss is
entirely the two LLM round trips.** Router (≈1.5 s incl. gate overhead)
plus answer (≈0.7 s) own ~90 % of the post-commit budget — the same
structure as the local baseline, just smaller absolute numbers. The
stages the cloud stack was supposed to prove are in fact at-floor:
Deepgram's final lands only ~170 ms after the VAD commit (its 300 ms
server endpointing overlaps the 0.40 s VAD floor almost entirely) and
ElevenLabs Flash first byte is a rock-steady 177/187 — both inside the
200/400 envelope on their own. The target is unreachable while a router
LLM round trip sits in the hot path and the answer adapter buffers the
full completion (the Anthropic adapter has no `stream_chat` either —
TTFT == total, the Johnny-dny gap applies to it too). Phase 3 router
triage + Johnny-dny answer streaming are the levers; re-measure this
stack after both.

Measurement notes specific to the cloud path:

- **No LiveKit STT metric rows exist on the Deepgram direct-streaming
  path** (it bypasses the StreamAdapter, and streaming finals emit no
  `STTMetrics` the way the batch `recognize()` path does), so the
  harness's `stt_ms` / `router_ms` / `e2e_vad_commit_ms` columns are
  null. The wall-clock `stt_final_after_vad_end_ms` instrument still
  works, and the router+gate row above is derived per turn as
  `first_audio_wall − (vad_end + stt_final_after_vad_end) − llm_total −
  tts_ttfb` (it therefore folds in EOU predict + reply scheduling
  overhead alongside the router LLM round trip).
- **Deepgram split-turn semantics** mirror the trt.12 mlx-sidecar
  finding: the server's 300 ms endpointing fires finals at intra-utterance
  pauses well below the fixtures' clause gaps, so multi-clause fixtures
  split at the 0.40 s VAD floor and the orphaned fragments ("Johnny," …)
  get reasoned router declines. Real conversation tolerates this better
  than scripted fixtures (a fragment decline is usually correct), but
  floor-drop experiments must account for it.
- The run only worked after the **Anthropic structured-output fix**
  shipped with this bead (`app/providers/anthropic_llm.py`): the adapter
  used to drop `response_format` entirely — the model never saw the
  router's decision schema and fenced its JSON in markdown, which the
  strict parser rejected, so **every** turn terminalized
  `no_reply(router_declined, 'router returned no structured output')`.
  The adapter now injects the schema + a JSON-only instruction into the
  request `system` text and parses fence-/prose-tolerantly.

**Deepgram interim verification (the other half of trt.14):** a live
playground voice session (chrome-devtools MCP, session #104, fake-mic
WAV) on the same trio showed the trt.13 live caption growing through
multiple distinct Deepgram hypotheses per utterance ("John," →
"Johnny," → "Johnny, what day comes" → "Johnny, what day comes after
Friday?"), clearing on each final, with replies flowing and
`transcript_chunks` persisting finals only (9 rows, interims ephemeral
by design). One cosmetic nuance: Deepgram finals carry a speaker id, so
the transcript pane labels them "Speaker" (`speaker-line`) rather than
"You" (`user-line`) — flow is identical. Raw artifacts:
`.validation/Johnny-trt.14/` (harness log + JSON, DOM trace,
frozen-caption screenshot).

## In-UI tips on every provider settings page

Each provider's settings modal at `/providers` now ships a **Latency
& tuning tips** section (see `/providers/+page.svelte` and the
`tips` field on each adapter's `field_schema()`). Tips lead with a
concrete number where possible, name the knob the tip applies to,
and explicitly say what the trade-off is. To add or revise a tip,
edit `tips=(...)` in the adapter's `field_schema()` classmethod and
rebuild the api image (the api is COPY-baked; see the codebase
patterns in `.ralph-tui/progress.md`).

## Provider prewarm at session start — ✅ SHIPPED (Johnny-trt.8, 2026-06-10)

Before this change the FIRST turn of every browser session paid the
providers' lazy one-time loads on top of its normal stage costs:
faster-whisper's weight load (`_ensure_model`), Piper's ~700 ms voice ONNX
load, and — dominant on the local stack — Ollama's GGUF model load when the
model wasn't resident.

Every provider now has an optional `async warm_up()` hook
(`app/providers/base.py`, default no-op). Right after
`BrowserAgentSession.build(...)` the browser runner
(`app/services/browser_pipeline_runner.py`) fires every assembled provider's
hook **concurrently, as a background task** — session start never waits on
it, and a failed warm-up is logged and non-fatal (the first turn just pays
the lazy load as before). Implemented hooks:

- **faster-whisper** — loads the Whisper weights (`_ensure_model`).
- **Piper, persistent runtime** — a throwaway tiny synth of the configured
  voice pays the ONNX load into the process-wide warm cache. `subprocess`
  (cold start is structural) and `http-sidecar` (the sidecar warms itself
  at launch) stay no-ops, as does batch STT via the Parakeet sidecars.
- **openai-compatible LLM** — a 1-token `max_tokens: 1` ping so the server
  (Ollama, LM Studio, vLLM) loads the model. Hosted-API adapters (OpenAI,
  Anthropic, Gemini) keep the no-op: nothing to load, and a per-session
  ping would only burn quota.

Measured (2026-06-10, the baseline trio — Parakeet MLX + Ollama
`llama3.2:3b` unloaded before each run + Piper persistent
`en_GB-northern_english_male-medium`; 6-turn harness runs, artifacts under
`.validation/Johnny-trt.8/`):

| 6-turn local run            | turn-1 router | turn-1 tts_ttfb | turn-1 e2e (VAD commit → first byte) | same-run warm e2e p50 |
| --------------------------- | ------------- | --------------- | ------------------------------------ | --------------------- |
| without prewarm             | 2953 ms       | 576 ms          | **3903 ms**                          | 2130 ms               |
| with prewarm                | 606 ms        | 57 ms           | **1007 ms**                          | 1546 ms               |

The warm-up itself took 1453 ms wall (Ollama ping 1452 ms ∥ Piper voice
575 ms ∥ Parakeet no-op 0 ms) — paid while the session is still being set
up, not by the first speaker. With prewarm the first turn is no longer the
slowest turn but the *fastest* (smallest chat context), i.e. comfortably
within the ≤ 100 ms-of-steady-state acceptance bar. Reproduce with the
`--prewarm` harness flag (above); the harness awaits the warm-up before
turn 1 so the cold turn measures warmed state deterministically.

**Keep Ollama resident between sessions: `OLLAMA_KEEP_ALIVE=24h`.** The
ping loads the model *at session start*; Ollama's default `keep_alive`
(5 min) then evicts it again after idle, so a session that goes quiet —
or the gap between two sessions — re-pays the 1–5 s GGUF load (mid-session:
on the next router call, where the prewarm can't help). Set it on the
**host's** Ollama server (it is not a Johnny/compose variable — see the
note in `.env.example`):

```bash
# macOS app install (persists across restarts):
launchctl setenv OLLAMA_KEEP_ALIVE 24h   # then restart the Ollama app
# or for a manually-run server:
OLLAMA_KEEP_ALIVE=24h ollama serve
```

## Phase-1 capstone — re-measured (Johnny-trt.11, 2026-06-11)

Phase 1 of the Johnny-trt epic shipped five hot-path changes: browser
Silero floor 0.55 → 0.40 s (trt.5), browser engine endpointing
`min_delay` 0.5 → 0.40 s (trt.6; the semantic detector itself was
wontfixed on RSS), Piper `chunk_bytes` default 4096 → 1024 (trt.7 —
measured TTFB-neutral), provider prewarm at session start (trt.8),
client-side auto barge-in (trt.9 — a client-side cut, by design not
visible to the server-side harness), and the Meet bridge audit (trt.10 —
found the ~2 s pacat buffer, fix bead Johnny-dkj). This section is the
phase gate: the re-measured phase-over-phase deltas. All artifacts under
`.validation/Johnny-trt.11/`.

**Controlled felt delta — stub harness (deterministic LLM/TTS).** With
provider noise stubbed out, the felt p50 effect of the Phase-1 knobs is
clean. 24-turn stub runs, warm percentiles, `first_audio_wall_ms`
(speech-end → first PCM frame):

| config                                    | warm felt p50 | vs Phase-0 |
| ----------------------------------------- | ------------- | ---------- |
| Phase-0 shape (floor 0.55, endpointing 0.5) | 958.6 ms    | —          |
| floor 0.40 only (endpointing 0.5)          | 834.8 / 838.9 ms † | −121 ms |
| Phase-1 shape (floor 0.40, endpointing 0.40) | 801.0 ms   | **−157.6 ms (−16 %)** |

† measured independently on two days (trt.5's A/B and trt.6's A/B) —
agreement within 4 ms, which is the instrument's run-to-run noise with
stub providers. **The Phase-1 acceptance bar (felt p50 ≥ 100 ms better
than Phase-0) is met on the controlled measurement: −157.6 ms.**

**Local stack — four 24-turn `--providers local` runs, ABBA-counter-
balanced (A = Phase-0 shape `--vad-min-silence-s 0.55
--endpointing-min-delay-s 0.5`, no prewarm; B = Phase-1 defaults +
`--prewarm`), Ollama unloaded before every run.** Same trio as the
Phase-0 baseline (Parakeet MLX + Ollama `llama3.2:3b` + Piper
persistent; stored `chunk_bytes` 4096 held constant; one drift — the
configured voice is now `en_US-hfc_male-medium`, constant across all
four runs). Pooled warm per-turn samples (A n=31, B n=36):

| stage (warm p50/p95, ms)  | Phase-0 shape | Phase-1 active | delta |
| ------------------------- | ------------- | -------------- | ----- |
| vad_end (turn commit)     | 563 / 604     | 404 / 423      | **−159 / −181, deterministic** |
| stt                       | 152 / 210     | 153 / 206      | flat  |
| router                    | 970 / 1402    | 1103 / 1563    | +133 / +162 (run noise, identical code) |
| llm_ttft                  | 524 / 679     | 593 / 777      | +69 / +98 (run noise) |
| tts_ttfb                  | 74 / 244      | 93 / 206       | +19 / −38 |
| first_audio_wall (felt)   | 2218 / 2800   | 2276 / 2902    | +58 / +103 (masked, see below) |
| **cold turn 1 (felt e2e, per run)** | **4413 / 3619** | **1661 / 1725** | **−2.3 s mean (prewarm)** |

The turn-commit win is deterministic — every Phase-1 run, every turn,
−160 ms with a few-ms spread — and the prewarm takes the cold turn from
the slowest turn of a session (router 2558/2043 ms, tts 610/632 ms) to
the *fastest* (router ~850 ms, tts ~70 ms). The warm felt total is
statistically flat despite the −159 ms commit shift because the two
LLM stages (~1.6 s, ~75 % of the felt budget) drift more between
*identical-config* runs than the entire knob effect: matched-fixture
router+llm medians moved +330 ms between pair-1 arms on identical code
(run-to-run Ollama noise + context-growth differences from stochastic
router declines — B-arm runs replied more turns, so they carried more
chat history at every matched fixture). That noise floor is exactly the
documented Phase-2/3 territory (streaming STT, router triage, history
windowing); Phase 1 was never going to move router/llm. For comparison
against the recorded Phase-0 sanity run (warm felt p50 2356.2): the two
Phase-1 runs landed 2257.1 and 2320.4 (−99/−36 ms), while the same-day
Phase-0-shape controls landed 2173.2 and 2295.4 — same spread, same
conclusion: on the local stack the felt number is LLM-noise-bound and
the harness resolves Phase-1's effect in the stages it touched, not in
the pooled felt total.

**Varied-pause regression — zero premature turn-cuts.** All 20
hesitation fixtures (mid-sentence gaps 0.20–0.35 s, several at the
0.35 s edge) and all 24 bundled latency fixtures read as exactly one
Silero utterance each at the production 0.40 s browser floor
(`verify_fixtures_at_floor.py`, real engine VAD, real frame pacing).

**Client barge-in false-positive check — clean (live session 84,
chrome-devtools).** With the trt.9 oscillator fake-mic held at
sub-threshold "room noise" (rms 0.028 ≥ the 0.02 rms threshold, peak
0.04 < the 0.08 peak threshold — the AND-gate's job): **67.6 s of
accumulated bot speech with the mic open produced zero gate fires**, and
both long replies ran to completion (`agent_decisions` 398/399 =
`spoken/replied`). Positive control on the next reply: full voice
(gain 0.5) fired the gate **34 ms** after onset (`rms=0.357,
peak=0.500`), cut local playback, and the server stop terminalized the
turn as `no_reply(barge_in)` (decision 400, INV-1 clean). Console: the
single intentional `[barge-in]` breadcrumb and nothing else.

**Phase-1 verdict.** Hot-path knobs: shipped and measured (−157.6 ms
controlled felt p50; −159 ms deterministic commit shift on the local
stack). Cold start: solved (−2.3 s, first turn now the fastest).
Turn integrity and barge-in safety: regression-clean. The remaining
felt-latency owners on the local stack are the router and answer LLM —
Phase 2 (streaming STT) and Phase 3 (router triage) pick up from here,
with Johnny-dny (answer streaming) still the measured #1.

## Phase 2 — Parakeet cache-aware streaming STT (Johnny-trt.12, 2026-06-11)

The MLX sidecar's batch POST is gone from the hot path: the Parakeet
provider on the `mlx-sidecar` runtime now relays session audio over the
sidecar's `WS /transcribe_stream` and re-emits its events — interim
transcripts while the speaker talks (480 ms incremental decodes,
`parakeet-mlx transcribe_stream(context_size=(256,256), depth=2)`, ~170 ms
p50 per decode) plus a final per utterance, endpointed **sidecar-side** at
360 ms of trailing silence (RMS tracking + a 200 ms pre-flush decode so
the final is just a result-read, measured 11–98 ms `final_ms` live).
Because 360 ms sits below the session-VAD floors (0.40 s browser / 0.50 s
rooms), the final transcript already exists when Silero's END_OF_SPEECH
fires — the turn-commit `max(VAD floor, endpointing min_delay, STT-final)`
is now VAD-bound instead of STT-bound. The adapter layer drives this
runtime directly (no VAD-buffered StreamAdapter); `in-container` /
`coreml-sidecar` stay batch via the provider's `batch_only` property, and
`JOHNNY_PARAKEET_FORCE_BATCH=1` pins the old batch path as an escape
hatch.

**Harness A/B (local stack: Parakeet MLX + Ollama llama3.2:3b + Piper;
12-turn runs, counterbalanced, `ollama stop` between runs; new
`stt_final_after_vad_end_ms` wall metric = user FINAL transcript arrival
minus the VAD speaking→listening edge):**

| arm | n (turns) | stt_final_after_vad_end p50 | p95 | range |
| --- | --- | --- | --- | --- |
| batch (forced)  | 24 | **+140 ms** | +200 ms | +106 … +270 ms |
| streaming       | 36 | **−99 ms**  | −64 ms  | −147 … −43 ms |

Every streaming final landed *before* the VAD commit (negative delta) —
the Phase-2 acceptance bar "STT-final ≤ 100 ms after VAD end" is met
with ~200 ms to spare, and ~140 ms of serial STT tail is removed from
every local turn. Felt first-audio on this stack is router/LLM-bound
(±100–150 ms p50 run noise — see the Phase-1 attribution), so the felt
re-measure belongs to the trt.15 capstone.

**Quality (real-model, real-time-paced 75 s of the trt.5 hesitation
fixture over the live WS):** word-level parity with batch — all complete
clauses identical; the 0.035 token-WER vs batch is segment-boundary
punctuation plus a clip-edge truncation artifact. Interim cadence: 3–6
interims per 1.5–2.2 s utterance (~2 Hz while speaking; interims emit
only when text *changes*, so hesitation gaps produce no spurious events).
400 ms decode chunks were trialled for cadence margin and rejected: more
encode boundaries produced word slips ("do ye know") that 480 ms runs
did not. Artifacts: `.validation/Johnny-trt.12/` (spike matrix, three WS
validation runs, A/B harness JSONs).

Live verification: playground session 86 (chrome-devtools, fake-mic
fixture) ran the streaming path end-to-end — one long-lived WS per
session, per-utterance `stream.segment` lines in the sidecar log,
transcripts committed per turn, trt.9 client barge-in unaffected.

## Semantic turn detector (en) in-process — ✅ SHIPPED (Johnny-1qr, 2026-06-11)

The Johnny-trt.6 follow-up: the **English-only** LiveKit EOU model now runs
*inside the API process* on the roomless browser session.
`InProcessInferenceExecutor` (`johnny/agent/turn_detector.py`) implements the
SDK's `InferenceExecutor` protocol over the registered `_EUORunnerEn` — the
~3.3 s model load and each `run()` happen in worker threads, the load is
shielded so a cold first prediction's 3 s timeout cannot abort or duplicate
it, and `InProcessEnglishModel` exposes the executor kwarg the stock
`EnglishModel` hides behind `get_job_context()`. Engagement is gated at
session build on the operator's STT language normalizing to `en`
(`stt_language_from_provider_config` reads the same option keys the STT
adapter stamps per-transcript, so the build gate and the SDK's per-turn
`supports_language` gate cannot disagree); any other language — or
`JOHNNY_BROWSER_FORCE_VAD_TURNS=1` — keeps the trt.6 VAD-only retune. The
loaded runner is a process singleton: **+414 MB RSS measured in-image**
(under the bead's 500 MB abort line; multilingual stays wontfixed at
+884 MB), loaded once via the trt.8 background session warm-up, warm
`predict_end_of_turn` p50 1.7 ms. Meet/room path untouched (it keeps the
job-context `MultilingualModel`).

What it changes: the engaged session runs endpointing `{min_delay: 0.40,
max_delay: 1.5}` — the same 0.40 s Silero floor as the VAD-only path
(one shared model), but a >0.40 s pause whose accumulated transcript the
model judges **mid-thought** escalates the commit to 1.5 s, and resumed
speech cancels it entirely; pre-1qr every such pause was an unconditional
hard cut. A "complete" verdict commits at the floor — today's exact timing.
Cost on the felt path: ~+12 ms e2e p50 (24-turn stub A/B, the per-turn EOU
bounce: thread-hop + 1.7 ms inference), with hesitations ≤ 0.35 s riding
out mechanically exactly as before (VAD never fires). Live proof
(playground session 97, streaming Parakeet + Ollama llama3.2:3b + Piper,
scripted fake-mic WAV, 7 utterances → exactly 9 INV-1-clean decisions):
two utterances with an incomplete lead phrase + 0.45/0.55 s mid-sentence
pause each landed as **one** decision carrying both transcript segments
(the model held the commit through the pause — `max_delay` escalation is
unreachable under `turn_detection="vad"`, so the holds are themselves proof
of engagement), while a complete-sentence + 0.5 s pause control correctly
split in two, and the trt.5 hesitation fixtures (0.20–0.35 s) stayed
single turns.

**A 0.20 s floor drop ("commit earlier when confident") was built and
reverted inside this bead's validation.** With `min_silence` 0.20 +
`min_delay` 0.20 the stub harness measured felt p50 798 → 610 ms (−188 ms;
`vad_end` 402 → 202 ms) — but the live varied-pause run hit the bead's
abort criterion: the 0.35 s-edge hesitations split, because the streaming
sidecar finalizes at ~0.36 s of trailing silence and **hallucinates
terminal punctuation at segment edges** ("Jenny, can you?"), which the EOU
model correctly reads as a complete utterance and commits. On the local
stack the drop was worth only ~30 ms anyway (commits become bound by the
sidecar's ~0.36 s endpoint); the −188 ms stands as the fast-finals upper
bound (stub 80 ms finals ≈ cloud STT). Re-attempting the floor drop
requires fixing the sidecar's edge-punctuation artifact first (and a
sub-0.36 s finalize would then also be needed for the full win). All runs,
verdict probes, fixtures and DB dumps: `.validation/Johnny-1qr/`.

## Phase-2 capstone — re-measured with streaming STT active (Johnny-trt.15, 2026-06-11)

Phase 2 of the Johnny-trt epic put streaming STT into the voice pipeline
and made it visible: Parakeet cache-aware streaming over the sidecar WS
(trt.12), live user captions (trt.13), live bot-reply captions (trt.39),
the Deepgram voice-path verification + all-cloud baseline + Anthropic
structured-output fix (trt.14), and the playground stale-state reset
(trt.40). This section is the phase gate: four 24-turn `--providers
local --prewarm` harness runs, ABBA-counterbalanced (A = Phase-1 shape,
`JOHNNY_PARAKEET_FORCE_BATCH=1` pinning the batch path; B = Phase-2
streaming), Ollama unloaded before every run, same trio as the Phase-0/1
capstones (Parakeet MLX + Ollama `llama3.2:3b` + Piper persistent
`en_US-hfc_male-medium`). One symmetric drift vs the Phase-1 capstone
day: the Johnny-1qr semantic EOU (shipped in between) was engaged in
**both** arms (`semantic-eou(en)`, the production browser default,
~+12 ms e2e). All artifacts under `.validation/Johnny-trt.15/`.

**The Phase-2 acceptance bar — STT-final ≤ 100 ms after VAD end on the
local stack — is met with margin.** `stt_final_after_vad_end_ms` over
all warm turns (n=46 per arm, replies and declines alike):

| arm | p50 | p95 | worst |
| --- | --- | --- | ----- |
| batch (forced, Phase-1 shape) | +136 ms | +193 ms | +210 ms |
| streaming (Phase-2)           | **−109 ms** | **−69 ms** | −46 ms warm / +44 ms cold |

Every warm streaming final landed *before* the VAD commit; the worst
final anywhere in 48 streaming turns was the one cold turn at +44 ms.

**Pooled warm replied per-turn stages (A n=28, B n=33).** The streaming
path has no LiveKit STT metric rows (trt.14 methodology), so the commit
and router rows are derived per turn: `commit_wait = vad_end + max(0,
stt_final_after_vad_end)`, `router+gate residual = first_audio −
commit_wait − llm_total − tts_ttfb` (the A-arm residual reproduces its
directly-measured `router_ms` 1063/1390 — the derivation cross-checks):

| stage (warm p50/p95, ms)  | A batch | B streaming | delta |
| ------------------------- | ------------- | -------------- | ----- |
| vad_end                   | 416 / 442     | 405 / 430      | flat (same 0.40 s floor) |
| stt_final_after_vad_end   | +138 / +192   | −103 / −61     | **−242 / −253, deterministic** |
| commit_wait (speech-end → turn commit) | 556 / 611 | 405 / 430 | **−151 / −181, deterministic** |
| llm_ttft                  | 536 / 695     | 559 / 814      | +24 / +118 (run noise) |
| router + gate residual    | 1063 / 1390   | 1096 / 1533    | +32 / +142 (run noise) |
| tts_ttfb                  | 78 / 159      | 79 / 170       | flat |
| first_audio_wall (felt)   | 2164 / 2739   | 2241 / 2711    | +77 / −28 (masked, see below) |
| **cold turn felt (per run)** | **2100 / 1532** | **1961 / 1423** | **−124 ms mean** |

The turn commit is now **VAD-bound**: commit_wait in the streaming arm
equals `vad_end` exactly, and the arms do not overlap at all (A
504–617 ms, B 373–439 ms across every turn of every run). That is the
~150 ms of serial batch-STT tail removed from every local turn —
deterministically, matched-fixture median −145 ms (spread −191..−98,
all 18 matched fixtures moved). The pooled felt total is statistically
flat for exactly the Phase-1-capstone reason: post-commit router+LLM
drift between identical-config runs (±100–150 ms p50, plus
context-growth — the B arms replied 33 warm turns vs A's 28, carrying
more chat history at every later fixture) exceeds the commit win;
matched-fixture felt median +21 ms confirms the cancellation. Felt e2e
vs the Phase-1 capstone's recorded numbers: 2276/2902 → 2241/2711
(−35 p50 / −191 p95, inside the noise floor). The honest phase-over-
phase claim is the deterministic one: **speech-end → turn-commit p50
556 → 405 ms (−151 ms); the felt total remains router/LLM-owned —
Phase-3 territory.** The remaining commit-wait lever is the 0.40 s VAD
floor itself (the 1qr 0.20 s drop attempt is blocked on the sidecar
edge-punctuation artifact, documented above).

**Manual playground sniff (session #105, chrome-devtools, fake-mic
WAV, streaming trio + semantic EOU).** The trt.13 "You · partial"
caption grew at the ~480 ms sidecar decode cadence through multiple
hypotheses per utterance and was replaced in place by each final
(untampered 120 ms DOM-sampling trace); trt.39 bot bubbles reconciled
on completed replies, and an interrupted reply's bubble cleared with
zero ghost text. `agent_decisions`: 10 decisions, 0 missing terminals
(INV-1 clean) — 7 replied, 2 barge-in suppressions (the WAV's short
gaps, by design), 1 reasoned decline; 19 transcript rows, finals only.
The trt.12 turn-semantics change showed up exactly as documented:
lead-in fragments split at effective gaps in [0.40, 0.52) s that batch
STT's slow tail used to re-glue, while the semantic EOU glued two
think-aloud fragments ("Johnny, let me think." + the question) into
single decisions. Console: zero errors/warnings.

**Phase-2 verdict.** Streaming STT: shipped and measured — the STT tail
is gone from the hot path (final precedes the commit by ~100 ms instead
of trailing it by ~140 ms), the acceptance bar met with ~150 ms of
margin on warm turns, and the pipeline is now visibly streaming in the
UI (live captions both directions). Felt p50 on the local stack is
unchanged within noise because the two LLM calls own the post-commit
budget — the same attribution as Phase 1, now with the commit stage at
its floor. Phase 3 (router triage) and Johnny-dny (answer streaming)
own the next felt-latency win.

## Phase-3 capstone — triage-vocabulary cost measured (Johnny-trt.21, 2026-06-11)

Two 24-turn `--providers local --prewarm` harness runs on the integrated
Phase-3 branch (same canonical trio + method as the Phase-2 capstone;
`ollama stop` before each; pooled warm replied n=44 vs Phase-2's n=33,
analysis in `.validation/Johnny-trt.21/`):

| stage (p50/p95, ms) | Phase-2 capstone | Phase-3 branch | delta |
|---|---|---|---|
| commit_wait (speech-end → commit) | 405.0 / 429.5 | 404.8 / 442.8 | flat — Phase-2 win retained |
| stt_final_after_vad_end | −103.4 / −61.3 | −106.8 / −73.3 | flat (every warm final pre-commit) |
| **router stage** (residual ≈ direct `triage_ms` ±30) | **1095.5 / 1532.8** | **1663.9 / 2198.4** | **+568 / +666** |
| answer llm_total | 559.0 / 814.6 | 660.5 / 922.0 | +101 / +107 (ctx growth — branch replies more: 44/46 vs 33/46) |
| tts_ttfb | 79.0 / 170.2 | 87.0 / 181.4 | flat |
| felt e2e | 2240.9 / 2710.5 | 2749.2 / 3541.2 | +508 / +831 (≈ the triage delta) |

**Attribution.** Per-call, not context: at turns 2–6 (fresh session) Phase-2
residuals were 775–995 ms vs 1192–1606 ms on this branch. The harness session
wires no TaskCoordinator, so the task-catalog prompt text is ruled out — the
delta is the Phase-3 **router schema growth** (trt.16 `action` + `task
{kind, args, ack}`, trt.53 ack-required + restraint descriptions) riding every
call as `response_format` on the 3B router. The gate's own code adds nothing
measurable (timed span unchanged; shadow scorer before it, transcript window
after it; residual ≈ direct `triage_ms` cross-check). Matched-fixture median
+564 ms over 21 fixtures.

**Delegated-turn felt shape (live playground, session #27).** Ack first audio
= commit +2989 ms ≈ speech-end +3.4 s: triage 2731 ms (91 %) + say dispatch
~28 ms + TTS TTFA 230 ms, zero `answer_llm` hop — the Phase-3 structural claim
holds exactly; the whole felt budget is the triage call. The levers are the
triage term itself: heuristic fast-path (Johnny-trt.51), per-agent small triage
model slots (trt.41/42), schema-payload slimming (follow-up bead from this
capstone).

## Optimization candidates — in priority order (re-ranked from the 2026-06-10 baseline)

1. **True answer-LLM token streaming** (Johnny-dny) — the measured #1.
   `openai_compatible_llm` never overrides `stream_chat`, so the base
   fallback buffers the full completion: answer TTFT == total = 3.0 s
   p50 / 4.1 s p95. Until this lands, TTS cannot start before the whole
   answer exists, and no other optimisation can rescue the felt number.
2. **Router cost + context growth** (Johnny-trt Phase 3 triage) — the
   measured #2. The derived router+gate gap is ~1.1–1.5 s on early
   turns and grows past 4–5 s after ~25 turns of accumulated chat
   context (llama3.2:3b prompt processing scales with context). Triage
   schema + history windowing both attack this.
3. **Persistent piper process** — ✅ **SHIPPED** (Johnny-1ge epic) and
   now measured in real turns: per-turn first-audio drops 795 ms p50 vs
   the `subprocess` runtime (see the before/after table above). Because
   piper-tts 1.x has no streaming CLI, the warm path is an in-process
   `PiperVoice` cache rather than a literal long-lived child process —
   same net effect. Pick it in Settings → Providers → Local Piper →
   Runtime. See [TTS_RUNTIMES.md](TTS_RUNTIMES.md).
4. **Smaller piper chunk_bytes** — ✅ default changed 4096 → 1024
   (Johnny-trt.7), and the measurement **falsified the premise**: no
   TTFB effect on this stack. Subprocess runtime: the piper CLI
   delivers its PCM in one burst after spawn + ONNX load + synth
   (`total − ttfa` ≈ 4 ms), so the read size never gates the first
   byte — isolated A/B TTFB p50 884.4 ms (4096) vs 882.1 ms (1024),
   delta 2.3 ms over 10 interleaved spawns; an 8-turn harness A/B
   agrees (tts_ttfb p50 807 vs 822 ms — run noise; artifacts under
   `.validation/Johnny-trt.7/`). Persistent runtime: `chunk_bytes` is
   not on its first-byte path at all (the library's own chunks stream
   through). The smaller default still ships finer first-frame
   granularity (2 972 B → 744 B at 16 kHz, ~93 → ~23 ms of audio per
   frame) for downstream pacing; rows saved with an explicit 4096
   keep their stored value.
5. **Keep Ollama warm** — ✅ the session-start half **SHIPPED**
   (Johnny-trt.8, the provider-prewarm section above): every session
   start now pings the LLM (and pre-loads whisper/Piper) concurrently
   with setup, taking the measured first-turn e2e from 3903 ms to
   1007 ms. Still on the operator: set `OLLAMA_KEEP_ALIVE=24h` on the
   Ollama host so the model is not evicted again after 5 idle minutes
   (mid-session idle gaps re-pay 1–5 s of GGUF load where the prewarm
   can't help).
6. **Audio bridge buffering** — ✅ audited (Johnny-trt.10) and a
   **real buffer found**: the Python layers forward frame-at-a-time
   (LiveKit downlink queue → `MeetRoomBridge` pump → per-frame
   write+flush into pacat, `bufsize=0`), but the **pacat/PulseAudio
   stage buffers seconds** — `_spawn_playback_process` passes no
   `--latency` flags, so the stream gets PA default attrs
   (prebuf ≈ tlength ≈ 2 s at 16 kHz mono) and the null sink runs at
   ~1.94 s sink latency. Measured in the meet-worker image with the
   real `MeetAudioBridge` (harness under `.validation/Johnny-trt.10/`):
   utterance onset **3.9 s** after the bridge writes it (bursty shape;
   prebuf re-arms after every silent gap, and a 300 ms utterance never
   plays until more audio arrives), **3.7–3.8 s standing** when the
   downlink stream is continuous. With
   `--latency-msec=50 --process-time-msec=10`: **57 ms** onsets in the
   bursty shape (sink latency 1.94 s → 11.6 ms); the continuous shape
   still locks in a 0.5–1.0 s start transient, so the fix pairs the
   flags with bridge-side silence-gating of downlink writes. This is
   the dominant Meet-surface latency cost → fix bead **Johnny-dkj**
   (P1). Applies to the legacy unified S2S in-worker path too (same
   bridge).

## What this work shipped (Johnny-ckz.8 iteration + Johnny-cxu baseline)

- `ProviderTip` dataclass on `ProviderSchema` and `tips` field
  populated on every provider adapter (`backend/app/providers/*.py`).
- Frontend renders tips in a dedicated "Latency & tuning tips"
  section inside the providers modal (`frontend/src/routes/providers/+page.svelte`).
- Test suite pins the schema serialisation and asserts every adapter
  ships tips with non-empty topic + body (`backend/tests/providers/test_schema.py`).
- This document, codifying targets and methodology.
- **The measured baseline above** (Johnny-cxu, 2026-06-10): real
  20+-turn p50/p95 per stage on the configured local stack, before/after
  persistent piper, bottleneck attribution with traces, and "Measured on
  this machine" tips on the Parakeet / OpenAI-compatible / Piper provider
  cards. Capture method: the scripted fake-mic run documented under
  "How to measure on this machine".

## Still open — tracked separately

- ✅ One-command scripted latency harness replacing the manual fake-mic
  drive — **SHIPPED** (Johnny-trt.1, sanity-gated against this baseline;
  see "One-command scripted harness" above).
- True answer-LLM token streaming for openai-compatible — Johnny-dny
  (the measured #1 bottleneck).
- STT timing-row turn attribution off-by-one — Johnny-5vb.
- ✅ Streaming STT (partials) — **SHIPPED** (Johnny-trt Phase 2:
  trt.12 Parakeet streaming + trt.13/trt.39 live captions; capstone
  re-measure in the Phase-2 capstone section above). The
  `in-container` / `coreml-sidecar` Parakeet runtimes remain batch-only
  (documented follow-up in `.validation/Johnny-trt.12/00-decision-note.md`).
