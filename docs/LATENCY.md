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
Johnny-trt.1 turns this method into a one-command harness.

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

The ~60 ms warm first byte ≈ the ~93 ms `chunk_bytes` buffer at
4096 B / 22 050 Hz partially overlapped with synthesis — dropping
`chunk_bytes` to 1024 is the next TTS nibble (Johnny-trt Phase 1).
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
4. **Smaller piper chunk_bytes** — at 22050 Hz the default 4096 bytes
   is ~93 ms of head-of-line delay baked into every warm TTFB; 1024
   cuts it to ~23 ms at the cost of more syscalls (Johnny-trt Phase 1
   has the knob task).
5. **Keep Ollama warm** — set `OLLAMA_KEEP_ALIVE=24h` on the Ollama
   host so the first call after idle doesn't pay 1–5 s of GGUF load
   (the baseline runs re-warmed explicitly before each session).
6. **Audio bridge buffering** — verify the meet-worker bridge does
   not buffer TTS chunks before forwarding. The pipeline yields
   frames as they arrive; the bridge must too. Audit, then file
   if there is a real buffer (Johnny-trt Phase 1 Meet-bridge audit).

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

- One-command scripted latency harness replacing the manual fake-mic
  drive — Johnny-trt.1 (sanity-gated against this baseline).
- True answer-LLM token streaming for openai-compatible — Johnny-dny
  (the measured #1 bottleneck).
- STT timing-row turn attribution off-by-one — Johnny-5vb.
- Streaming STT (partials) — Johnny-trt Phase 2; until then "STT first
  partial" is not a measurable stage on the batch Parakeet path.
