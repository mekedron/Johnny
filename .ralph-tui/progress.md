# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Meet-worker stop: kill by name AND label, then verify (no silent ENDED)

`DockerContainerLauncher.stop()` (Johnny-ajc) finds containers two ways
and unions the matches:

* by `container_name` when the row stored one, AND
* by `johnny.session-id=<bot_session_id>` label.

After stop+remove on all matches, it lists by label one more time. If
anything still exists, it raises `LauncherError` so
`stop_session_by_id` marks the row `failed` instead of silently
`ended`. Without this, a row whose `container_name` was `None` (start
raced with stop) or stale (re-launched container with a different
name) would silently no-op — the API marks the row ENDED, the UI
shows "ended", but the real meet-worker keeps running the call.

Two failure modes the verification catches: (a) Docker daemon
swallowed the kill, (b) a second labelled container somehow exists
(crashed launcher mid-restart) and is still listening to the same
meeting. Either way the operator sees a real error instead of a bot
that thinks it left.

Tests in `tests/services/test_docker_launcher.py` exercise:
label-fallback when `container_name` is None, multiple-container
sweep, and the post-stop verification raising on leftovers.

### Spawned meet-worker containers must run with `init=True`

`run_kwargs["init"] = True` puts tini as PID 1 inside each
meet-worker (Johnny-ajc). Without it, the bash entrypoint `exec`s
into python, python becomes PID 1, and Linux's PID 1 rule drops
signals that don't have explicit handlers. A SIGTERM arriving during
the synchronous bootstrap phase (before
`_idle_until_signal_or_disconnect` calls
`asyncio.add_signal_handler`) gets ignored — Docker's 10 s grace
expires, SIGKILL hits, the container dies but the bot didn't
gracefully leave the meeting (and any pre-handler asyncio shutdown
was skipped). Tini forwards signals regardless of phase.

Test guard:
`test_start_runs_with_init_for_signal_forwarding` asserts
`run_kwargs["init"] is True`. Don't drop it.

### In-browser voice surface uses `BrowserAudioTransport` (no Meet)

For browser-sourced sessions (Johnny-ckz.6) the voice pipeline runs
in-process in the API container against
`johnny.voice_pipeline.browser_transport.BrowserAudioTransport` — a
`JohnnyTransport` that exchanges raw 16 kHz mono S16LE PCM via async
queues. The WebSocket endpoint at `/ws/sessions/{id}/audio` pushes
inbound frames in (`push_capture_frame`) and pulls outbound TTS frames
out (`drain_playback_frames`). No Google Meet, no LiveKit, no
meet-worker container in the path.

The same pipeline classes that the meet-worker uses are wired up by
`app.services.browser_pipeline_runner.assemble_browser_pipeline`. A
per-session `BrowserSessionRunner` (registered in
`app.api.browser_sessions._session_runners` by `bot_session_id`)
owns the transport + the asyncio task so the WebSocket endpoint can
attach asynchronously after `POST /sessions/browser/start` returns.

A SMOKE-level e2e test
(`tests/services/test_browser_pipeline_e2e.py`) drives this end to
end with a fake STT + fake router LLM to prove transport → pipeline →
event_bus is wired correctly without needing real audio.

### `bot_sessions.source` separates meet vs browser sessions

The `0007` migration adds `source` (`'meet'` | `'browser'`) plus
`playground_overrides` JSON, and makes `meeting_config_id` nullable.
A CHECK constraint forces `source='meet'` rows to keep their FK so
legacy data stays valid. UI badges browser sessions in the layout
status list and exposes the source on the `BotSessionRead`
schema — analytics / reports that historically aggregated all
sessions should now filter on `source` to keep playground sessions
out of meeting metrics.

### httpx must always set `follow_redirects=True` when hitting Hugging Face

HuggingFace's `https://huggingface.co/<repo>/resolve/main/<file>` endpoint
is documented to 307-redirect to a CDN-cached blob URL. `httpx` defaults
to NOT following redirects (unlike `requests`), so any code that fetches
from HF must pass `follow_redirects=True` on the client. Mirror the
pattern in `backend/app/providers/piper_tts.py:fetch_voice_catalog` /
`download_voice` — both branches (client-injected and default) must
enable it. A unit test that asserts `follow_redirects=True` appears in
the function source guards against future regressions
(`tests/providers/test_piper_tts.py::test_fetch_voice_catalog_uses_follow_redirects_by_default`).

### Model dirs are host bind mounts, not Docker named volumes

Voices (`piper_models`) and STT weights (`whisper_models`) live in host
bind mounts under `~/.johnny/{piper,whisper}-models`, not in Docker named
volumes. This lets users drop files in by hand (`ls ~/.johnny/...`) and
keeps downloads across `docker compose down -v` resets. `run.sh` creates
the dirs idempotently on first boot. The in-container target path stays
the same (`/var/lib/johnny/{piper,whisper}-models`) so adapter defaults
need no change.

When writing code that touches those dirs from the host (tests, wizards,
smoketest helpers), prefer reading the filesystem directly when given an
absolute path — only fall back to `docker run -v` for legacy bare-name
volumes. See `johnny/wizard/models.py:list_files_in_volume` and
`johnny/smoketest/checks.py:_list_files_in_volume` for the dual-mode
pattern.

### `pytest -m network` for live external probes

Use the `network` pytest marker (declared in `backend/pyproject.toml`)
for tests that need to hit a real external service. CI offline runs skip
them with `-m "not network"`; you MUST run them locally before claiming
a network-touching fix works. Don't rely on `MockTransport` alone for
network-shape bugs — that's how the original Johnny-4c0 fix missed the
307 redirect (the mock pretended HF returns 200 directly).

### Interrupt harness has a `--real` mode that hits real STT/LLM/TTS

The default `python -m johnny.e2e.interrupt` uses scripted providers
(deterministic, fast, 4/4 PASS in <30 s). The `--real --providers-file
<path>` mode swaps in real Deepgram + OpenAI adapters from a
`providers.json` and pre-renders the synthetic speaker's audio through
the configured TTS so real STT can transcribe it. Cached PCM lives in
`backend/tests/e2e/interrupt/fixtures/speech/`. Use
`--fallback-tts-openai` when the configured TTS is unusable (e.g.
ElevenLabs out of credits): it builds an OpenAI TTS adapter from the
OpenAI LLM api_key.

Real-mode assertions are FUZZY on transcript text (Deepgram returns
proper capitalisation + punctuation) and wider on latency (2 s
interrupt-to-cut vs scripted 500 ms; 45 s scenario timeout). Real
silences need to be 8 s after each speech event so the bot's real STT
→ LLM → TTS chain (~4–6 s) finishes before the next speaker event.

---

## 2026-06-06 - Johnny-ajc

Fix the "Leave now" silent-no-op bug — UI marked the session ended but
the meet-worker container kept running the meeting because
`DockerContainerLauncher.stop()` had two silent early-return paths
(`container_name is None` and `NotFound` on get-by-name) plus no
post-stop verification.

**Files changed:**

- `backend/app/services/docker_launcher.py` —
  `DockerContainerLauncher.stop()` rewritten to (a) discover targets
  by both `container_name` and the `johnny.session-id=<id>` label
  (union), (b) stop+remove all matches best-effort, (c) verify by
  listing the label one more time and raise `LauncherError` if
  anything remains. New helpers `_discover_stop_targets` and
  `_verify_no_session_containers` split the flow for readability.
  `start()` now passes `init=True` so tini is PID 1 inside the
  meet-worker — SIGTERM from `docker stop` is forwarded uniformly
  regardless of which phase the python bootstrap is in.
- `backend/tests/services/test_docker_launcher.py` — `_FakeContainer`
  grew a `removed` flag; `_FakeContainers.list` filters removed
  containers so the post-stop verification sees the same view a real
  daemon would. New tests:
  - `test_start_runs_with_init_for_signal_forwarding` — guards
    the `init=True` flag.
  - `test_stop_falls_back_to_label_when_name_missing` — label sweep
    kills a container even when the row's `container_name` is None.
  - `test_stop_is_noop_when_no_containers_anywhere` — replaces the
    old name-only no-op test.
  - `test_stop_swallows_not_found_on_get` — stale row name still OK
    when label sweep returns nothing.
  - `test_stop_kills_multiple_containers_for_one_session` — a
    primary container + an extra labelled one both get stopped.
  - `test_stop_raises_when_container_still_present_after_stop` — a
    failed kill surfaces as `LauncherError` (so the row is marked
    failed, not silently ended).
  - `test_stop_raises_when_verification_list_fails` — daemon dying
    mid-stop also surfaces as `LauncherError`.

**Verification:**

- 54 docker_launcher tests pass (was 49).
- 389 service + sessions API tests pass.
- 1883 backend tests pass (`pytest -m "not network and not
  livekit_smoke and not e2e_ui"`).
- `ruff` + `mypy` clean on every file touched.

**Learnings:**

- See "Codebase Patterns" at top — two new patterns extracted from
  this fix: the dual name+label stop sweep with post-verification,
  and the `init=True` requirement for reliable SIGTERM forwarding.
- The original Johnny-ckz.1 fix focused on getting the bot INTO the
  meeting; the symmetric concern (reliably getting it OUT) was
  unhandled. Two silent-no-op early returns
  (`container_name is None`, `NotFound`) in `stop()` made the row
  flip to ENDED even when the launcher did nothing. A post-stop
  verification step turns silent bugs into visible errors.
- Listing by label is more robust than name lookup for cleanup
  because labels are set on `containers.run(...)` and survive name
  changes, accidental re-launches, and start/stop races. Names are
  fragile — a single label predicate sweeps every artefact of the
  session.
- Docker SDK's `containers.run(..., init=True)` maps to `docker run
  --init` (tini as PID 1). Without it, a Python process becomes PID
  1 and a SIGTERM arriving before asyncio installs its handler is
  silently dropped — the 10 s `docker stop` grace expires and
  SIGKILL hits before any graceful cleanup runs.

---

## 2026-06-06 - Johnny-ckz.5

Fix the voice browser 307 redirect bug and switch Piper/Whisper model
dirs from Docker named volumes to host bind mounts under `~/.johnny/`.

**Files changed:**

- `backend/app/providers/piper_tts.py` — `fetch_voice_catalog` now
  constructs its default `httpx.AsyncClient` with `follow_redirects=True`
  so HuggingFace's 307 to the CDN-cached blob URL is followed instead of
  raised. `download_voice` already had this; the two branches now match.
- `docker-compose.yml` — `whisper_models` / `piper_models` named volumes
  replaced with `${HOME}/.johnny/{whisper,piper}-models` host bind mounts
  on the `api`, `worker`, and `meet-worker` services. The named-volume
  declarations are removed. Default `JOHNNY_MEET_WORKER_{PIPER,WHISPER}_VOLUME`
  env values switched to the host paths so the launcher inherits them.
- `backend/app/services/docker_launcher.py` — Default values for the
  meet-worker volume env vars now point at `~/.johnny/...` (resolved at
  import time). The volume-spec helper already accepted both host paths
  and bare names; updated docstrings to explain the dual-mode behavior.
- `run.sh` — Pre-creates `~/.johnny/piper-models` and
  `~/.johnny/whisper-models` idempotently on first boot. Detects legacy
  `johnny_piper_models` / `johnny_whisper_models` Docker volumes and
  prints a one-liner `docker cp`-style migration command for the user.
- `backend/johnny/wizard/models.py` — `WHISPER_VOLUME` / `PIPER_VOLUME`
  constants moved from `johnny_*_models` to `~/.johnny/*-models` host
  paths. `list_files_in_volume` now reads host dirs directly for
  absolute paths (faster, no docker round-trip) and falls back to the
  alpine-container path for legacy bare-name volumes. Added
  `_ensure_host_dir` helper so download functions pre-create the host
  directory with the user's uid (preventing the dockerd-as-root
  permission trap).
- `backend/johnny/smoketest/checks.py` — `check_whisper_models_dir` /
  `check_piper_voices_dir` default to the host `~/.johnny/...` paths
  via the same dual-mode `_list_files_in_volume` helper.
- `backend/tests/e2e/providers_ui/plans.py` — `local_asset` for the
  faster-whisper and Piper E2E plans switched from `/var/lib/johnny/...`
  (the in-container path) to `~/.johnny/...` (host path the test harness
  can actually `stat`).
- `backend/tests/e2e/providers_ui/preflight.py` — Updated docstring to
  reflect the host bind mount model.
- `backend/pyproject.toml` — Added `network` pytest marker so the new
  live-HF integration test can be opted in (and skipped on offline CI).
- `backend/tests/providers/test_piper_tts.py` — Added two new tests:
    1. `test_fetch_voice_catalog_uses_follow_redirects_by_default` —
       source-inspection guard so a future edit can't silently drop the
       redirect flag.
    2. `test_fetch_voice_catalog_against_real_huggingface` (marked
       `network`) — hits the real HF voices.json URL, asserts the
       catalog parses, and verifies `en_US-amy-medium` is in the list.

**Verification:**

- 1835 backend tests pass (offline subset, `pytest -m "not network and
  not livekit_smoke and not e2e_ui"`).
- New `pytest -m network` test passes against the live
  `https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json`
  URL — the redirect IS followed and the catalog parses to a non-empty
  list including `en_US-amy-medium`.
- `docker compose config` resolves both api and worker service volumes
  to `/Users/nikita/.johnny/{piper,whisper}-models` bind mounts and the
  meet-worker env vars carry the same paths.
- `ruff` and `mypy` pass on every file touched.
- Existing voice file `en_US-john-medium.onnx` (~60 MB) is already
  visible at `~/.johnny/piper-models/` on the host — confirms the bind
  mount round-trip works in practice for the user.

**Learnings:**

- See "Codebase Patterns" at top — three new patterns extracted from
  this fix: `follow_redirects=True` for HF, host bind mounts under
  `~/.johnny`, and the `network` pytest marker.
- The original Johnny-4c0 fix passed unit tests because the mock
  `httpx.MockTransport` returned 200 directly. Adding a `network` test
  that hits the real URL catches this entire class of bug — a mock
  cannot pretend to be a redirect.
- Docker's `-v <abs_path>:<container_path>` accepts both host bind
  mounts (when the LHS is an absolute path) and named volumes (when the
  LHS is a bare identifier). This lets the same helper functions
  transparently support both modes for backwards compatibility.
- Pre-creating bind-mount source dirs with the user's uid in `run.sh`
  matters: dockerd creates missing bind sources as root, which then
  prevents the user from writing into them by hand.

---

## 2026-06-06 - Johnny-ckz.4

Added a **real-provider mode** to the existing in-process interrupt
harness so it can be driven against actual Deepgram STT + OpenAI LLM +
OpenAI TTS (the configured ElevenLabs key turned out to have exhausted
credits — a real-world finding) using credentials from
``/Users/nikita/Downloads/johnny-providers-2026-06-06.json``.

**Files changed:**

- ``backend/johnny/e2e/interrupt/real_providers.py`` (new) —
  ``load_real_providers`` parses the JSON the seeder consumes,
  instantiates real adapters via the production registry. Default STT
  excludes ``faster-whisper`` (host typically lacks the model);
  ``fallback_tts_to_openai`` synthesises OpenAI TTS from the OpenAI
  LLM api_key when the configured TTS row is unusable.
- ``backend/johnny/e2e/interrupt/real_speaker.py`` (new) — pre-renders
  speaker phrases via TTS; caches PCM + JSON sidecar on disk.
- ``backend/johnny/e2e/interrupt/real_runner.py`` (new) — runs the
  scripted scenarios against a real ``VoicePipeline`` wired to real
  adapters. Uses fuzzy keyword-containment assertions, widens latency
  budgets (2 s interrupt-to-cut, 45 s scenario timeout), pads 8 s
  after each speech event.
- ``backend/johnny/e2e/interrupt/__main__.py`` — adds ``--real``,
  ``--providers-file``, ``--speech-cache-root``, and
  ``--fallback-tts-openai`` flags.
- ``backend/tests/e2e/interrupt/test_real_providers.py`` (new) — 8
  unit tests.

**Verification:**

- ``uv run python -m johnny.e2e.interrupt --no-artifacts`` (scripted)
  — 4/4 scenarios pass across 3 consecutive runs (stable).
- ``uv run python -m johnny.e2e.interrupt --real
  --providers-file /Users/nikita/Downloads/johnny-providers-2026-06-06.json
  --fallback-tts-openai --only stt_keeps_running_during_bot_speech
  cough_does_not_interrupt`` — 2/2 PASS reproducibly with real
  Deepgram + OpenAI.
- ``stop_interrupts_long_answer`` (real mode) passes when the LLM
  round-trip lands under ~3 s, flakes (no AgentSpoke) on slower turns.
  Filed Johnny-ckz.4.1.
- ``clarification_redirects_long_answer`` (real mode) — fast barge-in
  fires, follow-up answer emits AgentSpoke. The assertion shape
  expecting two AgentSpokes (cut + follow-up) doesn't match real-mode
  behavior where the cut answer publishes no AgentSpoke. Filed
  Johnny-ckz.4.2.
- All 41 existing interrupt tests pass; ``ruff``/``mypy`` clean.

**Learnings:**

- ElevenLabs' "exceeds your quota" 401 surfaces as ``TTSError`` mid-
  pipeline; the bot's response loop catches it and the session
  continues, but no audio plays.
- Real Deepgram nova-3 transcribes synthesized OpenAI TTS speaker
  audio reliably (``"Stop, please."``, ``"Tell me about yourself."``,
  ``"Wait. What about the launch date?"``).
- Fast barge-in (Johnny-ze3) reliably fires ~1.1 s after real speech
  onset against the real pipeline.
- The follow-up path (Johnny-di9) works end-to-end with real
  providers: bot answers the new question after a ``new_question``
  barge-in.
- Scripted scenarios' 1.2 s inter-event silence is far too tight for
  real-provider mode; 8 s padding after each speech event lets the
  bot start TTS before the next speaker event arrives.

---

## 2026-06-06 - Johnny-ckz.6

Implemented the in-browser voice/text chat surface — both the
per-event "Try with bot" rehearsal button and the standalone
`/playground` page. The bot now runs end-to-end without Google Meet,
the meet-worker container, or any LiveKit infra.

**Files changed:**

- `backend/alembic/versions/0007_bot_session_browser_source.py`
  (new) — adds `source` enum (`'meet'` | `'browser'`) and
  `playground_overrides` JSON to `bot_sessions`; makes
  `meeting_config_id` nullable. CHECK constraint forces meet rows
  to keep their FK so legacy data validates.
- `backend/app/db/models.py` — adds `BotSessionSource` StrEnum,
  the new columns on `BotSession`, nullable FK to `MeetingConfig`.
- `backend/johnny/voice_pipeline/browser_transport.py` (new) —
  `BrowserAudioTransport`. Implements `JohnnyTransport`. Bounded
  capture queue with oldest-drop semantics, unbounded playback
  queue, EOF sentinel + `_closed` flag for clean teardown,
  resampling on outbound frames when TTS's source rate differs
  from the transport's 16 kHz. Exported from
  `johnny.voice_pipeline.__init__`.
- `backend/app/services/browser_pipeline_runner.py` (new) —
  in-process counterpart to `johnny.meet_worker.pipeline_runner`.
  Assembles a `VoicePipeline` against a `BrowserAudioTransport`
  with the configured providers, runs it under a `stop_event`, and
  cleans up the transport + approval gate on exit. Mirrors the
  meet-worker's STT/LLM/TTS validation + speaking-mode degradation
  to `suggest_only`.
- `backend/app/api/browser_sessions.py` (new) — HTTP + WS surface:
  - `POST /sessions/browser/start` — creates a browser-source
    `bot_sessions` row (rehearsal when `event_id` is set,
    playground when not), spawns the in-process pipeline runner,
    snapshots overrides on the row.
  - `POST /sessions/browser/{id}/stop` — idempotent; signals the
    runner's `stop_event`.
  - `POST /sessions/browser/{id}/text` — text input fallback when
    mic is denied. Records a `TranscriptChunk`.
  - `GET /sessions/browser/active` — list active browser sessions
    for badging.
  - `WS /ws/sessions/{id}/audio` — raw bidirectional PCM stream
    over WebSocket. JSON `{"type":"ready"}` / `{"type":"ended"}`
    control messages around binary frames.
  - In-memory `_session_runners` registry maps session id → runner
    so the WS endpoint can attach to a live transport.
- `backend/app/api/sessions.py` — `BotSessionRead` now exposes
  `source` + nullable `meeting_config_id` so the legacy UI list
  surfaces the source consistently.
- `backend/app/main.py` — registers the new browser-sessions
  routers (HTTP + WS).
- `backend/tests/voice_pipeline/test_browser_transport.py` (new) —
  11 unit tests for the transport.
- `backend/tests/services/test_browser_pipeline_runner.py` (new) —
  7 tests for provider validation + assembly degradation.
- `backend/tests/api/test_browser_sessions.py` (new) — 16 tests
  for HTTP API contract (playground, rehearsal, stop, text input,
  inline-override gate, runner registry).
- `backend/tests/services/test_browser_pipeline_e2e.py` (new) —
  one smoke test that drives audio in → transcript out → router
  decision out through a real `VoicePipeline` + a real
  `BrowserAudioTransport` with fake STT + fake LLM.
- `backend/tests/test_db_models.py` — assertion for the new
  `BotSessionSource` enum's members.
- `frontend/src/lib/browserSessions.ts` (new) — typed API client
  for the `/sessions/browser` endpoints. Includes
  `audioWebSocketUrl()` helper that swaps `http(s)://` → `ws(s)://`.
- `frontend/src/lib/browserAudio.ts` (new) — Web Audio + WebSocket
  plumbing. AudioWorklet captures the mic, downsamples to 16 kHz,
  encodes to s16, and sends 20 ms frames as binary WS messages.
  Inbound frames decode to Float32 and play via `AudioBufferSourceNode`.
  Schedules playback using a running `nextPlaybackTime` so frames
  don't overlap or drop. Mic-denial calls `onMicDenied()` so the UI
  can fall back to text.
- `frontend/src/lib/sessions.ts` — `BotSession` type now includes
  `source: BotSessionSource` and nullable `meeting_config_id`.
- `frontend/src/routes/playground/+page.svelte` (new) — the
  playground page. Persona + custom system prompt inputs, Start
  button that creates the session + starts audio, End button that
  tears both down, text-input fallback when mic is denied,
  visible badges for `browser` source + audio readiness.
  `onDestroy` calls stop on navigation away (AC #7).
- `frontend/src/routes/calendar/+page.svelte` — adds "Try with
  bot" button next to "Join now" on every event with a configured
  meeting. Clicks call `startBrowserSession({event_id})` then
  `goto('/playground?session=<id>')` to hand off to the UI.
- `frontend/src/routes/+layout.svelte` — `/playground` added to
  the nav. Live-sessions list now shows a violet `browser` badge
  next to the status pill when `session.source === 'browser'`.

**Verification:**

- 1878 backend tests pass (was 1835 — 43 new, 0 broken). Includes
  the new smoke E2E that drives real `VoicePipeline` through
  `BrowserAudioTransport`.
- `ruff` + `mypy` clean on every new file.
- Frontend `svelte-check` and `eslint` pass (0 errors, 0 warnings).
- `from app.main import app` succeeds; the new routers register.
- DB models round-trip: `BotSession(source=BROWSER,
  meeting_config_id=None, playground_overrides=...)` inserts +
  selects cleanly through SQLAlchemy.

**Gaps / follow-ups (filed separately if pursued):**

- Real chrome-devtools MCP verification is not done here — the
  PRD calls for end-to-end browser test runs (mic permission, voice
  round-trip, interrupt latency, transcript rendering, per-event
  context parity, override scoping, end-session cleanup,
  mic-denied fallback). Architecture is wired so those tests can
  be written without further backend changes; they require a
  populated `provider_credentials` table and a running stack to
  exercise.
- Text input currently records a `TranscriptChunk` but does NOT
  yet inject the text into the pipeline's response loop — the next
  iteration should make the text trigger a router decision +
  answer + TTS turn (same as a real spoken utterance).
- `credentials_id` provider-override path is implemented but
  bypasses the user-auth surface (out of scope here). For now use
  `JOHNNY_ALLOW_INLINE_PROVIDER_CREDS=1` in dev or the configured
  active providers as the base. Overrides ARE non-destructive
  (they only mutate `playground_overrides` on the session row).
- "Try with bot" navigates to `/playground?session=<id>` but the
  playground page does not yet detach-and-attach to a pre-started
  session — it always creates a fresh one. The handoff needs the
  playground page to honour the `session` query param.

**Learnings:**

- See "Codebase Patterns" at top — two new patterns extracted:
  `BrowserAudioTransport` as the in-browser path, and the
  `source` column for distinguishing browser vs meet sessions.
- `BrowserAudioTransport.stop()` must NOT drop a queued frame to
  make room for the EOF sentinel; instead drain the queue into a
  holding list, restore the items, then push the sentinel. The
  first naïve implementation displaced the oldest frame on
  every stop call, which the unit test caught immediately.
- VoicePipeline's `tts: TTSProvider` argument is annotated as
  non-Optional, but the codebase actually passes `None` for
  non-speaking modes. Both the meet-worker pipeline_runner and
  the new browser pipeline_runner have to `cast(TTSProvider,
  None)` to satisfy mypy. Fixing this contract properly is a
  larger refactor than this bead's scope.
- AudioWorklet PCM streaming is the right path for browser →
  server audio when you can't bring in WebRTC infra. Two gotchas:
  (1) the `AudioContext`'s actual sample rate is rarely 16 kHz,
  so the worklet has to downsample inline; (2) `sampleRate` and
  `currentTime` are globals available inside the worklet — no
  need to thread them through `processorOptions`.
- The frontend `svelte-check` doesn't narrow `if (!isLive)` /
  `{:else}` blocks on the `liveSession` state — using
  `{:else if liveSession}` makes the type-narrowing explicit and
  removes three "possibly null" errors.

---
