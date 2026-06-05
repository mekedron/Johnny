# PRD: Johnny — Google Meet AI Meeting Bot

## Overview
Johnny is a single-user AI assistant that joins your Google Meet meetings on your behalf and can listen, transcribe, suggest replies, ask for approval, or speak autonomously within configured constraints. It runs as a daemon stack (Docker Compose) with a SvelteKit web UI for calendar browsing, per-meeting configuration, and real-time approval/transcript visibility.

The product is built around three swappable layers: meeting connector (Playwright+Chromium driving a real Google Meet session), voice-agent pipeline (Pipecat with a LiveKit transport adapter), and provider-agnostic STT/LLM/TTS adapters (cloud and local from day one). All meeting data — full transcripts, router decisions, and agent utterances — is persisted to PostgreSQL.

## Goals
- Let the user mark calendar events for Johnny to attend, with per-meeting context, instructions, and behavior mode
- Support four bot modes: listen-only, suggest-only, approval-required, and limited auto-speak — selectable per meeting
- Provide provider adapters from day one so OpenAI, Anthropic, Gemini, Deepgram, ElevenLabs, faster-whisper, vLLM/Ollama, and Piper are all swappable without code changes
- Run entirely on local Docker Compose with PostgreSQL, Redis, FastAPI, SvelteKit, and isolated meet-worker containers per active meeting
- Allow Johnny to join either as a dedicated bot Google account or as the user's own account, configurable per meeting
- Persist full meeting audit log (transcripts, router decisions, agent utterances) for post-meeting review

## Quality Gates

These commands must pass for every user story:
- `uv run pytest` — Backend tests
- `uv run ruff check` — Python linting
- `uv run mypy` — Python type checking
- `pnpm typecheck` — Frontend type checking
- `pnpm lint` — Frontend linting

For UI stories, also include:
- Verify in browser using chrome-devtools MCP and capture screenshots of the relevant view(s) for visual validation

## User Stories

### US-001: Project scaffolding
As a developer, I want a working monorepo skeleton with `uv`-managed Python backend and pnpm-managed SvelteKit frontend so that subsequent stories have a stable foundation.

**Acceptance Criteria:**
- [ ] `backend/` contains FastAPI app initialized with `uv init` and `pyproject.toml` listing fastapi, uvicorn, sqlalchemy, alembic, pydantic, pytest, ruff, mypy
- [ ] `frontend/` contains SvelteKit project initialized with pnpm and TypeScript enabled
- [ ] `backend/app/main.py` exposes `GET /health` returning `{"status": "ok"}`
- [ ] Frontend home route renders "Johnny" heading and fetches `/health` successfully
- [ ] Root `README.md` documents `uv sync`, `pnpm install`, and `docker compose up` commands

### US-002: Docker Compose stack
As a developer, I want a Docker Compose configuration with all services so that the entire stack runs with a single command.

**Acceptance Criteria:**
- [ ] `docker-compose.yml` defines services: `api`, `worker`, `frontend`, `postgres` (with pgvector extension), `redis`
- [ ] Each service has a healthcheck and named volumes for persistent data
- [ ] `api` exposes port 8000, `frontend` exposes port 5173, `postgres` exposes 5432 only on the internal network
- [ ] `.env.example` documents all required environment variables (DATABASE_URL, REDIS_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, provider API keys)
- [ ] `docker compose up` brings the stack up and `/health` is reachable from host

### US-003: Database schema and migrations
As a developer, I want SQLAlchemy models and Alembic migrations for all core tables so that the data model is versioned and reproducible.

**Acceptance Criteria:**
- [ ] Models exist for: `google_accounts`, `calendar_events`, `profile_templates`, `meeting_configs`, `bot_sessions`, `transcript_chunks`, `agent_decisions`, `agent_utterances`, `provider_credentials`
- [ ] `meeting_configs` links to a `profile_template` and stores override fields (instructions, context, allowed_replies, mode, identity)
- [ ] Alembic initial migration creates all tables and enables `vector` extension
- [ ] `pgvector` column on `transcript_chunks` for embedding storage
- [ ] `uv run alembic upgrade head` runs cleanly against the Compose postgres

### US-004: SvelteKit application shell
As a user, I want a navigable web UI shell with sidebar routing so that I can move between Calendar, Templates, Providers, and History sections.

**Acceptance Criteria:**
- [ ] Layout renders persistent sidebar with links: Calendar, Templates, Providers, History, Settings
- [ ] Each link routes to a placeholder page that renders its title
- [ ] Active route is visually indicated in the sidebar
- [ ] Layout includes a header showing the currently selected Google account (placeholder until US-006)
- [ ] Mobile-responsive collapse for sidebar at narrow viewport widths

### US-005: Google OAuth 2.0 desktop flow
As a user, I want to authenticate with Google using a desktop OAuth client so that Johnny can read my calendar and act with my credentials.

**Acceptance Criteria:**
- [ ] Backend endpoint `POST /auth/google/start` returns the consent URL for desktop OAuth client
- [ ] Backend endpoint `POST /auth/google/callback` exchanges authorization code for tokens and persists them encrypted to `google_accounts`
- [ ] Refresh-token rotation is handled by a shared client wrapper used by all Google API calls
- [ ] Required scopes: `calendar.readonly`, `calendar.events.readonly`, plus OpenID profile
- [ ] Tokens are encrypted at rest using a `FERNET_KEY` env var

### US-006: Multi-account management
As a user, I want to add both my personal Google account and a dedicated `johnny-bot` account so that I can pick which identity joins each meeting.

**Acceptance Criteria:**
- [ ] Settings page lists all connected Google accounts with email, role label (`user` / `bot`), and "Disconnect" button
- [ ] "Add account" button initiates OAuth flow and lets the user tag the account as `user` or `bot`
- [ ] Disconnecting an account revokes the refresh token via Google's revocation endpoint
- [ ] At most one account can be designated the default `user` identity
- [ ] Multiple `bot` accounts may exist; meeting configs reference one by ID

### US-007: Calendar fetch and polling worker
As a user, I want my upcoming calendar events to load when I open the UI and refresh periodically so that I see accurate scheduling and detect rescheduled meetings.

**Acceptance Criteria:**
- [ ] Backend endpoint `GET /calendar/events?account_id=X&window_days=14` fetches events from Google Calendar API and upserts into `calendar_events`
- [ ] Each event row stores: external_id, start_time, end_time, summary, organizer, attendees (JSON), meet_link (parsed from `hangoutLink` or conference data), updated_at
- [ ] A background worker (Celery or Dramatiq beat) polls every 5 minutes for events that have a `meeting_config` attached, to detect time/location changes
- [ ] On detected change, the worker updates the row and emits a WebSocket event so the UI can refresh
- [ ] Polling cadence is configurable via env var

### US-008: Calendar view UI
As a user, I want to see my upcoming meetings in a chronological list so that I can decide which ones Johnny should attend.

**Acceptance Criteria:**
- [ ] Calendar page lists events grouped by day for the next 14 days
- [ ] Each row shows: time range, summary, organizer, attendee count, Meet link presence indicator
- [ ] Events without a Meet link are visually dimmed and not selectable for bot configuration
- [ ] "Refresh" button triggers an on-demand sync against `GET /calendar/events`
- [ ] Row click opens the meeting detail panel (next story)

### US-009: Per-meeting bot configuration form
As a user, I want to configure Johnny for a specific meeting with a profile template, overrides, identity, and mode so that the bot behaves correctly for that conversation.

**Acceptance Criteria:**
- [ ] Meeting detail panel shows: event metadata, "Enable Johnny" toggle, profile template selector, identity selector (user / bot accounts), mode selector
- [ ] Mode selector offers: Listen only, Suggest only, Approval required, Limited auto-speak
- [ ] Override fields editable per meeting: additional instructions, additional context, additional allowed replies
- [ ] Saving the form persists to `meeting_configs` and surfaces a success indicator
- [ ] Disabling "Enable Johnny" deletes the config row after confirmation

### US-010: Profile templates CRUD
As a user, I want reusable profile templates so that I can quickly apply the same bot behavior to multiple meeting types.

**Acceptance Criteria:**
- [ ] Templates page lists all templates with name, mode, and "Edit"/"Delete" actions
- [ ] "New template" form captures: name, default mode, base instructions, base context, allowed_replies list
- [ ] Deleting a template warns if any `meeting_configs` reference it; allow soft-delete or cascade detach
- [ ] Templates seed with two examples on first run: "Listen-only standup" and "Approval-required client call"
- [ ] Template form validates that `allowed_replies` is a non-empty list when mode is "Limited auto-speak"

### US-011: Provider adapter interfaces
As a developer, I want abstract base classes for STT, LLM, and TTS so that any concrete provider can be plugged in without changing the pipeline.

**Acceptance Criteria:**
- [ ] `backend/app/providers/base.py` defines `STTProvider`, `LLMProvider`, `TTSProvider` ABCs
- [ ] `STTProvider.transcribe_stream(audio_iter)` yields `TranscriptEvent` objects (partial/final, timestamp, confidence)
- [ ] `LLMProvider.chat(messages, tools=None, response_format=None)` returns `LLMResponse` with text and structured output support
- [ ] `TTSProvider.synthesize_stream(text, voice_id)` yields raw audio frames (16kHz mono PCM)
- [ ] A provider registry maps `(kind, name)` → factory and is populated at startup from `provider_credentials`

### US-012: Cloud STT adapters
As a user, I want cloud STT options so that I can start without local GPU resources.

**Acceptance Criteria:**
- [ ] `DeepgramSTT` implementation using Deepgram streaming WebSocket
- [ ] `OpenAIRealtimeSTT` implementation using OpenAI Realtime API
- [ ] Both adapters pass a shared `tests/providers/test_stt_contract.py` contract test (mocked transport)
- [ ] Each adapter is registered in the provider registry and selectable via configuration
- [ ] Latency and partial-transcript behavior documented in adapter docstrings (one short line each)

### US-013: Local STT adapter
As a user, I want a local STT option so that I can run Johnny without sending audio to a third party.

**Acceptance Criteria:**
- [ ] `FasterWhisperSTT` implementation wrapping `faster-whisper`
- [ ] Model size selectable via config (`tiny`, `base`, `small`, `medium`, `large-v3`)
- [ ] Model files cached to a Docker volume to avoid re-downloads
- [ ] Passes the same contract test as cloud STT adapters
- [ ] Streaming chunking honors VAD boundaries from the pipeline (no fixed-window assumption)

### US-014: Cloud LLM adapters
As a user, I want multiple cloud LLM options so that I can pick the best balance of quality, latency, and cost.

**Acceptance Criteria:**
- [ ] Adapters for OpenAI, Anthropic, and Google Gemini, each implementing `LLMProvider`
- [ ] All adapters support tool calling and JSON-schema structured output
- [ ] Shared contract test verifies streaming, tool calls, and structured-output paths
- [ ] Model name selectable per provider via config
- [ ] Errors map to a common `LLMError` exception hierarchy

### US-015: Local LLM adapter
As a user, I want a local LLM option so that I can keep meeting content fully on-device.

**Acceptance Criteria:**
- [ ] Single adapter `OpenAICompatibleLLM` that targets any OpenAI-compatible endpoint (vLLM, Ollama, LM Studio)
- [ ] Configurable `base_url`, `api_key`, and `model` per provider record
- [ ] Passes the shared LLM contract test against a mocked endpoint
- [ ] Documentation includes example configs for vLLM-served Qwen and Ollama-served Llama (one short README line each)
- [ ] Tool-call format negotiation handles both OpenAI-native and Hermes-style tool calls

### US-016: Cloud TTS adapters
As a user, I want cloud TTS options so that Johnny sounds natural without local GPU resources.

**Acceptance Criteria:**
- [ ] Adapters for ElevenLabs and OpenAI TTS, each implementing `TTSProvider`
- [ ] Streaming output produces 16kHz mono PCM frames suitable for the meet-worker audio bridge
- [ ] Voice ID configurable per provider
- [ ] Shared contract test verifies streaming and frame format
- [ ] Cancellation mid-stream stops upstream HTTP request cleanly

### US-017: Local TTS adapter
As a user, I want a local TTS option so that I can run the full stack offline.

**Acceptance Criteria:**
- [ ] `PiperTTS` implementation wrapping the Piper binary or Python bindings
- [ ] Voice model files cached to a Docker volume
- [ ] Passes the shared TTS contract test
- [ ] Streaming output meets the same 16kHz mono PCM format as cloud adapters
- [ ] Voice selection exposed via config

### US-018: Provider configuration UI
As a user, I want to manage provider credentials and select the active provider for STT, LLM, and TTS so that I can switch backends without touching code.

**Acceptance Criteria:**
- [ ] Providers page lists configured providers grouped by kind (STT, LLM, TTS) with active selection per kind
- [ ] "Add provider" form captures provider name, credentials, and provider-specific fields (base_url, model, voice_id)
- [ ] Credentials are stored encrypted using the same Fernet key as Google tokens
- [ ] "Test" button on each row invokes a 1-line smoke call (e.g., short STT against silence, LLM "say hi", TTS one-word synth) and reports success/failure
- [ ] Active provider per kind is enforced by a unique constraint and reflected in pipeline behavior on next bot session

### US-019: Meet worker base container
As a developer, I want a Docker image that runs Playwright + Chromium under Xvfb with PulseAudio configured so that the meet worker has a complete A/V environment.

**Acceptance Criteria:**
- [ ] `Dockerfile.meet-worker` extends a Playwright Python base and installs Xvfb, PulseAudio, and dependencies
- [ ] Entrypoint script starts Xvfb, PulseAudio, and a virtual sink + virtual source before invoking the worker process
- [ ] A diagnostic command `python -m johnny.meet_worker.selfcheck` verifies that the virtual mic and sink are detected
- [ ] Container can be run standalone with `docker run` and prints "self-check OK"
- [ ] Image is referenced from the main Compose file but spawned per-session, not at startup

### US-020: Google Meet join automation
As a user, I want Johnny to join the configured Meet link automatically as the selected Google identity so that the bot appears as a participant when the meeting starts.

**Acceptance Criteria:**
- [ ] Playwright script signs into Google with the selected account's stored cookies (or token-based flow)
- [ ] Script navigates to the Meet link, dismisses camera/mic preview prompts, and clicks "Join now"
- [ ] On join, the bot keeps its mic muted and camera off by default
- [ ] Join attempt fails fast with a structured error if the meeting hasn't started or access is denied
- [ ] Successful join updates `bot_sessions.status = 'joined'` and emits a WebSocket event

### US-021: Audio bridge
As a developer, I want the meet worker to capture meeting audio from PulseAudio and play synthesized audio into the virtual microphone so that the voice pipeline can hear and speak.

**Acceptance Criteria:**
- [ ] Capture path: PulseAudio monitor source → resample to 16kHz mono PCM → asyncio queue
- [ ] Playback path: asyncio queue → resample if needed → write to virtual mic source
- [ ] Bridge survives transient audio underruns without crashing the session
- [ ] Bridge is exposed as a `MeetAudioBridge` class with `capture_frames()` async generator and `play_frames(frames)` coroutine
- [ ] Unit tests verify resampling and frame size correctness using fixture audio

### US-022: Pipecat pipeline with Silero VAD
As a developer, I want a Pipecat-based pipeline that wraps VAD + STT + LLM + TTS so that the voice loop is orchestrated by a battle-tested framework.

**Acceptance Criteria:**
- [ ] Pipeline definition assembles: AudioInput → SileroVAD → STT → LLM → TTS → AudioOutput
- [ ] Transport layer is abstracted behind a `JohnnyTransport` interface with a default `LocalAudioTransport` that reads/writes the `MeetAudioBridge`
- [ ] VAD threshold configurable per meeting config
- [ ] Pipeline emits structured events (`TranscriptFinalized`, `RouterDecisionMade`, `AgentSpoke`) to a Redis pub/sub channel
- [ ] Unit test runs the full pipeline against a fixture WAV file and asserts events fire in the expected order

### US-023: Router LLM and decision schema
As a user, I want Johnny to decide whether to speak via a router model before producing an answer so that the bot does not interject inappropriately.

**Acceptance Criteria:**
- [ ] Router prompt takes: rolling transcript window, meeting instructions, allowed_replies, mode, and last decision
- [ ] Router returns structured JSON: `{should_speak, confidence, reason, reply_type, suggested_reply}`
- [ ] Decision is persisted to `agent_decisions` with timestamp, full input window, and raw model output
- [ ] Router only proceeds to the answer stage when `should_speak == true` and `confidence >= configured_threshold`
- [ ] Threshold configurable per profile template and overridable per meeting

### US-024: Answer LLM and TTS chain
As a user, I want Johnny to produce a spoken reply when the router approves so that the bot can contribute to the conversation.

**Acceptance Criteria:**
- [ ] Answer LLM receives: meeting context, full instructions, transcript window, and the router's `suggested_reply` (if any) as a hint
- [ ] Output streams token-by-token into the TTS adapter to minimize time-to-first-audio
- [ ] In "Limited auto-speak" mode, the answer is constrained to one of the `allowed_replies` (LLM picks the closest match, no free generation)
- [ ] Generated utterance is persisted to `agent_utterances` with mode, prompt, output text, and audio duration
- [ ] Cancellation (e.g., user interrupts via UI) stops both LLM stream and TTS playback within 500 ms

### US-025: LiveKit transport adapter
As a developer, I want a LiveKit transport implementation of `JohnnyTransport` so that the pipeline can run inside a LiveKit room when stronger realtime infra is needed.

**Acceptance Criteria:**
- [ ] `LiveKitTransport` joins a LiveKit room and exposes the same `capture_frames` / `play_frames` interface as `LocalAudioTransport`
- [ ] Transport selection is a single config flag — no other pipeline code changes
- [ ] Smoke test: pipeline runs end-to-end against a local LiveKit dev server (containerized) with the same fixture audio
- [ ] Local Pipecat-only mode remains the default for development
- [ ] README documents the swap with a one-line config example

### US-026: Listen-only and Suggest-only modes
As a user, I want non-speaking modes so that I can use Johnny as a transcriber or as a real-time suggestion engine without it ever talking.

**Acceptance Criteria:**
- [ ] In "Listen only" mode, the pipeline skips the router entirely and only persists transcript chunks
- [ ] In "Suggest only" mode, the router runs and writes decisions, but the answer stage is replaced by a UI notification carrying the suggested reply
- [ ] Both modes leave `agent_utterances` empty for the session
- [ ] WebSocket events distinguish `mode=listen` vs `mode=suggest` for the UI
- [ ] Mode is enforced server-side; a client-side bug cannot cause the bot to speak in a non-speaking mode

### US-027: Approval-required mode with browser push
As a user, I want approval-required mode to push notifications to my browser so that I can approve replies without keeping the UI focused.

**Acceptance Criteria:**
- [ ] Service worker registered in SvelteKit requests notification permission once per session
- [ ] When router decides `should_speak=true`, backend creates a pending `agent_decision` and pushes a Web Push notification with the suggested reply
- [ ] Notification has "Approve" and "Reject" actions; clicking either calls the backend within 30 seconds
- [ ] If no response within a configurable timeout (default 15 seconds), the decision is auto-rejected and logged
- [ ] In-UI live view also shows the pending decision with the same approve/reject controls

### US-028: Limited auto-speak mode
As a user, I want auto-speak mode to be constrained to my pre-approved phrase list so that Johnny can respond automatically without saying anything risky.

**Acceptance Criteria:**
- [ ] When mode is "Limited auto-speak", the answer LLM is invoked with a tool/structured-output schema that forces selection from `allowed_replies`
- [ ] If no allowed reply fits, the bot stays silent and logs the skipped decision
- [ ] Spoken utterance must be a verbatim match for an allowed reply (no paraphrasing)
- [ ] Spoken count per meeting is rate-limited (configurable, default max 3 per 5 minutes)
- [ ] Each utterance is persisted with the matched `allowed_reply_id` for auditing

### US-029: Bot session scheduler
As a developer, I want a background scheduler that spawns and tears down bot sessions at the right time so that Johnny joins promptly and exits cleanly.

**Acceptance Criteria:**
- [ ] Celery (or Dramatiq) beat checks every minute for `meeting_configs` whose event starts within the next 2 minutes and has no active `bot_session`
- [ ] For each due meeting, scheduler enqueues a `start_session` task that spawns the meet-worker container
- [ ] A `stop_session` task is scheduled for `event.end_time + 60s` and gracefully terminates the worker
- [ ] Manual "Join now" and "Leave now" buttons in the UI invoke the same tasks
- [ ] Scheduler state (active sessions, scheduled jobs) is visible in the UI status panel

### US-030: Per-session container spawning
As a developer, I want each meet session to run in its own isolated container so that one bad meeting cannot affect others.

**Acceptance Criteria:**
- [ ] `start_session` task uses Docker SDK to launch a `meet-worker` container with env vars (session_id, meet_link, account_id, provider_config, instructions)
- [ ] Container is named `meet-worker-session-<id>` and labeled for easy filtering
- [ ] On container exit, logs are tail-copied to `bot_sessions.logs` and the row status updated
- [ ] Crashed containers are not auto-restarted; the session is marked failed with the exit reason
- [ ] A cleanup task prunes stopped containers older than 24 hours

### US-031: WebSocket for live updates
As a user, I want the UI to receive live events from active sessions so that I see transcripts, decisions, and approval prompts in real time.

**Acceptance Criteria:**
- [ ] Backend exposes `WS /ws/sessions/{session_id}` that streams events from a Redis pub/sub channel
- [ ] Frontend subscribes when viewing a session and renders events in a virtualized list
- [ ] Event types: `transcript_partial`, `transcript_final`, `router_decision`, `approval_pending`, `agent_spoke`, `session_status_change`
- [ ] Reconnect logic recovers gracefully without duplicating rendered events
- [ ] A separate `WS /ws/global` carries notifications about session lifecycle for the calendar view

### US-032: Live transcript and approval UI
As a user, I want a live view of an active session with transcript, decisions, and approval controls so that I can supervise Johnny in real time.

**Acceptance Criteria:**
- [ ] Session view shows three panes: rolling transcript, decision feed, pending approvals
- [ ] Transcript pane shows speaker labels when available, partial vs finalized styling, and auto-scrolls
- [ ] Decision feed shows each router decision with reason, confidence, and outcome (spoken / suppressed / pending)
- [ ] Pending approvals pane shows the suggested reply with approve/reject buttons and a countdown to auto-reject
- [ ] "End session" button appears at the top and triggers `stop_session`

### US-033: Transcript and audit persistence
As a user, I want full audit data persisted so that I can review what was said, what Johnny decided, and what Johnny spoke.

**Acceptance Criteria:**
- [ ] `transcript_chunks` stores finalized chunks with session_id, start_offset, end_offset, speaker, text, embedding (pgvector)
- [ ] `agent_decisions` stores every router invocation with input window, raw output, and outcome
- [ ] `agent_utterances` stores every spoken reply with mode, prompt, output text, and audio duration
- [ ] All three tables are indexed by session_id and timestamp for fast retrieval
- [ ] A nightly job (Celery beat) computes and stores embeddings for transcripts that lack them

### US-034: Post-meeting history view
As a user, I want a per-meeting history view so that I can review past sessions, search transcripts, and audit Johnny's behavior.

**Acceptance Criteria:**
- [ ] History page lists past sessions with date, duration, mode, decision count, utterance count
- [ ] Clicking a session opens a detail view with full transcript, decision feed, and utterance list (same panes as live view, read-only)
- [ ] Transcript search uses pgvector similarity over the embedding column
- [ ] Sessions can be deleted manually; deletion cascades to transcripts, decisions, and utterances
- [ ] Export button produces a JSON dump of the session and all related rows

## Functional Requirements
- FR-1: The system must support OAuth 2.0 desktop client flow for Google with `calendar.readonly`, `calendar.events.readonly`, and OpenID scopes
- FR-2: The system must allow multiple Google accounts to be connected and tagged as either `user` or `bot` identity
- FR-3: The system must fetch calendar events on demand when the UI opens and poll every 5 minutes (configurable) for changes to events with attached bot configs
- FR-4: The system must extract Google Meet links from `hangoutLink` and conference data fields of calendar events
- FR-5: The system must allow per-meeting configuration consisting of: profile template, identity (user or bot account), mode, and free-text overrides (instructions, context, allowed_replies)
- FR-6: The system must support four bot modes: Listen only, Suggest only, Approval required, Limited auto-speak
- FR-7: The system must expose pluggable adapters for STT, LLM, and TTS, with both cloud and local implementations available from day one
- FR-8: The voice pipeline must use Pipecat with a swappable transport, defaulting to a local audio transport and supporting LiveKit transport via a single config flag
- FR-9: The system must spawn an isolated Docker container per active meet session
- FR-10: The meet worker must join Google Meet via Playwright + Chromium with PulseAudio virtual mic and sink configured
- FR-11: The system must enforce mode constraints server-side: a non-speaking mode must make speaking physically impossible
- FR-12: In Limited auto-speak mode, spoken output must be a verbatim match for an entry in `allowed_replies`
- FR-13: The router LLM must produce structured JSON `{should_speak, confidence, reason, reply_type, suggested_reply}` and must be invoked before any answer generation
- FR-14: Approval-required mode must deliver browser push notifications and in-UI prompts, with a configurable auto-reject timeout (default 15 seconds)
- FR-15: All transcripts, router decisions, and agent utterances must be persisted to PostgreSQL
- FR-16: Provider credentials must be encrypted at rest using Fernet with a key from environment configuration
- FR-17: The entire stack must run via `docker compose up` with no manual setup beyond filling `.env` and authenticating Google accounts
- FR-18: All Python code must pass `ruff check`, `mypy`, and `pytest`; all frontend code must pass `pnpm typecheck` and `pnpm lint`

## Non-Goals
- Multi-user accounts and tenant isolation (single-user MVP only)
- AWS, ECS, EKS, or any cloud deployment configuration
- Webhook-based calendar push notifications (polling-only)
- Telegram, Slack, email, or any non-browser notification channel
- Mobile native apps
- Speaker diarization beyond what STT providers offer natively
- Video capture or screen-share recording (audio only)
- Automatic system-theme detection or light theme variants (dark UI only or chosen single theme)
- Self-hosted LiveKit infrastructure beyond a local dev container
- Multi-language meeting handling (English-only for MVP; provider defaults apply)
- Detection-evasion or stealth-bot behavior

## Technical Considerations
- Pipecat already ships a `LiveKitTransport`; the adapter in US-025 should wrap that rather than reimplementing
- pgvector lives inside PostgreSQL to avoid introducing a separate vector DB
- The meet-worker image will be large (Chromium + Xvfb + PulseAudio + Python deps); plan for ~2 GB image size and use multi-stage builds where possible
- Provider adapter contract tests are critical — they prevent regressions when adding a new backend
- The Fernet key must be persisted across container restarts; document this in `.env.example`
- Google may flag automated Meet joins; using a dedicated bot account isolates risk from the user's primary account
- LLM router and answer stages can use different models (e.g., faster/cheaper router, smarter answerer); the adapter system should allow this per-pipeline-stage selection

## Success Metrics
- A user can connect Google accounts, mark a meeting, and have Johnny join silently within 90 seconds of the meeting start
- The four modes behave distinguishably in observable ways (transcripts only / UI suggestions / push approval / verbatim allowed replies)
- Swapping any one provider (STT, LLM, or TTS) requires only a UI change, never a code change
- A full session (join, transcribe, decide, optionally speak, exit) completes without manual intervention
- Post-meeting, the user can review the complete audit trail (transcript + decisions + utterances) for any past session
- All quality gates pass on every PR

## Open Questions
- Should the bot announce itself in chat on join when the per-meeting instructions don't specify disclosure behavior — default on, default off, or always prompt the user?
- For Limited auto-speak with rate limiting, should hitting the limit notify the user via push, or silently suppress?
- Should the embedding model used for pgvector transcript search be configurable, or fixed (e.g., to OpenAI `text-embedding-3-small`) for MVP?
- For the dedicated `johnny-bot` Google account, do we expect the user to create it manually or do we document an automated setup script?