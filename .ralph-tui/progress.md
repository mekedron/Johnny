# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Web Audio playback on a click-handler-await chain
When the AudioContext is constructed AFTER `await getUserMedia(...)`, Chrome's
autoplay policy can leave it in `suspended` state — every scheduled
`createBufferSource().start()` plays silently. ALWAYS call `await
audioCtx.resume()` immediately after `new AudioContext(...)` in any
flow that starts from a click handler but awaits other work first.
See `frontend/src/lib/browserAudio.ts` (Johnny-ckz.11). Also resume
on every playback frame as belt-and-suspenders for tab-focus loss.

### Per-session browser pipeline runner registry
Browser-source sessions hold an in-process `BrowserSessionRunner`
keyed by `bot_session_id`. The WebSocket endpoint looks the runner up,
shares its transport, and on disconnect schedules a grace timer
(`DISCONNECT_GRACE_SECONDS = 60`) + silent-drain task so the pipeline
keeps running for tab-close + reopen. See
`backend/app/api/browser_sessions.py::_schedule_disconnect_watchdog`.

### Pipeline.feed_text() — text→TTS injection
`VoicePipeline.feed_text(text)` publishes a `TranscriptFinalized` and
queues it on `_response_queue` so the router → answer → TTS path runs
exactly as it would for a transcribed voice utterance. Used by the
playground's text-input fallback (mic-denied / mic-muted). See
`backend/johnny/voice_pipeline/pipeline.py`.

---

## 2026-06-06 - Johnny-ckz.11

### What was implemented
Critical audio playback bug fix + comprehensive playground UI overhaul.

**Audio fix (the critical bug):**
- Root cause: `AudioContext` constructed after `await getUserMedia` could
  land in `suspended` state on Chrome (autoplay policy). Every
  `createBufferSource().start()` ran silently.
- `browserAudio.ts` now calls `await audioCtx.resume()` after creation
  and again on each playback frame as a guard against tab-focus loss.
- Verified end-to-end via chrome-devtools MCP: `AudioContext.state ===
  'running'`, 41 `BufferSource.start()` calls scheduled at advancing
  timestamps after the bot replied.

**Playground configuration UI:**
- Decision mode picker exposes all 6 `BotMode` values; switching mode
  drives the same router / approval / TTS code paths as a real meeting.
- Template picker pulls from `/templates`; selected template's
  `base_instructions` + `base_context` are stitched into the effective
  system prompt.
- STT / LLM / TTS provider override pickers (in an Advanced section)
  pull from `/providers`; each row defaults to "Use active default".
- Context-injection textarea is appended to the system prompt as
  `Additional context` so the playground can simulate per-event
  surfaces without a calendar event.
- Persona + custom system prompt kept from before.

**Live UI improvements:**
- Idle / Listening / Thinking / Speaking state indicator driven by
  speaking flag + mic level + recent router-decision timestamps.
- Active-config chips (mode / template / STT / LLM / TTS / persona)
  so the user can verify settings without leaving the live view.
- Speaker volume slider routes through a new `GainNode` between
  `BufferSource` and `audioCtx.destination`.
- Mic level meter (10 Hz RMS sampling via `AnalyserNode`).
- Independent mic + speaker mute toggles.
- Live transcript pane (left) + controls pane (right), responsive
  to single-column on tablet/mobile.

**Tab-close survival + reopen:**
- WS disconnect now schedules a 60 s grace timer instead of tearing
  down the transport. A concurrent silent-drain task absorbs TTS
  frames produced while no tab is attached.
- Re-attach within the grace window cancels both timer + drain. After
  the grace expires the watchdog calls `transport.stop()` and the
  pipeline exits cleanly, marking the session ENDED.
- A second tab attaching while one is already connected is refused
  with `{"type":"ended","reason":"session already attached"}`.
- Session-detail page shows a "Reopen playground" button for
  browser-source rows in non-terminal states; it links to
  `/playground?session=ID`.
- Playground page detects `?session=ID`, fetches `/sessions/{id}`,
  hydrates persona / system_prompt / mode from `playground_overrides`,
  seeds the transcript pane from history, and starts a fresh audio
  WS against the live runner.

**Text input now drives the full pipeline:**
- `VoicePipeline.feed_text(text)` injects typed input as a
  `TranscriptFinalized` and queues it for the response loop. Router /
  answer / TTS run identically to a voice utterance.
- `/sessions/browser/{id}/text` endpoint calls `pipeline.feed_text`
  when the runner is alive; falls back to persisting a chunk if not.

**Backend schema:**
- `BotSessionRead` (regular `/sessions/active`, `/sessions/{id}`)
  surfaces `audio_ws_path` for browser-source rows so the
  session-detail page can offer Reopen.
- `playground_overrides` is also exposed on the read model.

### Files changed
**Frontend**
- `frontend/src/lib/browserAudio.ts` — AudioContext resume, GainNode,
  mute toggles, mic level meter, speaking-state callback.
- `frontend/src/lib/sessions.ts` — BotSession adds `audio_ws_path` +
  `playground_overrides`.
- `frontend/src/routes/playground/+page.svelte` — total rewrite
  covering all config knobs, state indicators, live controls,
  reattach via `?session=ID`, hydrated transcript.
- `frontend/src/routes/sessions/[id]/+page.svelte` — Reopen
  playground button for browser-source live sessions.

**Backend**
- `backend/app/api/browser_sessions.py` — disconnect grace timer,
  silent drain, second-tab refusal, text endpoint drives pipeline,
  runner captures pipeline reference via on_assembled callback.
- `backend/app/api/sessions.py` — `BotSessionRead` exposes
  `audio_ws_path` + `playground_overrides`.
- `backend/app/services/browser_pipeline_runner.py` —
  `run_browser_pipeline` accepts `on_assembled` callback so the API
  layer can capture the assembled pipeline.
- `backend/johnny/voice_pipeline/pipeline.py` — `feed_text(text)`
  injects typed input as a finalised transcript.

**Tests**
- `backend/tests/voice_pipeline/test_pipeline.py` — 2 new tests for
  `feed_text` (drives router→answer→TTS; rejects empty).
- `backend/tests/api/test_browser_sessions.py` — 3 new tests
  (active endpoint surfaces audio_ws_path, session detail surfaces
  audio_ws_path, disconnect watchdog round-trip).

**Verification**
- Frontend `pnpm check`: 0 errors, 0 warnings.
- Backend pytest (voice_pipeline + browser_sessions + sessions):
  338 passed, 2 skipped.
- chrome-devtools MCP screenshots in
  `.ralph-tui/iterations/playground-*.png`.

### Learnings
- **AudioContext autoplay-policy gotcha** — see the pattern at the
  top of this file. This was the single root cause of the
  "transcription appears but no sound" bug.
- **Per-tab session ownership** — pipeline needs a TTL after WS
  disconnect, not immediate teardown, otherwise accidental tab close
  loses the session. A silent-drain coroutine is necessary so the
  unbounded playback queue doesn't accumulate frames during the
  grace window.
- **HMR doesn't help across docker-compose** — the frontend
  container bakes source at build time, so iterating UI changes
  requires `docker compose up -d --build frontend`. (Confirmed by
  observing the page render the prior page version after a code
  edit + reload without a rebuild.)
- **chrome-devtools MCP probe pattern for closure-private state** —
  the audio module's `AudioContext` and `GainNode` live in a closure.
  Inject an `initScript` that wraps `AudioContext`, captures every
  instance on `window.__lastAudioCtx`, and patches
  `AudioParam.value` setter to snapshot writes globally. That gave
  hard evidence for the audio-fix proof (state running, 41 frames
  scheduled, gain.value written by slider drag).

---
