# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Pipeline event additions
New `johnny.voice_pipeline.events.*` events flow through Redis pub/sub via
`EventBus.publish(event)` and the WS layer (`app/api/ws.py`). Unmapped event
`type` strings pass through to the wire unchanged — frontend WS handler
branches on `event.type` and just ignores unknown types, so adding a new
event type is fully additive (no frontend change required for the event
to be safely delivered). Wire renames live in `WIRE_TYPE_MAP`.

### PipelineConfig knobs
`PipelineConfig` is a frozen dataclass. New per-session knobs go there
with module-level `DEFAULT_<name>` constants exporting via
`johnny.voice_pipeline.pipeline.__all__` and the package `__init__.py`
`__all__`. Tests pin the defaults via `PipelineConfig().attr == DEFAULT_<name>`
so a future refactor can't silently flip behaviour.

### Docker `johnny-api-1` mounts
The api container does NOT bind-mount the backend source — the image bakes
`backend/` in at build time. To validate runtime changes via
chrome-devtools without a full rebuild, `docker cp` the changed files into
`/workspace/...` and `docker restart johnny-api-1`. Verify the new code is
live with `docker exec johnny-api-1 grep -c <new-symbol> <file>`.

### Transport-level interrupt contract
The `VoicePipeline.interrupt()` event flag is checked between LLM deltas
and TTS frames, but transports with deep buffers (browser-WebRTC has
audio in server playback queue + WS send buffer + browser AudioContext
scheduler) need a second hook to actually cut audio the user already
hears. `JohnnyTransport.cancel_playback()` is the contract: default
no-op for `LocalAudioTransport` (PulseAudio holds ≤20 ms so the
generator's `aclose()` is already a tight cut). `BrowserAudioTransport`
overrides it to drain `_playback_q` synchronously and emit an
`{"type":"interrupt"}` control message on a separate `_control_q` that
the WS sender forwards alongside PCM. `VoicePipeline.interrupt()` calls
this hook so fast-barge-in / barge-in classifier / Stop-button all share
one cut path.

### Frontend `cancelScheduledPlayback`
TTS frames are scheduled into the future via
`AudioBufferSourceNode.start(nextPlaybackTime)`. Even after the WS stops
delivering frames, already-scheduled buffers keep playing for as long as
the schedule cursor extends past `currentTime`. Stopping all
`activeOutputs` source nodes + resetting `nextPlaybackTime` cuts the
tail. Called both locally on `requestInterrupt()` (Stop button — fastest
path, sub-frame cut) and on receipt of the server's
`{"type":"interrupt"}` control message (voice barge-in path).

---

## 2026-06-06 - Johnny-ckz.14: STT noise gate before router LLM

### What was implemented
Layered noise filter between STT and the router so ghost turns
('you', 'uh', 'hm', '............') never reach the LLM. Five
defence layers, all per-session-configurable on `PipelineConfig`:

1. **Audio-too-short gate** (`noise_filter_min_audio_ms`, default 250 ms)
   — VAD-cut bursts shorter than the floor never reach STT. Skipping
   the STT round-trip is the leverage point — a noisy mic burns provider
   budget without it.
2. **Empty / punctuation-only gate** — strings that strip to nothing or
   match `^[\\s\\W_]+$` (catches "............", "...", "??", "…").
3. **Minimum-char gate** (`noise_filter_min_chars`, default 2) — single
   letters can never be meaningful intent. The floor stays at 2 so 'no'
   still passes.
4. **Stoplist gate** (`noise_filter_stoplist`, default catches `you`,
   `uh`, `um`, `hm`, `mm`, `ah`, `eh`, `oh`, `thank you`,
   `thanks for watching`, `subtitles by the amara.org community`).
   Match is case-insensitive after stripping outer punctuation, so
   `" Uh. "` matches `uh`. Ambiguous short words ('thanks', 'bye',
   'okay then') are deliberately omitted — they're real human turns.
5. **Confidence floor** (`noise_filter_min_confidence`, default 0
   = disabled). Per-provider tunable once each STT's confidence
   distribution is calibrated.

When the gate fires, a new `TranscriptFiltered` event is published on
the session bus (and persisted to Redis via `RedisEventBus`) carrying
text/reason/speaker/confidence/audio_duration_ms so the activity log
(Johnny-ckz.7) can render dropped turns and the stoplist can be tuned
by operators.

Master `noise_filter_enabled` switch defaults `True`. Tests that pin
pre-Johnny-ckz.14 behaviour flip it off explicitly.

### Files changed
- `backend/johnny/voice_pipeline/pipeline.py` — constants
  (`DEFAULT_NOISE_FILTER_*`, `DEFAULT_NOISE_STOPLIST`,
  `_PUNCTUATION_STRIP_CHARS`, `_PUNCTUATION_ONLY_RE`), `PipelineConfig`
  fields, `VoicePipeline._is_audio_below_noise_floor`,
  `VoicePipeline._classify_transcript_as_noise`,
  `VoicePipeline._publish_noise_filtered`, integration in
  `_transcribe_and_emit`.
- `backend/johnny/voice_pipeline/events.py` — `TranscriptFiltered`,
  `TranscriptFilteredEventType`, `TranscriptFilteredReason`.
- `backend/johnny/voice_pipeline/__init__.py` — re-exports.
- `backend/tests/voice_pipeline/test_pipeline.py` — 9 new tests
  (parametrised stoplist coverage, regression for `yes`/`no`/`okay`,
  disabled flag, low-confidence drop, audio-too-short skip,
  audio_duration_ms carried on post-STT events, default stoplist
  omits / contains specific tokens, `PipelineConfig` defaults).
- `backend/tests/voice_pipeline/test_events.py` — 5 new tests for
  `TranscriptFiltered` (defaults, full payload, frozen, dict
  serialisation, reason Literal contract).
- `backend/tests/voice_pipeline/test_pipeline.py` — fixed 4 prior
  tests that relied on single-char placeholder transcripts ('a',
  'b', 'c', 'd') by either switching to multi-char (`hello team`,
  `alpha`/`bravo`/...) or explicitly turning the gate off in tests
  whose subject was an unrelated path (fast-barge-in log line).

### Validation
- Unit tests: 19 new pass; 1979 total tests pass, 12 skipped.
- mypy: clean on changed files.
- ruff: only one pre-existing UP041 unrelated to my changes.
- Container runtime: imported `DEFAULT_NOISE_STOPLIST`,
  `PipelineConfig().noise_filter_enabled == True`, and ran a manual
  classifier sweep against the bead's exact examples — every noise
  case dropped with the right reason, every legitimate short reply
  passed.
- chrome-devtools MCP: playground loaded with new code, fresh
  session #249 reached `joined`, typed `yes` produced a
  `transcript_finalized` row (real-word regression check passes),
  zero console errors / warnings, screenshot at
  `.validation-ckz14-artifacts/playground-after-noise-gate.png`.

### Learnings
- **Whisper hallucination catalogue.** The specific tokens Whisper
  emits during silence are well-documented (`you`, `uh`, `thanks for
  watching`, `subtitles by the amara.org community`). These belong in
  the default stoplist; anything that could plausibly be a real one-
  word reply (`thanks`, `bye`, `okay`) must NOT be — the AC explicitly
  forbids over-filtering them. A regression test (`test_default_noise_
  stoplist_omits_legit_short_replies`) pins the contract.
- **Test-fake transcripts can collide with new gates.** Existing pipeline
  tests freely used `['a', 'b']` as filler STT output. After the
  gate landed, those single-char strings hit the `too_short` path and
  silently dropped — pre-existing tests that asserted on downstream
  effects (router calls, decision rows) failed. Two fix patterns are
  valid: (a) make the test transcripts realistic ('hello team') when
  the test is conceptually about a real turn, (b) explicitly
  `noise_filter_enabled=False` when the test is about an unrelated
  code path. Pattern (b) reads as documentation of intent — "this
  test does not care about the gate" — which made the diff easier to
  review.
- **`feed_text` deliberately bypasses the gate.** Typed input is
  intentional; we never want to drop a user typing `uh` literally.
  The gate lives only on the STT → router edge.

---

## 2026-06-06 - Johnny-ckz.13: Playground voice barge-in + visible Stop button

### What was implemented
A two-layer interrupt fix for the in-browser voice surface — the
`pipeline.interrupt()` event flag is no longer enough because TTS audio
is buffered in three places (server playback queue → WS send buffer →
browser `AudioContext` scheduler), so the user keeps hearing the bot for
hundreds of ms after the pipeline has already given up on the response.

Backend (`johnny.voice_pipeline`):
1. **`JohnnyTransport.cancel_playback()`** — new method on the abstract
   base class, default no-op. Lets the pipeline ask any transport to
   flush its outbound buffer. `LocalAudioTransport` (meet-worker /
   PulseAudio) keeps the no-op because its mixer holds ≤20 ms and
   `aclose()` on the TTS generator is already a tight enough cut.
2. **`BrowserAudioTransport.cancel_playback()`** — drains `_playback_q`
   synchronously (so frames not yet handed to the WS never reach the
   browser) and pushes `{"type":"interrupt","seq":N}` onto a new
   `_control_q` that the WS sender forwards alongside PCM via
   `drain_control_messages()`. Idempotent; sequence counter is exposed
   for tests.
3. **`VoicePipeline.interrupt()`** — now calls
   `self.transport.cancel_playback()` after setting the event so every
   interrupt source (fast-barge-in / barge-in classifier / Stop button)
   gets the queue drain + browser notification for free.

Backend (`app/api/browser_sessions.py`):
4. **`{"type":"stop"}` WS control message** — new client→server message
   parsed by `_handle_client_control` (refactored from the legacy
   substring match into a proper JSON parser that also still accepts
   `{"type":"end"}`). On receipt, calls `pipeline.interrupt()` (which
   chains to `transport.cancel_playback()`) plus a direct
   `transport.cancel_playback()` so the cut still works if the user
   clicks Stop before the pipeline finishes assembling.
5. **`control_sender` task** — third task running alongside `receiver`
   and `sender` on the audio WS, draining `transport.drain_control_messages()`
   and `send_json()`-ing each control frame so it arrives in band.
6. **Silent drain** when the browser disconnects also drains the control
   queue (via `asyncio.gather`) so stale interrupts can't pile up while
   nobody is listening.

Frontend (`browserAudio.ts`):
7. **`requestInterrupt()`** — new method on `BrowserAudioSession`. Calls
   `cancelScheduledPlayback()` immediately (sub-frame local cut) and
   `socket.send({"type":"stop"})` to tell the server to stop synthesising
   and drain its queue. Safe to call when nothing is playing.
8. **`cancelScheduledPlayback()`** — stops every `AudioBufferSourceNode`
   in `activeOutputs`, disconnects them, resets `nextPlaybackTime` to
   `currentTime`, and bumps an `interruptCount` for tests. Called by
   both `requestInterrupt()` (local) and the WS `onmessage` handler when
   `{"type":"interrupt"}` arrives (server).
9. **Incoming `{"type":"interrupt"}` handler** — wired in `socket.onmessage`
   alongside `ready` and `ended`.

Frontend (`+page.svelte`):
10. **Visible Stop button** — new `.interrupt` styled button in the
    controls pane (`data-testid="playground-interrupt-button"`) with a
    clear "Stop bot" label and a tooltip explaining voice barge-in is
    also available. Clicking it calls `audioSession.requestInterrupt()`
    and flips `isSpeaking=false` immediately so the state indicator
    flips to idle without waiting for the next event.

### Files changed
- `backend/johnny/voice_pipeline/transport.py` — `cancel_playback`
  default-no-op on `JohnnyTransport`.
- `backend/johnny/voice_pipeline/browser_transport.py` — `_control_q`,
  `cancel_playback`, `drain_control_messages`, `interrupt_seq` property,
  `close_playback` now closes both queues.
- `backend/johnny/voice_pipeline/pipeline.py` — `interrupt()` calls
  `transport.cancel_playback()` defensively.
- `backend/app/api/browser_sessions.py` — `_handle_client_control` (new),
  `control_sender` task in `browser_audio_socket`, silent drain
  expanded.
- `backend/tests/voice_pipeline/test_browser_transport.py` — 6 new tests
  (queue drain, control message, idempotency, queue-empty case,
  close-while-pending, future-playback still works).
- `backend/tests/voice_pipeline/test_pipeline.py` — 2 new tests
  (interrupt calls transport.cancel_playback, interrupt still works if
  transport raises).
- `backend/tests/api/test_browser_sessions.py` — 4 new tests (stop
  control with pipeline / without pipeline, end control still
  disconnects, unknown control ignored).
- `frontend/src/lib/browserAudio.ts` — `requestInterrupt`,
  `cancelScheduledPlayback`, `getInterruptCount`, server-interrupt
  handler.
- `frontend/src/routes/playground/+page.svelte` — Stop button +
  `interruptBot()` handler + `.interrupt` styles.

### Validation
- **Unit tests**: 17 new pass (6 transport, 2 pipeline, 4 WS handler,
  +5 prior). All 328 voice_pipeline tests pass; all 23 browser-session
  tests pass.
- **mypy / svelte-check**: clean on changed files.
- **chrome-devtools MCP, 12 measured Stop-button cut runs** against a
  live playground session #262 (text-input-driven turns to bypass mic
  echo confounds):
  - Cut latency p95 (stop click → server interrupt control ack) =
    **3.9 ms** vs. 300 ms AC #3 budget.
  - Frames-after-stop p95 = 9 frames (180 ms of in-flight audio that
    `cancelScheduledPlayback` immediately drops).
  - Total frames per run p95 = 16 (≪ full reply, which would be 100s
    of frames at 20 ms each).
- Raw telemetry saved to `.validation-ckz13-artifacts/telemetry.json`.
- Screenshots in `.validation-ckz13-artifacts/`:
  `playground-with-stop-button.png` (button visible) and
  `playground-after-interrupt.png` (post-stop idle state).
- **Surface parity**: the per-event "Try with bot" surface routes via
  `/playground?session=<id>` (calendar/+page.svelte:370) so it shares
  the same component + WS code path — verified by re-attaching to
  session #259 via that URL and seeing the Stop button + new
  request/interrupt round-trip.

### Learnings
- **Three buffering layers, not one.** The mental model "interrupt =
  set a flag" only works when the cut path holds ≤1 frame of audio.
  Browser-WebRTC pipelines have audio in the server's playback queue,
  the OS TCP send buffer, the browser's WS receive buffer, AND the
  AudioContext's pre-scheduled buffer sources (each `start(nextTime)`
  schedules into the future). Each layer needs an explicit cut: queue
  drain (server), tail discard via `cancelScheduledPlayback` (browser).
  Without the latter, even an empty server queue still leaks ~150 ms of
  TTS to the user.
- **`JohnnyTransport.cancel_playback` default no-op is the right shape.**
  PulseAudio buffers ≤ 20 ms so the existing `aclose()` cut is enough.
  Forcing every transport to implement a meaningful flush would have
  forced the LocalAudioTransport to bridge into the bridge's internal
  queues for no user-visible benefit. A no-op default keeps the
  abstraction honest.
- **Idempotent interrupts let the Stop button skip the pipeline
  guard.** Calling `pipeline.interrupt()` AND `transport.cancel_playback()`
  in `_handle_client_control` (rather than one-or-the-other) means a
  Stop click that races with pipeline assembly (`runner.pipeline is None`)
  still cuts audio. The transport's interrupt-seq counter just bumps
  twice; the browser tolerates duplicate interrupts (no-op when nothing
  is playing).
- **`socket.send` runs through the patched constructor too.** When
  hooking the WebSocket in chrome-devtools telemetry, I had to wrap
  both `addEventListener('message')` and the `send` method — otherwise
  outgoing `{"type":"stop"}` is invisible to the test harness and the
  round-trip can't be measured. Easy to miss when retro-fitting
  instrumentation.

---
