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
STT first-partial  ──┤  N/A on the batch Parakeet path (streaming STT
                     │  is Johnny-trt Phase 2)
STT final          ──┤  Parakeet MLX sidecar: ~123 ms p50 measured
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

**Semantic turn detector: investigated and wontfixed (Johnny-trt.6).**
The plan was LiveKit's multilingual EOU model in-process on the roomless
browser session (an `InferenceExecutor` shim around the registered
`_EUORunnerMultilingual`) paired with `{"min_delay": 0.2, "max_delay":
1.5}` — commit early when the model calls the utterance complete, hold
hesitant speech to 1.5 s. The spike killed it on the bead's own abort
line (RSS > ~500 MB): the multilingual ONNX is 396 MB on disk and
`initialize()` costs **+884 MB RSS** in the api process (~1.0 GB total
with imports; `enable_cpu_mem_arena=False` doesn't help — still ~1.05 GB
total). Warm inference is ~12 ms, so CPU was never the issue. Two extra
findings: the multilingual revision (v0.4.1-intl) supports 14 languages
**not including Finnish** (the per-turn `supports_language` gate would
skip it for fi configs anyway), and the EOU bounce task only runs after
VAD END_OF_SPEECH — the model cannot commit *below* the Silero floor, so
"commit at ~0.2 s" additionally requires dropping the VAD floor, which
re-segments the batch-STT StreamAdapter. The **English-only** model fits
the line (+406 MB RSS, 1.4 ms inference) and is filed as an opt-in
follow-up (Johnny-1qr). Full spike numbers:
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
All-cloud p50/p95 gets measured when the Phase-2 Deepgram verification
lands (Johnny-trt.14).

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
- Streaming STT (partials) — Johnny-trt Phase 2; until then "STT first
  partial" is not a measurable stage on the batch Parakeet path.
