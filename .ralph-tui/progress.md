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

### Provider export endpoint mirrors the seed schema (Johnny-k3z)
`GET /providers/export` returns the SAME JSON shape the seeder consumes,
so an export → file → re-import roundtrip reproduces provider state
exactly. `with_secrets=false` (default) emits empty credentials dicts —
the file is safe to share/commit. `with_secrets=true` decrypts and
inlines plaintext keys. The endpoint never 500s on a corrupt ciphertext;
that row just exports with empty credentials so the rest of the
inventory remains downloadable. Imports `SUPPORTED_FILE_VERSION` from
`providers_seed` so any future schema bump fails loudly at both sides
together. Filename is `johnny-providers-YYYY-MM-DD.json` (UTC date),
served via `Content-Disposition: attachment` + `Cache-Control: no-store`
so browsers neither cache nor preview secrets. Route is `/export`
literal (not `/{id}/export`) to avoid colliding with provider-id routes
— FastAPI matches `/export` before any dynamic int path because the
route is registered before them in source order.

### Browser blob download via temporary `<a download>` + revokeObjectURL
`frontend/src/lib/providers.ts::downloadBlob` is the canonical pattern
for any "fetch a file → save it" UI: mint an object URL, append a
hidden `<a>` to the DOM, click it, then revoke the URL. This is
preferable to opening in a new tab (browsers may render JSON inline
instead of saving) or to `window.open(url)` (URL leaks until the
window closes). The companion `parseFilenameFromDisposition` pulls
the `filename="..."` token out of `Content-Disposition` so the saved
filename matches the server's choice rather than a generic blob name.

### Provider seed file shape is the interchange format (Johnny-d3e / Johnny-k3z)
`/config/providers.json` (and the future export endpoint Johnny-k3z)
share a single schema defined in
`app/services/providers_seed.py::parse_providers_file`. Shape:
```json
{
  "version": 1,
  "providers": [
    {"kind": "stt|llm|tts", "provider_name": "...",
     "display_name": "...", "credentials": {...},
     "options": {...}, "is_active": true}
  ]
}
```
Any change to the schema MUST bump `SUPPORTED_FILE_VERSION` and update
both sides (seeder + export). The seeder is wired into
`app/main.py`'s lifespan after `seed_initial_templates`; modes are
controlled by `JOHNNY_PROVIDERS_FILE` (path) and
`JOHNNY_PROVIDERS_SEED_MODE` (`insert-only` default, `overwrite`, or
`disabled`). The mount is `./config:/config:ro` so the API never
writes back. Activation of `is_active=true` rows uses a bulk
`UPDATE ... WHERE kind=X AND id != target` per kind at the END of
the loop (not row-by-row) so the partial unique index
`(kind) WHERE is_active` is never transiently violated. Same write
the `POST /providers/{id}/activate` endpoint performs.

### End-of-turn endpointing: 800 ms silence + interrupt-survives-router (Johnny-arh)
The bot's "user has finished speaking" trigger is VAD-driven silence detection
in `VoicePipeline._utterances()`: once N consecutive silence frames are seen
(where `N = end_of_speech_ms / frame_duration_ms`), the buffered utterance
yields and runs through STT → router → answer → TTS. The default
`end_of_speech_ms` is now **800 ms** (was 600 ms — too short for natural
mid-sentence thinking pauses, so the bot would jump in over a user's own
multi-clause sentence). 800 ms covers the 200–700 ms hesitation pauses
typical of natural speech while still feeling responsive at true end-of-turn.

Race condition fix layered on top of the threshold bump: there's a window
between an utterance finalising and TTS starting (router LLM stage ≈ 200 ms)
where the user can resume speaking. Fast barge-in (Johnny-ze3) fires
`interrupt()` from the VAD speech-onset trigger, setting `_interrupt_event`.
The legacy code cleared `_interrupt_event` at the start of
`_answer_and_speak` — which immediately wiped any fire that landed during
the router stage, so the bot proceeded to speak over the user.

The fix moves the clear from `_answer_and_speak` to the very start of
`_respond_to_transcript_inner` (BEFORE the router). Then a post-router
`if self._interrupt_event.is_set(): persist(suppressed); return` short-
circuits the answer LLM call entirely. Net effect: every response gets
exactly ONE clear (at its own start); any interrupt fired during its
router/answer/TTS lifetime sticks through to the end.

A regression-pin test (`test_interrupt_cleared_at_response_start_not_in_answer_and_speak`)
greps the AST of `pipeline.py` to assert there's exactly one
`self._interrupt_event.clear()` call AND that it lives inside
`_respond_to_transcript_inner`. If a future refactor reintroduces the
clear in `_answer_and_speak`, the test breaks loudly with an explicit
"the Johnny-arh race regression is back" signal.

### Bot's own utterances live in `_transcript_history` as `Bot (you)` (Johnny-7qp)
The pipeline now mixes participant transcripts AND the bot's own
utterances into a single `_transcript_history` list so the router /
answer LLM prompts can recall what the bot itself just said. Without
this, the bot couldn't answer "what did you just say?" or "repeat
that" — its own prior speech never reached the prompt. After every
successful `_answer_and_speak`, `_remember_bot_utterance(text, ts)`
appends a `TranscriptFinalized` with `speaker=BOT_SPEAKER_LABEL`
(`"Bot (you)"`, defined in `transcript_history.py` so the
SQLAlchemy-free meet-worker module can import it). The system prompts
in `_router_messages` and `_answer_messages` explicitly tell the LLM
that lines prefixed `Bot (you):` are its own prior speech.

Rehydration after a container restart pulls BOTH `transcript_chunks`
and `agent_utterances` rows for the session, merges them
chronologically by `created_at`, and emits bot utterances as
`TranscriptFinalized(speaker=BOT_SPEAKER_LABEL, text=output_text)`.
Both the SQL-backed and HTTP-backed loaders do this — the API
endpoint already returns utterances with `created_at`, so the HTTP
loader just merges them client-side. Tests that exercise the SQL
loader must set `created_at` explicitly because SQLite's
`CURRENT_TIMESTAMP` is second-precision and back-to-back inserts in
the same test would otherwise collide.

The `current_pos` identity check in `_answer_messages` /
`_router_messages` is unaffected: the participant transcript object
passed in is still the one in the list, regardless of the bot
entries mixed around it. With buffered transport (no per-frame
await), tests of the round-trip recall path need `_SlowFakeSTT`
(20 ms per utterance) so the respond loop has time to flip
`_response_in_flight` and append the bot utterance between
transcripts — without that, both transcripts land in history before
the first response runs, and the bot utterance is appended at the
end (after the second transcript), which doesn't surface in the
second prompt because the prompt builder slices `history[:current_pos]`.

---

## 2026-06-06 - Johnny-arh
- Fixed the bot speaking over a user mid-sentence. Two-layer fix:
  1. **Threshold bump**: `DEFAULT_END_OF_SPEECH_MS` 600 → 800 ms in
     `johnny.voice_pipeline.pipeline`. The 600 ms default was shorter than
     natural mid-sentence hesitation pauses (200–700 ms per speech-research
     norms), so a user pausing to think mid-clause would trigger end-of-turn
     and the bot would jump in. 800 ms absorbs natural pauses without
     making true end-of-turn feel sluggish.
  2. **Race fix on `_interrupt_event`**: moved the lone `clear()` call from
     the start of `_answer_and_speak` to the start of
     `_respond_to_transcript_inner` (before the router LLM call), and
     added a post-router `if self._interrupt_event.is_set(): suppress`
     short-circuit. Without (2), even an 800 ms threshold leaves a window
     during the router LLM call (200–500 ms) where a fast barge-in fire
     would be silently wiped by the legacy clear in `_answer_and_speak`,
     letting the bot speak over the user.
- New 6 tests in `tests/voice_pipeline/test_pipeline.py`:
  - `test_default_end_of_speech_ms_is_800` — pins the new 800 ms default.
  - `test_natural_mid_sentence_pause_below_default_does_not_split` —
    700 ms silence between two tones merges into ONE utterance.
  - `test_natural_mid_sentence_pause_above_threshold_splits` — 900 ms
    silence DOES split (guards against an over-correction).
  - `test_user_resume_during_router_aborts_answer` — gated router LLM
    calls `interrupt()` mid-flight; asserts no TTS calls, no utterance
    row, decision marked `suppressed`, answer LLM never invoked (saved
    cost).
  - `test_user_resume_during_router_does_not_emit_agent_spoke` — same
    race but asserts no `AgentSpoke` published (UI subscribers don't
    see a phantom "the bot said X").
  - `test_interrupt_cleared_at_response_start_not_in_answer_and_speak` —
    AST-level pin: exactly ONE `_interrupt_event.clear()` call in
    `pipeline.py` AND it lives inside `_respond_to_transcript_inner`.
    Future refactors that re-introduce the clear in `_answer_and_speak`
    fail loudly with the explicit regression message.
- Files changed:
  - `backend/johnny/voice_pipeline/pipeline.py`
  - `backend/tests/voice_pipeline/test_pipeline.py`
- **Learnings:**
  - `_interrupt_event` has a subtle lifecycle: it's the only "stop NOW"
    signal across the entire response pipeline (router, answer LLM stream,
    TTS frame yield). When clearing it, the location matters: clearing at
    the start of a stage means any fire DURING earlier stages of the SAME
    response gets wiped. The legacy code cleared inside `_answer_and_speak`,
    which was correct for the "interrupt left over from previous response"
    case but wrong for "interrupt fired during this response's router
    stage". Moving the clear to the response entry point (before the
    router) solves both: previous-response leftovers are still wiped
    (same as before), AND this-response fires survive.
  - Existing tests covering interrupt behaviour all PASS with the moved
    clear without modification, because they fire interrupt either
    inside the answer LLM's `stream_chat` (which runs AFTER the clear in
    both old and new code) or inside the TTS stream (likewise). The race
    the bead describes is the niche case where the fire lands DURING the
    router stage — none of the pre-Johnny-arh tests exercised it.
  - When writing the "interrupt during router" test, simulating the race
    is tricky because real router LLMs return in milliseconds. The
    pattern is a `_GatedRouterLLM` that sets an event when it's "entered"
    the chat call, then awaits a release event before returning. The
    test calls `pipeline.interrupt()` from the same task between entry
    and release — guarantees the event is set BEFORE the router returns
    without relying on cross-task scheduling timing. `enable_barge_in=False`
    keeps the fast-barge-in classifier out of the picture so the assertion
    pins exactly the post-router check.
  - Raw PCM (no WAV header) works straight through `_BufferedTransport`
    when each frame is the right size (640 bytes = 20 ms @ 16 kHz mono
    S16LE). Skipping the WAV-encode-then-decode roundtrip dropped a
    handful of mypy errors about `Wave_read` vs `Wave_write` typing on
    a reused `BytesIO`. Future fixtures that need a custom audio layout
    can use `_make_pcm_with_pause(...)` directly.
  - The AST-walk regression test (`test_interrupt_cleared_at_response_start_not_in_answer_and_speak`)
    is a high-leverage pattern for "this specific invariant must be
    enforced at a specific source location". It reads the module text,
    parses it with `ast.parse`, walks the function definitions, and
    asserts the call appears in exactly one named function. Worth
    reaching for whenever a fix's correctness depends on the LOCATION
    of a single call (clear, set, persist, etc.) — code-review can
    miss "you put this in the wrong function" but the AST test can't.
---

## 2026-06-06 - Johnny-7qp
- Fixed bot losing its own conversation context: the LLM prompts now
  surface every prior bot utterance alongside participant transcripts
  in the same `_transcript_history` list, so the bot can quote /
  paraphrase what it just said when asked to repeat itself.
- Added `BOT_SPEAKER_LABEL = "Bot (you)"` in
  `johnny.voice_pipeline.transcript_history` (the SQLAlchemy-free
  module both loaders import). The label is unlikely to collide with
  a real Meet display name and reads correctly in the prompt without
  extra translation.
- `VoicePipeline._remember_bot_utterance(text, timestamp_ms)` appends
  a `TranscriptFinalized(speaker=BOT_SPEAKER_LABEL, ...)` to
  `_transcript_history` after every successful `_answer_and_speak`.
  Empty / whitespace-only output is skipped. The existing window-size
  cap (`_enforce_history_window`, extracted out of `_remember_transcript`)
  applies to bot entries too.
- System prompts in both `_router_messages` and `_answer_messages`
  now include a paragraph explaining that `Bot (you):` lines are the
  bot's own prior utterances — so when a participant says "repeat
  what you just said", the answer LLM grounds its reply in the
  verbatim text of those prior bot lines.
- Rehydration loaders updated:
  - `SqlAlchemyTranscriptHistoryLoader` now queries both
    `transcript_chunks` and `agent_utterances` and merges by
    `created_at` (tie-break: transcripts before utterances so
    "participant spoke, bot replied" order survives a same-second
    insert).
  - `HttpTranscriptHistoryLoader._payload_to_transcripts` now folds
    the API's `utterances` list into the result, parsing ISO-8601
    `created_at` (both `Z` and `+00:00` forms) to keep ordering
    deterministic. Unparseable / missing dates fall back to wire
    order so an older API can't break rehydration.
- Files changed:
  - `backend/johnny/voice_pipeline/pipeline.py` — `_remember_bot_utterance`,
    prompt builders, post-speech bookkeeping.
  - `backend/johnny/voice_pipeline/transcript_history.py` —
    `BOT_SPEAKER_LABEL` constant + `__all__` export.
  - `backend/johnny/voice_pipeline/__init__.py` — re-export.
  - `backend/app/services/transcripts.py` — SQL loader merges bot
    utterances.
  - `backend/johnny/meet_worker/transcript_loader.py` — HTTP loader
    parses utterances + chronological merge.
  - `backend/tests/voice_pipeline/test_pipeline.py` — 9 new tests
    (recall round-trip, prompt rendering, label semantics, window
    cap interaction, system message wording).
  - `backend/tests/services/test_transcripts.py` — 4 new SQL-loader
    tests + the `AgentUtterance.__table__` fixture addition.
  - `backend/tests/test_meet_worker_transcript_loader.py` — 6 new
    HTTP-loader tests covering the merge, blank text, unparseable
    dates, and the `+00:00` form.
- **Learnings:**
  - The `current_pos` identity check (`t is transcript`) makes mixing
    bot and participant entries safe without a separate list. The
    participant transcript passed into the prompt builder is always
    the one in `_transcript_history`, so the slicing works regardless
    of how many bot entries are interleaved around it.
  - With the unbuffered `_FakeSTT` + `_BufferedTransport` test fixtures,
    both transcripts can be transcribed before the respond loop ever
    runs. That means the bot utterance is appended AFTER transcript 2
    in the list — which doesn't surface in transcript 2's prompt
    because `history[:current_pos]` cuts it off. `_SlowFakeSTT`
    (20 ms per utterance) restores realistic interleaving; this is
    the same pattern the barge-in tests use.
  - SQLite's `func.now()` is second-precision, so back-to-back inserts
    in the same test get identical `created_at` values and break the
    chronological merge assertion. Set `created_at` explicitly on the
    ORM instance before `commit()` (the `_ts(seconds_offset)` helper)
    to make the merge order deterministic without affecting prod.
  - The API's `GET /sessions/{id}` endpoint already returns
    `utterances` with `created_at` — no DB migration was needed.
    The HTTP loader just had to start consuming that field and
    interleave it with the transcripts list.
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

## 2026-06-06 - Johnny-d3e
- Added a JSON-file seeder so the user can commit a `providers.json`
  alongside the stack and have provider rows auto-reconciled on every
  API boot. The file shape is the canonical interchange format for
  this seeder AND the export endpoint (Johnny-k3z, still open) — both
  sides must move together if the schema changes.
- New module `app/services/providers_seed.py`:
  - `SeedMode` enum: `INSERT_ONLY` (default), `OVERWRITE`, `DISABLED`.
    Default is the safest: missing rows get inserted, existing rows
    are left alone (UI edits survive). `OVERWRITE` re-encrypts
    credentials, replaces options, and syncs `is_active`. `DISABLED`
    is a kill switch.
  - `parse_providers_file(path)` validates shape strictly: version
    must be `1`, every entry needs a valid `kind` enum + non-empty
    `provider_name` + `display_name`; `credentials` must be a string
    map (no nulls); `options` must be a JSON object; `is_active`
    must be a real bool. Rejects the file as a whole on any error
    so a partial parse never leaves the DB half-seeded.
  - `seed_providers_from_file(session, crypto, ...)` returns a
    `SeedResult` dataclass tracking created / updated / skipped /
    activated entries. Logs a one-line summary per run.
  - Activation handling: collects all `is_active=true` entries, picks
    the last per kind (matching how the activate endpoint behaves),
    deactivates siblings via a bulk UPDATE so the partial unique
    index `(kind) WHERE is_active` is never violated. Warns when the
    file declares multiple actives for the same kind.
- Wired into `app/main.py` lifespan after `seed_initial_templates`.
  Wrapped in the same try/except pattern: a malformed file logs a
  warning, the API still boots.
- docker-compose.yml: bind-mounted `./config:/config:ro` into the api
  and worker services (read-only so the API can't write back to the
  source-of-truth file), exported `JOHNNY_PROVIDERS_FILE` and
  `JOHNNY_PROVIDERS_SEED_MODE` to both. Created an empty `config/`
  directory with a `.gitkeep` so the bind mount works on a fresh
  clone, plus `config/providers.example.json` showing the shape.
- `.env.example` documents both env vars with the same defaults the
  compose file ships.
- Files changed:
  - `backend/app/services/providers_seed.py` (new)
  - `backend/app/main.py` (lifespan hook)
  - `backend/tests/services/test_providers_seed.py` (new, 46 tests)
  - `docker-compose.yml` (bind mount + env vars)
  - `config/.gitkeep` (new)
  - `config/providers.example.json` (new)
  - `.env.example` (documentation)
- **Learnings:**
  - Defining `SUPPORTED_FILE_VERSION = 1` as a constant + a parse-time
    version check is cheap insurance: any future change to the file
    shape forces an intentional bump that ripples through both the
    seeder and the export endpoint (Johnny-k3z). A test pinning the
    constant catches accidental drift.
  - The active-per-kind partial unique index means we can't naively
    flip `is_active=true` row-by-row inside a single transaction —
    the second row of the same kind would violate the index even if
    the first one was about to be deactivated. The fix is to collect
    all activation requests during the row loop, then do a bulk
    `UPDATE ... WHERE kind=X AND id != target` for each kind at the
    end (one round-trip per kind). Mirrors what the
    `POST /providers/{id}/activate` endpoint does, so the two paths
    stay in sync.
  - Coercing credential values to strings (`str(val)` for any int /
    float that sneaks into the JSON) is correct because the encrypted
    blob shape is `dict[str, str]` — `decrypt_json` would otherwise
    reject the value on read. A test pins this so a future user who
    exports a port number as an int doesn't get a runtime decrypt
    failure mid-meeting.
  - In `OVERWRITE` mode, an explicit `is_active=false` MUST clear the
    flag (not leave it as-is) — otherwise an export → edit → re-import
    roundtrip can't deactivate a provider, which would be surprising.
    Insert-only mode never touches the flag because the whole point
    of that mode is "don't clobber UI state".
---

## 2026-06-06 - Johnny-k3z
- Added one-click export of every provider configuration as a single
  JSON file matching the Johnny-d3e seeder schema. The user can keep
  the file as a backup, move it to a new machine, or drop it into
  `config/providers.json` so the next stack startup re-seeds the rows.
- New endpoint `GET /providers/export?with_secrets={true|false}`. Body
  is the same `{"version": 1, "providers": [...]}` shape the seeder
  expects. Default `with_secrets=false` returns empty credential dicts
  so the file is safe to share or commit. `with_secrets=true` decrypts
  and inlines plaintext keys; a corrupt-ciphertext row exports with
  empty credentials rather than 500ing the whole download. The endpoint
  imports `SUPPORTED_FILE_VERSION` from `providers_seed` so any future
  schema bump touches both sides together.
- Response headers: `Content-Disposition: attachment; filename=
  "johnny-providers-YYYY-MM-DD.json"` (UTC date) and
  `Cache-Control: no-store` so the browser saves the download instead
  of previewing it, and a `with_secrets=true` payload never lands in
  HTTP cache. Pretty-printed JSON (2-space indent) so users can diff
  / edit the file by hand.
- Frontend `providers.ts` gets `exportProviders(withSecrets)` returning
  `{blob, filename}` (filename parsed from `Content-Disposition` so the
  saved file matches the server's choice) plus a reusable `downloadBlob`
  helper that mints an object URL, clicks a hidden `<a download>`, then
  revokes the URL — the canonical browser pattern for blob downloads.
- `/providers` UI gets an "Export configuration" button next to
  Refresh/Add. Click opens a modal with an explicit "Include API keys
  and other secrets" checkbox (off by default) plus copy explaining
  the tradeoff. Errors surface inline; the modal also can't be
  dismissed mid-download to avoid race-y URL leaks.
- Files changed:
  - `backend/app/api/providers.py` — new endpoint + UTC import +
    `SUPPORTED_FILE_VERSION` import.
  - `backend/tests/api/test_providers.py` — 15 new tests (empty
    export, filename format, attachment disposition, with/without
    secrets, all kinds, metadata round-trip, version matches seeder,
    full export → seeder import round-trip, corrupt ciphertext path,
    pretty-printed body, no-store cache header, route precedence,
    ordering by kind + display_name).
  - `frontend/src/lib/providers.ts` — `exportProviders`,
    `downloadBlob`, `parseFilenameFromDisposition`.
  - `frontend/src/routes/providers/+page.svelte` — Export button,
    modal, checkbox-row styles.
- **Learnings:**
  - The seeder's `parse_providers_file` accepts an empty `credentials:
    {}` (via `_ensure_str_mapping`'s `None → {}` coercion when the key
    is missing, plus the empty-dict identity path), so an
    `with_secrets=false` export is still a valid import payload — it
    just leaves credentials blank on the re-imported row. That's the
    desired behaviour: "no secrets" mode is for share/commit cases
    where the recipient pastes keys in by hand.
  - FastAPI route precedence is source order: `/providers/export` had
    to be declared in this file *before* any `GET /providers/{id}`
    route would have shadowed it as a 422. Since there's no
    `GET /providers/{id}` route in this codebase, the ordering risk is
    only latent — but the regression test pins the literal route so a
    future addition can't accidentally break it.
  - `Content-Disposition: attachment; filename="..."` makes the
    browser save the response rather than navigate to it; combined
    with the `<a download="...">` click trick on the client, the saved
    filename matches the server's `YYYY-MM-DD` choice without the
    client needing to know the date. The fallback
    `parseFilenameFromDisposition → defaultExportFilename` keeps the
    UI working even if a proxy strips the header.
  - Parsing the filename out of `Content-Disposition` with a single
    regex (`/filename="([^"]+)"/`) is enough for this case (no
    international encoding, no quoted-string escapes) — adding RFC
    6266 encoding handling would be premature complexity for a
    server we control.
---

### ElevenLabs Scribe STT is batch-only (Johnny-1zg)
The ElevenLabs Scribe API is fundamentally different from Deepgram /
OpenAI Realtime: it has NO streaming surface. There is no WebSocket,
no partials, no interim deltas — just a single
`POST /v1/speech-to-text` multipart request returning the final text
in one JSON response. So `ElevenLabsSTT.transcribe_stream` buffers the
entire VAD-bounded utterance into a `bytearray`, fires one
multipart POST, and yields exactly ONE event with `is_final=True`. The
pipeline's VAD boundary is the only finality signal.

The wire-side trick: instead of WAV-wrapping the PCM, pass
`file_format=pcm_s16le_16` as a form field and post the raw 16 kHz
mono S16LE bytes directly — Scribe keys off the form field, not the
MIME type (which is `application/octet-stream`). That matches the
meet-worker bridge format exactly, so no header munging or transcoding
is needed. This is the same pattern any future "send PCM to a batch
endpoint" adapter should follow.

Implications for latency: end-to-end response time is dominated by the
batch round-trip (~300-800 ms for a short utterance), so this is a
good fallback when streaming partials aren't the constraint but
accuracy or specific language coverage matters. The existing
`_smoke_test` (200 ms of silence) works unchanged — silence returns
empty text → `_parse_response` filters it → 0 events yielded → smoke
test still reports OK.

Confidence comes from `language_probability` (the language-detect
confidence), clamped to [0, 1]. The Scribe response also carries
per-word timestamps in `words`, but the pipeline doesn't consume
them yet so the adapter just propagates the top-level `text`.

## 2026-06-06 - Johnny-1zg
- Added ElevenLabs Scribe as a first-class STT provider so kind=stt
  in the /providers UI now lists "ElevenLabs" alongside Deepgram and
  OpenAI Realtime.
- New `app/providers/elevenlabs_stt.py` — `ElevenLabsSTT` adapter
  registered under `(ProviderKind.STT, "elevenlabs")`. Same `api_key`
  credential as the existing `ElevenLabsTTS` adapter (shared
  `xi-api-key` auth header pattern). The single "elevenlabs" name now
  maps to TWO factories (STT and TTS) under different kinds — that's
  fine: `ProviderRegistry` keys on `(kind, name)` tuples.
- `field_schema()` declares:
  - **auth**: `api_key` (required, secret)
  - **model**: `model_id` (`scribe_v2` default / `scribe_v1`),
    `language_code` (blank = auto-detect), `diarize`,
    `tag_audio_events`
  - **advanced**: `base_url`, `file_format` (`pcm_s16le_16`),
    `timeout_s`
- `transcribe_stream` is batch: buffers the iterator, POSTs raw PCM
  via multipart with `file_format=pcm_s16le_16`, yields one
  `is_final=True` `TranscriptEvent`. Empty buffer / empty `text`
  short-circuits without yielding (matches Deepgram's empty-handling).
- Errors translated to `STTError` with detail extraction matching
  the TTS adapter's pattern: `detail.message`, `detail` (string),
  top-level `message`, raw body.
- Test file `tests/providers/test_elevenlabs_stt.py` — 36 tests
  covering config defaults, schema, response parsing, transcribe
  flow, both contract tests, error paths, and registry behavior.
  Mirrors the structure of `test_elevenlabs_tts.py`.
- Files changed:
  - `backend/app/providers/elevenlabs_stt.py` (new)
  - `backend/app/providers/__init__.py` — import, register, export
  - `backend/tests/providers/test_elevenlabs_stt.py` (new, 36 tests)
- **Learnings:**
  - The `_smoke_test` STT path (200 ms of silence) flows fine through
    a batch adapter: silence yields empty `text`, `_parse_response`
    returns None, the test reports "0 transcript event(s)" — `ok=True`.
    No need to special-case batch adapters in the smoke harness.
  - Multipart form posting with httpx is straightforward: pass
    `data=` for form fields and `files=` for the binary, set
    `Accept: application/json` explicitly so the server can't
    surprise us with a 415. No need for manual boundary handling.
  - Two adapters can share `PROVIDER_NAME = "elevenlabs"` because
    the registry's keyspace is `(kind, name)`. The test
    `test_elevenlabs_stt_and_tts_share_name_under_different_kinds`
    asserts this pattern works and the factories are distinct.
  - The frontend `/providers` page reads schemas dynamically via
    `GET /providers/schemas` (`app/api/providers.py:_all_schemas`),
    so registering the adapter at import time is enough — no UI
    code changes were needed to make ElevenLabs appear in the STT
    dropdown. Same goes for the structured settings form.
---

### Interrupt e2e harness uses real-time-paced transport (Johnny-2bw)
The unit suites' `_BufferedTransport` is fine for behaviour checks but
useless for latency-budget assertions — it yields every frame as fast as
the consumer pulls. `johnny.e2e.interrupt` ships a `PacedScriptedTransport`
that `asyncio.sleep(frame_duration_ms / 1000)`s between frames; that is
the *only* way to make "interrupt cut within 500 ms" meaningful, because
fast barge-in fires synchronously from the VAD loop and the only delay
budget is real wall-clock. Captured frames are tagged (`event_tag`) so
the runner can recover monotonic timestamps for "the interrupt started"
and "the bot's first AgentSpoke completed" — those two are what the
500-ms budget is measured against.

### Per-sentence TTS flush multiplies wall-clock latency in scripted harnesses
`_stream_answer_into_tts` flushes per sentence — every period in the
answer is a separate `tts.synthesize_stream` call. With a 5-sentence
answer and a `PacedTTS(frame_count=100, frame_period_s=0.02)` that's 5 ×
2 s = 10 s of TTS wall-clock per response. The harness's
`_scenario_budget_s` baked in a 5-s safety buffer that was tight enough
to trip on scenarios that exercised TWO complete answers (e.g. cough
scenario where the cough utterance gets a SENTINEL transcript and the
router reuses its last decision → second redundant answer). Two
workarounds in the harness:
* Override `answer_text` per scenario when only the interrupt mechanic is
  under test — a single-sentence reply keeps wall-clock tight.
* Always script ONE router_decision per finalised utterance. The
  `SwitchingRouterLLM` reuses the last decision when scripted ones run
  out, which is correct production behaviour but a trap in tests where
  the second utterance is incidental (cough, sentinel, etc.).

### Interrupt-to-cut latency is measured to first AgentSpoke, not last played frame
The naive measurement — "interrupt onset to the LAST played frame" —
breaks for `new_question` style barge-in scenarios where the bot fires a
follow-up answer immediately after the interrupt cut: those follow-up
frames blow the budget by seconds. The right boundary is the END of the
INTERRUPTED bot answer, which corresponds to the FIRST `AgentSpoke`
event's `timestamp_ms` (published just after `_stream_answer_into_tts`
returns). Convert it to wall-clock via
`pipeline._session_started_at + first_spoke.timestamp_ms / 1000.0`; the
transport's monotonic timestamps live in the same `loop.time()` frame so
subtraction is direct. This is what makes the clarification scenario
pass with delta_ms=147 instead of delta_ms=9383.

### Bead Johnny-arh: _interrupt_event resets between responses
After a successful fast barge-in fires, asserting
`pipeline._interrupt_event.is_set() == True` at end-of-run is wrong —
`_respond_to_transcript_inner` clears the event at the very start of
each NEW response (the Johnny-arh fix). By the time the run finishes,
any later response that *wasn't* interrupted will have cleared the
event back to False. The reliable evidence that fast barge-in fired is
`pipeline._fast_barge_in_count > 0` (or, for the classifier path, the
specific verdict on `barge_in_calls[-1]`). The harness uses the count.

## 2026-06-06 - Johnny-2bw
- Built `johnny.e2e.interrupt`, an automated two-bot interrupt-reproduction
  harness for the voice pipeline. Runs as
  `uv run python -m johnny.e2e.interrupt` and produces a pass/fail report
  plus a per-run JSON artifact in `tests/e2e/artifacts/<timestamp>-interrupt/`.
- Four scenarios mirror the bead's required reproductions:
  * `stop_interrupts_long_answer` — the headline session-160 bug. Verifies
    fast-barge-in cuts TTS within 500 ms and the bot yields the floor.
  * `clarification_redirects_long_answer` — `new_question` barge-in: cut
    THEN follow up. Verifies the follow-up actually emits.
  * `stt_keeps_running_during_bot_speech` — Johnny-har contract: all
    participant transcripts reach `transcript_chunks` even while bot is
    speaking. Mid-bot side chat lands in both the sink AND the event bus.
  * `cough_does_not_interrupt` — 80 ms cough below the 160 ms fast-path
    threshold doesn't trigger interrupt; bot completes its TTS in full.
- Each scenario runs in 4–11 s wall-clock at real frame pacing (20 ms /
  frame); full suite completes in ~27 s, well inside the bead's 5-min
  budget with margin for 10× repetition. 3/3 consecutive runs passed
  cleanly during smoke testing.
- Files changed:
  - `backend/johnny/e2e/__init__.py` (new package).
  - `backend/johnny/e2e/interrupt/__init__.py` — re-exports.
  - `backend/johnny/e2e/interrupt/__main__.py` — CLI (argparse + report).
  - `backend/johnny/e2e/interrupt/audio.py` — PCM frame synth (tone,
    silence, cough) at 16 kHz / 20 ms / mono / s16le.
  - `backend/johnny/e2e/interrupt/transport.py` — `PacedScriptedTransport`
    (real-time per-frame `asyncio.sleep`, tagged capture log, timestamped
    play log).
  - `backend/johnny/e2e/interrupt/providers.py` — `ScriptedSlowSTT`,
    `SwitchingRouterLLM`, `ScriptedAnswerLLM`, `PacedTTS` mimicking
    production timing without touching the network.
  - `backend/johnny/e2e/interrupt/scenarios.py` — four declarative
    `Scenario` definitions plus `scenarios_by_name` for `--only`.
  - `backend/johnny/e2e/interrupt/runner.py` — frame expansion, pipeline
    wiring, per-assertion evaluation.
  - `backend/johnny/e2e/interrupt/report.py` — `ScenarioResult` /
    `SuiteReport` / `render_summary` / `write_report`.
  - `backend/tests/e2e/interrupt/` — 41 tests covering audio synth, the
    paced transport's pacing behaviour, scripted providers, scenario
    catalog invariants, runner end-to-end against the real pipeline, and
    the report shape. All pass; full pytest run shows no regressions
    against the 1807-test suite.
- **Learnings:**
  - The harness's real-time pacing IS the assertion. Without it, fast
    barge-in fires at a non-deterministic offset and "cut within 500 ms"
    becomes meaningless. With it, the assertion routinely measures
    delta_ms≈150 (well under budget) so a regression that drops the
    latency back to 1.5-3 s would fail loudly.
  - Scripted providers don't need network access to test the interrupt
    path end-to-end — what matters is the *timing* of STT (per-utterance
    sleep), of the answer LLM (per-delta sleep), and of TTS (per-frame
    sleep). Together they let the production VoicePipeline drive its
    full state machine through the harness with no provider mocks
    inside the pipeline itself.
  - The harness *currently runs at the VoicePipeline level*, not at the
    meet-worker-container level. That's what the bead explicitly allows
    as a fallback when "Meet-in-the-loop is too fragile for CI". The
    scenarios are pure-data so a future container variant can consume
    the same definitions to drive a Playwright-mic-pipe variant — that
    is the natural next iteration.
---
