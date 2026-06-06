# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### VoicePipeline: split transcribe vs respond loops (Johnny-har)
`VoicePipeline.run()` runs two concurrent tasks: a transcribe loop (VAD →
STT → persist) and a respond loop (router → answer LLM → TTS). They
communicate via `self._response_queue`. The transcribe loop MUST NEVER
await the respond loop, because the transport's capture queue (≈2 s)
will silently drop frames otherwise — that's the Johnny-har regression.

When building prompts inside the response loop, find the current
transcript by IDENTITY (`t is transcript`), not by position
(`_transcript_history[:-1]`): later transcripts may have been appended
while this answer was being generated, so the current transcript is no
longer guaranteed to be at the end of history. `_router_messages` uses
the `is_current` flag in `transcript_window`; `_answer_messages` walks
`self._transcript_history` to find the index.

The `_response_queue` uses `None` as the end-of-stream sentinel so the
respond loop drains queued transcripts before exiting once capture ends.

### Voice barge-in: fire-and-forget classifier + generation guard (Johnny-di9)
While the bot is in `_respond_to_transcript` (responding to a previous
transcript), each newly-finalised participant transcript spawns a
fire-and-forget barge-in classifier task in `_transcribe_and_emit`.
The classifier is the SAME `router_llm` provider invoked with a
distinct prompt + `_BARGE_IN_SCHEMA`. When the verdict is
`should_interrupt=True` AND the response generation hasn't moved on,
the task calls `pipeline.interrupt()`.

Why fire-and-forget: awaiting the classifier inline in
`_transcribe_and_emit` would reintroduce the Johnny-har regression —
the transcribe loop would be gated on a slow LLM call, dropping
capture frames. Pending classifier tasks live in
`self._barge_in_tasks` and are gathered (with cancel) at the end of
`run()` to avoid "Task was destroyed but it is pending" warnings.

Why the generation guard: `self._response_generation` increments at
the start of each `_respond_to_transcript`. Each classifier task
captures the generation at spawn time. The verdict only fires
`interrupt()` if the captured generation still equals
`_response_generation` AND `_response_in_flight` is True. Without
this guard, a delayed verdict (e.g. the LLM call takes longer than
the response it was meant to interrupt) would abort a LATER
response that the user didn't mean to cancel.

The classifier is gated on `mode in SPEAKING_MODES` AND `speak=True`:
listen_only / suggest_only don't produce audio, so `interrupt()`
would be a no-op and the LLM call would just waste budget.
`enable_barge_in=False` in `PipelineConfig` is the kill switch.

### Slow STT in barge-in tests gives the response loop room to schedule
The synchronous `_FakeSTT` is so fast that the transcribe loop
processes ALL utterances before the response loop has a chance to
pull the first one off the queue — so by the time
`_should_classify_barge_in()` is checked for any later transcript,
`_response_in_flight` is still False and no classifier fires. The
fix is a `_SlowFakeSTT` with a small (~20 ms) per-utterance sleep,
which mimics production timing and lets asyncio interleave the
respond loop in. Without this, the only timing where barge-in fires
is when the response loop is genuinely wedged in a long stage —
which doesn't naturally happen with fake providers.

---

## 2026-06-06 - Johnny-di9
- Added voice-triggered barge-in. While the bot is in flight
  (`_response_in_flight=True` and mode allows audio), each
  newly-finalised participant transcript spawns a fire-and-forget
  barge-in classifier task in `_transcribe_and_emit`. The classifier
  reuses `router_llm` with a distinct prompt + `_BARGE_IN_SCHEMA`
  and returns one of `stop` / `correct` / `new_question` /
  `side_chat` / `noise` — the first three call `pipeline.interrupt()`.
- Added a generation guard: each response increments
  `_response_generation` at start of `_respond_to_transcript`. The
  classifier task captures the generation at spawn time and only
  fires interrupt if it still matches when the verdict returns. This
  stops a delayed verdict from aborting a *later* response the user
  didn't mean to cancel.
- Added `PipelineConfig.enable_barge_in` (default True) as a kill
  switch so tests can pin pre-barge-in behaviour.
- Renamed the original `_respond_to_transcript` body to
  `_respond_to_transcript_inner` so the in-flight / generation
  bookkeeping wraps every return path of the response logic
  uniformly. `listen_only` and `speak=False` early-return BEFORE the
  bookkeeping kicks in so non-speaking modes never spawn classifiers.
- New `_SlowFakeSTT` fixture (20 ms per-utterance sleep) for
  barge-in tests, because the synchronous `_FakeSTT` is too fast for
  the response loop to interleave (see codebase pattern above).
- Added 22 new tests covering: stop/correct/new_question fire
  interrupt; side_chat/noise don't; bot idle → no classifier;
  `enable_barge_in=False` → no classifier; listen_only / suggest_only
  / speak=False → no classifier; classifier prompt contains bot
  context; classifier LLM failure leaves bot running; stale verdict
  (gen guard) drops interrupt; parse safety defaults.
- Files changed:
  - `backend/johnny/voice_pipeline/pipeline.py`
  - `backend/johnny/voice_pipeline/__init__.py`
  - `backend/tests/voice_pipeline/test_pipeline.py`
- **Learnings:**
  - The TTS interrupt loop only checks `_interrupt_event` between
    yielded frames. A TTS provider that awaits forever inside a
    single `synthesize_stream` call (no further yields) is effectively
    uninterruptible — production TTS streams 20 ms frames so it's a
    non-issue in real deployments, but test fixtures need to either
    yield continuously or be released externally for the audio-cut
    assertion to land.
  - `_parse_barge_in_response` cross-checks the `should_interrupt`
    bool against the `category` and downgrades to no-interrupt for
    `noise` / `side_chat` — a misbehaving classifier can't smuggle
    an interrupt past the safety default.
---

## 2026-06-06 - Johnny-har
- Decoupled inbound STT from the bot's speak/think pipeline state.
  `VoicePipeline.run()` now spawns two concurrent tasks: transcribe
  (VAD → STT → persist) and respond (router → answer → TTS). Transcripts
  flow through `self._response_queue`; the transcribe task NEVER awaits
  the respond task, so participant audio always reaches
  `transcript_chunks` even when the bot is mid-utterance.
- Renamed the monolithic `_process_utterance` into two methods:
  `_transcribe_and_emit(utterance)` and
  `_respond_to_transcript(transcript)`.
- Fixed `_router_messages` and `_answer_messages` to identify the
  current transcript by identity, not by `[:-1]` position — concurrent
  appends from the transcribe loop mean the current transcript is no
  longer guaranteed to be the last entry in `_transcript_history`.
- Added regression test
  `test_transcription_keeps_running_while_bot_is_speaking`: stalls the
  TTS via a test-controlled `asyncio.Event` and asserts all four
  participant transcripts reach the sink before `AgentSpoke` fires.
- Files changed:
  - `backend/johnny/voice_pipeline/pipeline.py`
  - `backend/tests/voice_pipeline/test_pipeline.py`
- **Learnings:**
  - The transport's capture queue is bounded (≈2 s for both
    PulseAudio and LiveKit) and drops the oldest frame when full — so
    any long pause in the consumer silently drops audio.
  - Existing tests that asserted exact cross-utterance event ordering
    (`assert types == [...]`) were implicitly testing the BUG behaviour
    (serialised pipeline). Updated to use `sorted(types) == sorted(...)`
    or per-event-type assertions so the new concurrent model passes
    while preserving the semantic invariants.
  - Cached `_history_summary` cutoff is index-based; with concurrent
    appends the cutoff still works because we always summarise a strict
    prefix (oldest entries), and the current transcript may be anywhere
    after that prefix.
---

