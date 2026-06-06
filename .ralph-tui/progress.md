# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Bot-session storage_state lives on a shared Docker volume (Johnny-4ph)
The meet-worker needs a Playwright `storage_state.json` to sign into
Google as the bot. The file lives at
`{root}/account-<id>/storage_state.json` inside the API container,
where `root` is `/var/lib/johnny/google-auth` (bind-mount of the
`google_auth_state` docker volume) — or whatever
`JOHNNY_BOT_AUTH_STATE_ROOT` points at for tests. **Two interchangeable
producers** write to the same path:

* `johnny.tools.seed_auth_state` (CLI on the host, used by operators).
* `PUT /auth/google/accounts/<id>/bot-session` (UI upload).

Validation lives in `app.services.bot_auth_seed.validate_storage_state`:
must be a JSON object with a non-empty `cookies` list and (optionally)
`origins` as a list — anything else raises `BotSessionError` and is
surfaced as HTTP 400. Writes are atomic via `tempfile` + `os.replace`
in the target directory so the meet-worker never opens a half-written
file.

`bot_session_status(account_id)` is pure file-stat: `connected=True`
iff the path exists. It does NOT validate the cookies are still
session-valid — that determination only happens when the meet-worker
actually tries to sign in. So UI showing "Connected" only means the
file is present, not that the bot can join right now.

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

### Serving TTS PCM as in-browser-playable WAV (Johnny-6ij)
All TTS providers yield 16 kHz mono signed-16-bit LE PCM (the
canonical meet-worker audio format). To return that as a playable
`audio/wav` response, `app/api/providers.py::_pcm_to_wav_bytes`
uses the stdlib `wave` module directly against a `BytesIO`:
```py
buf = io.BytesIO()
with wave.open(buf, "wb") as wf:
    wf.setnchannels(PCM_CHANNELS)
    wf.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
    wf.setframerate(PCM_SAMPLE_RATE_HZ)
    wf.writeframes(pcm)
return buf.getvalue()
```
That emits a valid 44-byte RIFF header followed by the raw samples —
every modern browser plays it from a `new Audio(URL.createObjectURL(blob))`
with no decoder shim. Pattern is reusable any time the API needs to
return synthesised audio inline.

### Per-card Audio + revokeObjectURL on every teardown path
The `/providers` page can have multiple TTS cards playing samples at
once. The reactive Svelte rune (`$state`) doesn't tear down browser
resources for you — if you only revoke the object URL on the Stop
button, you leak the blob whenever playback ends naturally, errors,
or the user navigates away. The page keeps a plain
`Map<number, {audio, url}>` for the live handles plus a `$state`
array of playing ids for reactivity, and calls
`URL.revokeObjectURL` from FOUR places: Stop click, `audio.ended`
listener, `audio.error` listener, and `onDestroy`. Forget any one
and you leak a few KB of WAV every preview.

### Fast (VAD-driven) barge-in beats the LLM classifier on latency (Johnny-ze3)
The Johnny-di9 classifier-only barge-in is structurally too slow for
mid-utterance interrupts. End-to-end latency is the sum of:
* VAD end-of-speech detection (`end_of_speech_ms`, ~600 ms in prod)
* STT processing of the complete utterance (~200-500 ms)
* Classifier LLM call (~300-1000 ms)

That floors interrupt latency at ~1.5-3 s — long enough that the user
gives up and talks over the bot for the whole utterance. The fix is to
fire `interrupt()` from inside `_utterances()` once N consecutive
VAD-classified speech frames are seen AND the bot is responding (same
gating predicate as the classifier path). Default N = 8 frames at
20 ms/frame = 160 ms, which beats the 200 ms target and filters single-
frame coughs / lip-smacks without filtering real words.

The classifier still runs on the finalised transcript — but only as a
post-hoc observability signal, not a latency-critical decision. It can
log that the interrupt was "noise" or "side_chat" so operators can
audit false-positive rates without slowing the hot path. The fast path
runs *synchronously* inside the VAD frame loop (no `await` between the
threshold check and `interrupt()`) so the only latency is N frames of
speech plus the 1-frame TTS-event-check interval.

Per-utterance one-shot: a sustained 5-second speech burst must fire
`interrupt()` exactly ONCE, not 250 times. The `_utterances()` loop
keeps a `fast_barge_in_fired_this_utterance` flag that resets every
time VAD detects end-of-speech (or `max_utterance_ms` chunks the
buffer). Forgetting this resets makes the `_fast_barge_in_count`
observability counter useless and spams the log.

### BufferedTransport tests don't pace frames — fast path may not fire
The test `_BufferedTransport.capture_frames()` yields all frames as
fast as the consumer pulls them, with no per-frame `await`. That means
the `_utterances()` loop can burn through ALL queued frames in a
single event-loop scheduler tick, without ever yielding control to the
respond loop in between. Consequence: tests that depend on the
respond loop having flipped `_response_in_flight=True` while
later utterances' speech frames are being processed are *timing
fragile*. The four-utterance fixture happens to interleave because
the STT (with `_SlowFakeSTT`'s 20 ms sleep) is the only async-yield
point between consecutive utterances — so the respond loop runs
during that sleep. The two-utterance fixture often DOESN'T, because
all four "speech bursts then silence" segments fit into one
synchronous burn through the buffered transport.

**Practice for new tests of the fast path:** stall the bot inside TTS
via an `asyncio.Event` (the existing `_StallingTTS` pattern), wait for
`tts_entered` to confirm the response loop is wedged with
`_response_in_flight=True`, THEN assert on `_fast_barge_in_count`.
This pattern is timing-stable regardless of how many utterances the
fixture carries. Tests that assert the fast path does NOT fire (e.g.
`barge_in_min_speech_ms=0`) should ALSO stall the bot, to prove the
in-flight precondition was satisfied — otherwise the test could pass
because there was nothing to interrupt, which would be a regression
in disguise.

---

## 2026-06-06 - Johnny-4ph
- Added a UI surface for connecting the bot's Google sign-in session.
  The CLI helper (`johnny.tools.seed_auth_state`) stays in place for
  operators/automation; the new UI path lets a user upload the JSON
  file the helper produced (via `--keep-local`) instead of running
  `docker cp` themselves.
- New backend module `app/services/bot_auth_seed.py`: validates the
  uploaded JSON is a Playwright storage_state (object with a non-empty
  `cookies` array, optional `origins` array), writes atomically to
  `{root}/account-<id>/storage_state.json` using `tempfile` +
  `os.replace`, and exposes status / delete helpers. The root is
  env-overridable (`JOHNNY_BOT_AUTH_STATE_ROOT`) so tests use a tmp
  directory instead of the real `/var/lib/johnny/google-auth` mount.
  4 MiB upload cap so a stray file can't fill the volume.
- New endpoints in `/auth/google/accounts/{account_id}/bot-session/`:
  GET (status), PUT (upload + validate + write), DELETE (remove). All
  three reject `role=user` accounts with 400 — storage_state only
  makes sense for bot identities. PUT enforces the size cap with 413
  and surfaces validation failures as 400 without writing anything.
- Frontend: extended `accounts.ts` with `getBotSessionStatus` /
  `uploadBotSession` / `deleteBotSession`; the Settings page now
  shows "Bot session: Connected (saved ...)" or "Not connected" on
  every bot account row, with `Connect bot session` /
  `Replace bot session` / `Disconnect session` actions. The connect
  modal includes inline help on producing the JSON via the CLI
  helper (collapsed `<details>` block by default).
- Caveat / follow-up: this satisfies the "UI surface for bot sign-in"
  spirit of the bead but still requires the user to run the CLI to
  generate the JSON. Truly "no terminal needed" sign-in needs a
  helper container running Playwright + noVNC so the user can drive
  Chromium from their browser — that's a separate, larger bead.
  The current implementation IS what unblocks the path: the meet-worker
  reads the same on-disk file regardless of which producer wrote it.
- Files changed:
  - `backend/app/services/bot_auth_seed.py` (new)
  - `backend/app/api/auth.py`
  - `backend/johnny/tools/seed_auth_state.py` (docstring updated)
  - `backend/tests/services/test_bot_auth_seed.py` (new, 20 tests)
  - `backend/tests/api/test_auth_bot_session.py` (new, 14 tests)
  - `frontend/src/lib/accounts.ts`
  - `frontend/src/routes/settings/+page.svelte`
- **Learnings:**
  - The `google_auth_state` docker volume is mounted RW into the API
    container (`/var/lib/johnny/google-auth`) and RO into spawned
    meet-worker containers. Already perfectly set up for an
    API-writes-/-worker-reads pattern; no compose changes needed.
  - Playwright `storage_state.json` shape is a plain JSON object with
    `cookies: [...]` (required, list of cookie dicts) and `origins:
    [...]` (optional, localStorage by origin). A file with `cookies: []`
    means sign-in failed silently — caught as 400 here so the UI
    surfaces it instead of writing a useless file.
  - `os.replace` in the SAME directory as the target is atomic across
    a single filesystem (which the docker volume is), so the
    meet-worker never opens a half-written file even mid-upload.
  - The PUT endpoint reads the raw body via `request.body()` rather
    than `UploadFile` because Playwright's storage_state.json is
    naturally a JSON object — accepting it as the raw request body
    keeps the wire format trivial and avoids multipart boundary
    handling on the frontend.
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

## 2026-06-06 - Johnny-6ij
- Added a `Play` button on every TTS provider card on `/providers` so the
  user can hear the configured voice before wiring it into a real Meet.
  The existing `Test` button is unchanged — it stays a config-validity
  smoke check. `Play` is an additional, explicit voice-quality preview.
- New endpoint `POST /providers/{provider_id}/play_sample`: synthesizes
  a fixed demo phrase via the provider, wraps the raw 16 kHz mono S16LE
  PCM into a RIFF/WAV container with `wave.open(BytesIO)`, and returns
  `audio/wav` so the browser can play it inline with an `Audio`
  element. STT/LLM rows return 400 (kind mismatch); synthesis errors
  surface as 502 with the error message in `detail` so the UI can show
  it under the card.
- New `playSample(id)` in `frontend/src/lib/providers.ts` returns the
  WAV as a `Blob`. The page mints an object URL, hands it to a fresh
  `Audio`, and tracks the live `(audio, url)` pair in a `Map` keyed by
  provider id. The `ended` listener — plus an `error` listener and the
  `onDestroy` hook — calls `URL.revokeObjectURL` so we don't leak
  blobs on natural finish, navigation, or hard playback failure.
- Per-provider playback state: each TTS card has its own
  `Play / Loading… / Stop` state. Multiple providers can play
  simultaneously without stepping on each other (`playingHandles` is
  a `Map<number, Handle>`; `playingIds` is the reactive `$state` that
  Svelte tracks for the button label).
- Files changed:
  - `backend/app/api/providers.py` (new endpoint + WAV helper)
  - `backend/tests/api/test_providers.py` (9 new tests covering WAV
    shape, demo phrase wiring, non-TTS rejection, missing factory,
    synthesis failure, empty audio, and Test-endpoint invariance)
  - `frontend/src/lib/providers.ts` (new `playSample` blob fetcher)
  - `frontend/src/routes/providers/+page.svelte` (button UI, per-card
    playback state, cleanup hooks, error surface)
- **Learnings:**
  - The Python stdlib `wave` module accepts a `BytesIO` directly as
    its file argument — no need to write to a tmp file. With
    `setnchannels(1)/setsampwidth(2)/setframerate(16000)`, it emits
    a valid 44-byte RIFF header followed by raw PCM that every
    browser plays without a decoder shim. This is the path of least
    resistance for serving streamed TTS as a playable response.
  - `URL.createObjectURL` blobs leak until you explicitly call
    `revokeObjectURL`. The reactive Svelte rune (`$state`) doesn't
    automatically tear down browser-level resources, so storing the
    `(audio, url)` handle in a plain `Map` and revoking on every
    teardown path (Stop click, `ended`, `error`, `onDestroy`) is
    necessary to avoid leaks across navigations.
  - The `Test` endpoint synthesises the string `"hi"` — that's
    intentionally cheap so a config-validity check doesn't burn a
    full sentence's worth of TTS quota every time. The new `Play`
    endpoint uses a richer phrase ("Hi there! This is a quick voice
    sample so you can hear how I sound.") because it's specifically
    a *voice-quality* preview, not a config check.
---


## 2026-06-06 - Johnny-ze3
- Built voice barge-in that actually fires inside the latency budget.
  Johnny-har (concurrent transcribe/respond) + Johnny-di9 (post-utterance
  classifier) provided the scaffolding, but in a real Meet session the
  interrupt was effectively unreachable: VAD end-of-speech (600 ms) +
  STT (200-500 ms) + classifier LLM (300-1000 ms) floored interrupt
  latency at ~1.5-3 s, so the user could speak over the bot for 18+
  seconds and the bot wouldn't yield the floor (session 154 evidence).
- Added a VAD-driven fast path inside `_utterances()`. Each VAD-
  classified speech frame increments a per-utterance counter; once it
  crosses `barge_in_min_speech_ms / frame_duration_ms` (default 8 frames
  / 160 ms) AND the bot is responding AND the mode produces audio,
  `interrupt()` fires synchronously. No LLM in the hot path. The TTS
  loop already checks `_interrupt_event` between yielded frames (~20 ms
  per check), so end-to-end "user starts speaking → TTS cuts" is
  ~180 ms by construction.
- The classifier still runs on the finalised transcript — but now
  purely as a post-hoc observability signal. Operators get
  `category=noise/side_chat` verdicts in the log to audit false-positive
  rates, but the interrupt has already happened. Acceptable tradeoff:
  the bead explicitly ranked "real interrupts work" above "cough never
  false-positives".
- Added `barge_in_min_speech_ms: int = 160` to `PipelineConfig` as the
  knob. `0` disables the fast path (used by the legacy classifier-
  failure test to pin pre-fast-path semantics). The gating predicate
  `_should_fast_barge_in()` is intentionally identical to
  `_should_classify_barge_in()` so `enable_barge_in=False` /
  non-speaking modes turn BOTH paths off together — operators never end
  up with one path firing while the other is muted.
- Added `_fast_barge_in_count` counter + single-line `logger.info` per
  fire (includes session id and threshold) for production diagnosis.
  The pattern is the same as
  `barge-in classifier failed for session=...` — grep-friendly,
  one record per event.
- Added 13 new tests: default threshold, threshold-to-frame math,
  predicate parity with the classifier path, fast-path fires during
  bot response, doesn't fire when idle, disabled via
  `barge_in_min_speech_ms=0`, disabled via `enable_barge_in=False`,
  skipped in listen_only/suggest_only, skipped when `speak=False`,
  fires at most once per utterance, doesn't fire for sub-threshold
  bursts (80 ms), log line includes session id.
- Updated `test_barge_in_classifier_failure_does_not_interrupt` to
  pass `barge_in_min_speech_ms=0` so it stays focused on the
  classifier-fail semantics (the fast path is a separate interrupt
  source covered by its own tests).
- Files changed:
  - `backend/johnny/voice_pipeline/pipeline.py`
  - `backend/tests/voice_pipeline/test_pipeline.py`
- **Learnings:**
  - VAD speech-onset is a perfectly good barge-in signal on its own —
    no LLM needed in the hot path. The classifier only earns its place
    as a post-hoc auditor, not a latency-critical decision.
  - The `interrupt_event.clear()` call at the start of
    `_answer_and_speak` naturally absorbs a stale interrupt set during
    the *previous* response — so the fast path firing on utterance N's
    leading-edge tail doesn't leak into the response for utterance N+1.
    That's why the stale-verdict test still passes without modification:
    the fast path may fire, but the clear() between responses wipes it.
  - When writing tests of `_fast_barge_in_count`, you MUST stall the
    bot in TTS via an `asyncio.Event` before asserting. The
    `_BufferedTransport.capture_frames()` fixture doesn't pace frames
    — without the stall, the transcribe loop may consume all frames
    before `_response_in_flight=True` flips, and the fast path never
    fires. Tests that assert the fast path *doesn't* fire need the
    same stall, otherwise they're false-passing on the absence of the
    precondition rather than on the gating logic.
  - The default-disabled `_make_test_pipeline` helper packs the common
    "VAD-sensitive, slow STT, switching router, stalling TTS" setup
    behind one call site — five lines instead of forty per test.
    Worth the small abstraction because the fast-path test surface is
    going to grow as we tune the threshold per meeting.
---
