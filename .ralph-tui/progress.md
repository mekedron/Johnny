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

