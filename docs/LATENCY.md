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
| All-local (faster-whisper + Ollama + Piper)        | 300 ms     | 500 ms     |
| Mixed local STT/TTS + cloud LLM                    | 250 ms     | 450 ms     |
| All-cloud (Deepgram + OpenAI + ElevenLabs Flash)   | 200 ms     | 400 ms     |

Why 300 ms / 500 ms for the local stack: research on conversation turn-
taking (Stivers et al.) puts cross-cultural human response gaps at
roughly 200 ms, with a long tail past 500 ms reading as "they're
thinking". Aim under 500 ms and the bot feels alive; over 1 s and it
feels broken.

**Measured TTS contribution (updated after the Johnny-1ge runtime epic).**
The TTS first-byte component of the local budget is now ~40 ms warm with
the **Piper `persistent-subprocess`** runtime (was ~200–400 ms cold spawn
*every* turn before the runtime split). So on the all-local stack, TTS is
no longer a dominant cost — STT and LLM first-token now own the budget.
Per-runtime measured warm numbers: Piper persistent ~40 ms · Piper
http-sidecar ~90 ms · Kokoro http-sidecar ~425 ms · KittenTTS http-sidecar
~1.8 s (atomic synth). Full methodology + cold numbers + the other
providers/runtimes live in
[TTS_RUNTIMES.md](TTS_RUNTIMES.md#3-comparison-table).

## Latency map — the stages we measure

Each turn passes through these stages. The total is the only number
the user feels, but the breakdown is where bottleneck attribution
lives.

```
VAD end-of-speech  ──┐
                     │  end_of_speech_ms (default 800 ms; bakes in
                     │  the wait by design — see DEFAULT_END_OF_SPEECH_MS
                     │  comment in pipeline.py)
STT first-partial  ──┤
STT final          ──┤  Whisper batch: 100-400 ms CPU, 30-100 ms GPU
LLM first-token    ──┤  Router LLM. Hot path.
LLM total          ──┤  Answer LLM. Hot for spoken answer.
TTS first-byte     ──┤  Local default (Piper persistent): ~40 ms warm
                     │  (was ~200-400 ms cold spawn EVERY turn before the
                     │  runtime split shipped). + ~93 ms chunk buffer at
                     │  4096 bytes / 22050 Hz. See docs/TTS_RUNTIMES.md.
First audio frame  ──┘  to transport
```

The 800 ms `end_of_speech_ms` is intentional padding for natural
mid-sentence pauses (see Johnny-arh). Don't try to optimise it away —
the user-felt budget is **from** speech-end, not from when the last
voiced frame arrived. What we have to optimise is everything **after**
the VAD says "done".

## How to measure on this machine

### Quick sniff test (no instrumentation)

1. Open `/playground` in the browser.
2. Start a session with the active providers (Local Whisper, Ollama,
   Local Piper as of writing).
3. Say one short phrase. Use the **bot icon ping** — the time between
   the transcript appearing and the first audio byte playing is your
   real-world time-to-first-audio. Eyeball five or ten turns of varied
   length.
4. Watch `docker compose logs -f worker` while talking — the
   per-utterance logs include the STT model size, classifier timeout
   firings, and noise-filter drops. Anything unexpected here is the
   first place to look.

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

## Bottlenecks — where the time actually goes

On the **all-local** stack on a Mac M-series with 16 GB RAM, measured
informally during this work:

- **Piper spawn-per-utterance** *used to be* the largest single cost —
  200–400 ms of ONNX startup before any synthesis, paid every turn.
  The **TTS runtime split** (the Johnny-1ge epic) fixed this: the
  `persistent-subprocess` runtime keeps the voice's ONNX session warm
  in-process, so the second and later turns return first audio in
  ~40 ms. The old per-turn cold spawn now only happens on the
  `subprocess` runtime (kept as a debug baseline). Measured local-TTS
  first-byte by runtime, on an M-series Mac (16 GB), 67-char sample
  phrase, median of 3, read from the `X-TTS-TTFA-Ms` header / the
  Play sample badge / the `*.synth: ... ttfa_ms=` log line:

  | Provider · runtime | TTFA cold | TTFA warm |
  | --- | --- | --- |
  | Piper · `subprocess` | ~930 ms | ~930 ms (no warm state) |
  | Piper · `persistent-subprocess` | ~930 ms (incl. ~700 ms load) | **~40 ms** |
  | Piper · `http-sidecar` | ~90 ms + first-load | ~90 ms |
  | Kokoro · `http-sidecar` | ~1.7 s | ~425 ms |
  | KittenTTS · `http-sidecar` | ~1.8 s | ~1.8 s (atomic synth) |

  Full table (footprint, install complexity, when-to-pick, the
  in-container cells) is in
  [TTS_RUNTIMES.md](TTS_RUNTIMES.md#3-comparison-table).
- **Ollama first-token** on Qwen 8B Q4 is ~200–400 ms warm; on a 35B
  Q4_K_M model it climbs to 1.5–3 s, and that's the entire user-felt
  budget gone before TTS has a chance.
- **Whisper base** transcribes a 3-second utterance in ~150 ms; small
  takes ~350 ms. tiny is ~80 ms but quality drops noticeably for
  meetings. For the live router, base is the sweet spot.

On the **all-cloud** stack, latency is dominated by network RTT plus
first-byte time of each provider. EU/US round-trips of 30–80 ms
make a meaningful difference; pick the closest region you can.

## In-UI tips on every provider settings page

Each provider's settings modal at `/providers` now ships a **Latency
& tuning tips** section (see `/providers/+page.svelte` and the
`tips` field on each adapter's `field_schema()`). Tips lead with a
concrete number where possible, name the knob the tip applies to,
and explicitly say what the trade-off is. To add or revise a tip,
edit `tips=(...)` in the adapter's `field_schema()` classmethod and
rebuild the api image (the api is COPY-baked; see the codebase
patterns in `.ralph-tui/progress.md`).

## Optimization candidates — in priority order

1. **Persistent piper process** — ✅ **SHIPPED** (Johnny-1ge epic). This
   was the biggest single win for the local stack and it landed: the
   `persistent-subprocess` runtime keeps the voice warm and drops
   per-turn TTFA from ~200–400 ms cold spawn to ~40 ms warm. Because
   piper-tts 1.x has no streaming CLI, the warm path is an in-process
   `PiperVoice` cache rather than a literal long-lived child process —
   same net effect. Pick it in Settings → Providers → Local Piper →
   Runtime. See [TTS_RUNTIMES.md](TTS_RUNTIMES.md).
2. **Smaller piper chunk_bytes** — already documented in the in-UI
   tip. At 22050 Hz the default 4096 bytes is ~93 ms of head-of-line
   delay; 1024 cuts it to ~23 ms at the cost of more syscalls.
3. **Whisper on GPU** — when CUDA is available, faster-whisper drops
   to ~30 ms p50 on small models. Detection logic + auto compute_type
   switching is a separate ticket.
4. **Keep Ollama warm** — set `OLLAMA_KEEP_ALIVE=24h` on the Ollama
   host so the first call after idle doesn't pay 1–5 s of GGUF load.
5. **Audio bridge buffering** — verify the meet-worker bridge does
   not buffer TTS chunks before forwarding. The pipeline yields
   frames as they arrive; the bridge must too. Audit, then file
   if there is a real buffer.

## What this work shipped (Johnny-ckz.8 iteration)

- `ProviderTip` dataclass on `ProviderSchema` and `tips` field
  populated on every provider adapter (`backend/app/providers/*.py`).
- Frontend renders tips in a dedicated "Latency & tuning tips"
  section inside the providers modal (`frontend/src/routes/providers/+page.svelte`).
- Test suite pins the schema serialisation and asserts every adapter
  ships tips with non-empty topic + body (`backend/tests/providers/test_schema.py`).
- This document, codifying targets and methodology.

## What this work explicitly did not ship — tracked separately

- Real 20-turn p50/p95 measurement on the configured local providers —
  needs the activity-log instrument (Johnny-ckz.7) for cheap, repeatable
  capture. Filed as a follow-up bead.
- Persistent piper process — the headline optimisation, deserving its
  own scoped ticket.
- Whisper GPU detection / CPU-thread tuning — secondary optimisation,
  filed.
- "Measured on this machine" block reflecting live numbers per
  provider — needs Johnny-ckz.7's per-turn events to populate
  honestly, otherwise the numbers go stale. Filed as a follow-up.
