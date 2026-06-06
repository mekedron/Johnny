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
