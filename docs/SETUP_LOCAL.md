# Johnny — Local-First Setup Guide

This guide walks you through running Johnny entirely on your own machine, with
no audio, transcripts, or LLM calls leaving the host. Follow it top-to-bottom on
a fresh laptop and you will end with a Listen-only Google Meet bot that joins,
transcribes locally with faster-whisper, and persists the full transcript to
local PostgreSQL.

Every command block is copy-pasteable. Every model has a direct download link.

---

## 0. What you will end up with

- The full Compose stack: `api`, `worker`, `frontend`, `postgres` (with
  pgvector), `redis`, and the `meet-worker` image (spawned on demand)
- A dedicated `johnny-bot@gmail.com` Google account that joins the Meet
- One Whisper model on disk (~140 MB for `base`) for local STT
- One Llama or Qwen model served by Ollama for local LLM
- One Piper voice (~60 MB) for local TTS
- A first end-to-end smoke test against a real Google Meet meeting

---

## 1. Prerequisites

This guide runs **everything inside Docker**. You do **not** need to install
Python, Node, or any backend dependencies on the host for the steps below.

**Required:**

| Tool | Why | Install |
|------|-----|---------|
| Docker Desktop / Engine + Compose v2 | Runs the whole stack | <https://docs.docker.com/get-docker/> |
| Git | Cloning | <https://git-scm.com/downloads> |
| Python 3 (any 3.9+) | One-shot Fernet key generation in §3 — ships with macOS and most Linux; or use the `openssl` fallback below | <https://www.python.org/downloads/> |
| Ollama (recommended LLM runtime) | Local LLM serving on the host (not in Compose) | <https://ollama.com/download> |

**Optional — only if you also want to run the backend / frontend / wizard
outside Docker:**

| Tool | Why | Install |
|------|-----|---------|
| `uv` (Python package manager) | Backend dev (`cd backend && uv sync`), interactive setup wizard | <https://docs.astral.sh/uv/getting-started/installation/> |
| `pnpm` 9+ and Node.js 20+ | Frontend dev mode (`cd frontend && pnpm install && pnpm dev`) | <https://pnpm.io/installation> |

**Disk space budget**

| Item | Approx. size |
|------|--------------|
| Compose images (postgres, redis, api, worker, frontend) | ~2.5 GB |
| `johnny-meet-worker` image (Playwright + Chromium + PulseAudio) | ~2.0 GB |
| faster-whisper `base` model | ~140 MB |
| faster-whisper `large-v3` (optional, best quality) | ~3.0 GB |
| Local LLM (Qwen2.5-7B-Instruct Q4) | ~4.7 GB |
| Local LLM (Llama-3.1-8B-Instruct Q4) | ~4.9 GB |
| Piper voice `en_US-amy-medium` | ~63 MB |
| Postgres + Redis volumes | grows with use; budget 5 GB |
| **Total (recommended setup)** | **~15 GB free** |

Verify Docker is running:

```bash
docker version
docker compose version
```

---

## 2. Clone and bring up the stack

```bash
git clone <your-fork-or-clone-url> johnny
cd johnny
cp .env.example .env
```

At this point the stack will start, but Google OAuth and the local providers
won't work yet. We fill `.env` next.

---

## 3. Generate the encryption key

`FERNET_KEY` encrypts Google tokens and provider credentials at rest. Generate
it once and keep it; if you lose it, all encrypted rows must be deleted.

Use this stdlib-only one-liner — it produces a valid Fernet key (32 random
bytes, URL-safe base64) without needing the `cryptography` package installed
on the host:

```bash
python3 -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

No Python 3? Use `openssl` instead:

```bash
openssl rand 32 | base64 | tr '+/' '-_' | tr -d '\n'; echo
```

Open `.env` and paste the output into `FERNET_KEY=`.

---

## 4. Create the dedicated `johnny-bot` Google account

Johnny joins meetings as a Google identity. Using a dedicated account
(separate from your personal Gmail) isolates risk if Google flags automated
Meet joins.

1. Open <https://accounts.google.com/signup> in a fresh browser profile
2. Create an account such as `johnny-bot-<yourname>@gmail.com`
3. Sign out, then sign back in once to confirm the account is active and
   has accepted Meet's terms (open <https://meet.google.com> with the account
   and join a self-test meeting once)

You can also use your primary Google account; the per-meeting "identity"
selector lets you choose either. This guide assumes a dedicated bot account.

---

## 5. Register a Google OAuth desktop client

1. Open Google Cloud Console: <https://console.cloud.google.com/projectcreate>
2. Create a project (e.g., `johnny-local`) — accept defaults
3. Enable the Google Calendar API:
   <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>
   → click **Enable** on the new project
4. Configure the OAuth consent screen:
   <https://console.cloud.google.com/apis/credentials/consent>
   - User type: **External**
   - App name: `Johnny`
   - User support email: your email
   - Developer contact: your email
   - **Scopes**: add
     - `openid`
     - `.../auth/userinfo.email`
     - `.../auth/userinfo.profile`
     - `.../auth/calendar.readonly`
     - `.../auth/calendar.events.readonly`
   - **Test users**: add **both** your personal email **and**
     `johnny-bot-...@gmail.com`
5. Create the OAuth client credentials:
   <https://console.cloud.google.com/apis/credentials>
   → **Create credentials → OAuth client ID → Application type: Desktop app**
6. Copy the Client ID and Client Secret into `.env`:

```bash
GOOGLE_CLIENT_ID=<paste-here>
GOOGLE_CLIENT_SECRET=<paste-here>
# Keep the default redirect URI:
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/auth/google/callback
```

The default loopback redirect URI matches what the API exposes when you run
`docker compose up`. Desktop OAuth clients accept loopback redirects without
explicit registration.

---

## 6. Start the stack

```bash
docker compose up -d
docker compose ps                # all services should be 'healthy'
curl -s http://localhost:8000/health    # → {"status":"ok"}
open http://localhost:5173       # SvelteKit UI
```

If any service is unhealthy, see [Troubleshooting](#troubleshooting).

---

## 7. Build the meet-worker image

The meet-worker (Playwright + Chromium + Xvfb + PulseAudio) is *built* but not
*started* by `docker compose up`. The scheduler spawns one per active Meet
session. Build it now so the image exists when a session is scheduled.

```bash
docker compose --profile meet-worker build meet-worker
docker run --rm johnny-meet-worker:latest      # → prints "self-check OK"
```

The self-check confirms Xvfb, PulseAudio, the virtual sink, and the virtual
source are all wired up inside the container.

---

## 8. Local STT — faster-whisper

faster-whisper downloads models on first use into a Docker volume
(`whisper_models`, mounted at `/var/lib/johnny/whisper-models` inside the
meet-worker container). You do not need to download manually — the adapter
fetches from HuggingFace on first run. If you want to pre-warm the cache (e.g.
for offline use), the direct downloads are below.

### Pick a model size

| Model | Disk | Real-time on M-series Mac (CPU) | Quality | Direct download (HuggingFace) |
|-------|------|------|---------|---------|
| `tiny.en` | ~75 MB | ~25× real-time | OK for clean speech | <https://huggingface.co/Systran/faster-whisper-tiny.en> |
| `base.en` | ~140 MB | ~15× real-time | Good — **recommended** | <https://huggingface.co/Systran/faster-whisper-base.en> |
| `small.en` | ~460 MB | ~8× real-time | Very good | <https://huggingface.co/Systran/faster-whisper-small.en> |
| `medium.en` | ~1.5 GB | ~3× real-time | Excellent | <https://huggingface.co/Systran/faster-whisper-medium.en> |
| `large-v3` | ~3.0 GB | ~1× real-time | Best, multilingual | <https://huggingface.co/Systran/faster-whisper-large-v3> |

**Recommendation:** start with `base.en` for English meetings — fast on CPU, good
accuracy, fits in 140 MB. Move to `medium.en` if you find errors annoying.

### Pre-warm the model cache (optional)

```bash
# Pre-download base.en into the shared whisper_models volume so the
# meet-worker doesn't have to fetch on first session start.
docker run --rm \
  -v johnny_whisper_models:/var/lib/johnny/whisper-models \
  -e JOHNNY_WHISPER_MODEL_DIR=/var/lib/johnny/whisper-models \
  johnny-meet-worker:latest \
  python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', download_root='/var/lib/johnny/whisper-models', device='cpu', compute_type='int8')"
```

The model lands in `/var/lib/johnny/whisper-models/models--Systran--faster-whisper-base.en/`
inside the shared `whisper_models` Docker volume. The path is configurable via
`JOHNNY_WHISPER_MODEL_DIR` in `.env` (defaults match the volume mount).

You will register this in the UI in §11 below as STT provider name
`faster-whisper` with options `model_size=base.en`.

---

## 9. Local LLM — Ollama (recommended) or vLLM

Pick **one** runtime. Ollama is easier for first-time setup; vLLM is faster on
NVIDIA GPUs.

### Option A: Ollama (recommended for CPU / Apple Silicon)

```bash
# 1. Install Ollama: https://ollama.com/download
# 2. Pull a model. Pick one:

ollama pull qwen2.5:7b-instruct-q4_K_M     # ~4.7 GB, very capable
# or
ollama pull llama3.1:8b-instruct-q4_K_M    # ~4.9 GB, Meta's flagship 8B
# or (smaller, faster, less capable)
ollama pull qwen2.5:3b-instruct-q4_K_M     # ~1.9 GB

# 3. Confirm Ollama is serving on port 11434 (default):
curl http://localhost:11434/api/tags
```

Direct model pages:
- Qwen2.5-7B-Instruct: <https://ollama.com/library/qwen2.5>
- Llama-3.1-8B-Instruct: <https://ollama.com/library/llama3.1>

In the Providers UI (§11), you will register an LLM provider:
- Provider name: `openai-compatible`
- Options:
  - `base_url=http://host.docker.internal:11434/v1` (Ollama on host)
  - `model=qwen2.5:7b-instruct-q4_K_M` (must match `ollama pull` tag)
- Credentials: `api_key=ollama` (Ollama ignores the value but the field must be present)

> **Mac / Windows users:** `host.docker.internal` resolves from inside Compose
> services to the host machine. Linux users: add `--add-host=host.docker.internal:host-gateway`
> to the api/worker services in `docker-compose.yml`, or use the host IP.

### Option B: vLLM (NVIDIA GPU)

Requires an NVIDIA GPU with the container toolkit installed
(<https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html>).

```bash
docker run -d --name vllm --gpus all \
  -p 8001:8000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct \
  --max-model-len 8192
```

vLLM downloads from HuggingFace on first run. Model card:
<https://huggingface.co/Qwen/Qwen2.5-7B-Instruct>

In the Providers UI (§11):
- Provider name: `openai-compatible`
- Options: `base_url=http://host.docker.internal:8001/v1`, `model=Qwen/Qwen2.5-7B-Instruct`
- Credentials: `api_key=vllm`
- Add `tool_format=hermes` to options if you swap in a Hermes-style fine-tune
  (Nous Hermes, some Qwen variants emit `<tool_call>...</tool_call>`).

---

## 10. Local TTS — Piper

Piper voices are small (~60 MB) and run on CPU with no GPU required. Each voice
ships as two files: a `.onnx` model and a `.onnx.json` config sidecar. Both
must sit next to each other in the model directory.

### Recommended voice: `en_US-amy-medium`

Natural-sounding American English, medium quality. Direct downloads from the
official Piper voice catalogue:

```bash
# Download into the shared piper_models Docker volume so the meet-worker
# container picks them up at runtime. Filenames must match exactly.
docker run --rm \
  -v johnny_piper_models:/var/lib/johnny/piper-models \
  -w /var/lib/johnny/piper-models \
  curlimages/curl:latest \
  -fLO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx \
  -fLO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json
```

Browse the full voice catalogue: <https://huggingface.co/rhasspy/piper-voices/tree/main>
A web preview to audition voices is available at <https://rhasspy.github.io/piper-samples/>.

Other popular en_US voices (same path pattern — swap the speaker name):

| Voice | Style | URL |
|-------|-------|-----|
| `en_US-amy-medium` | Female, neutral, **recommended** | `.../en/en_US/amy/medium/en_US-amy-medium.onnx` |
| `en_US-ryan-medium` | Male, neutral | `.../en/en_US/ryan/medium/en_US-ryan-medium.onnx` |
| `en_US-libritts_r-medium` | Multi-speaker | `.../en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx` |
| `en_GB-alan-medium` | British male | `.../en/en_GB/alan/medium/en_GB-alan-medium.onnx` |

All paths are under `https://huggingface.co/rhasspy/piper-voices/resolve/main/`.

In the Providers UI (§11), you will register a TTS provider:
- Provider name: `piper`
- Options: `voice_id=en_US-amy-medium` (no `.onnx` extension needed)
- Credentials: none required

### Piper binary

The `piper` binary is bundled with the `johnny-meet-worker` image (the
provider's `JOHNNY_PIPER_BINARY` env var defaults to `piper` on PATH). If you
ever need to override it, point `binary` in the provider options at the
absolute path to a vendored build:
<https://github.com/rhasspy/piper/releases>.

---

## 11. Configure providers in the UI

Open <http://localhost:5173/providers>. You will see three sections — STT, LLM,
TTS — each empty. Add one provider per section, then click **Activate** on each.

### Add the STT provider

- Kind: **STT**
- Provider name: `faster-whisper`
- Display name: `Local Whisper`
- Credentials: (leave blank)
- Options (paste into the textarea):

```
model_size=base.en
device=cpu
compute_type=int8
```

Click **Save**, then **Activate**, then **Test** — the test runs a 1-second
silence transcription and should report success.

### Add the LLM provider

- Kind: **LLM**
- Provider name: `openai-compatible`
- Display name: `Local Ollama` (or `Local vLLM`)
- Credentials:

```
api_key=ollama
```

- Options (Ollama variant):

```
base_url=http://host.docker.internal:11434/v1
model=qwen2.5:7b-instruct-q4_K_M
```

Save → Activate → Test. The smoke test sends "say hi" and prints the reply.

### Add the TTS provider

- Kind: **TTS**
- Provider name: `piper`
- Display name: `Local Piper`
- Credentials: (leave blank)
- Options:

```
voice_id=en_US-amy-medium
```

Save → Activate → Test. The smoke test synthesises one word and confirms
the PCM frames come back at 16 kHz mono.

---

## 12. Silero VAD

Silero VAD is the voice-activity detector that segments meeting audio into
utterances before STT. It is **bundled in the meet-worker image** via the
`silero-vad` PyPI package and pulled on container build — no separate download.

If you maintain your own meet-worker build outside the Compose flow, install
manually:

```bash
pip install silero-vad torch
```

The model itself (~1.7 MB) downloads automatically into a torch cache on first
use. See <https://github.com/snakers4/silero-vad>.

---

## 13. (Optional) Local LiveKit dev server

The default voice transport (`local`) wraps the meet-worker's PulseAudio bridge
and needs no extra services. Only set this up if you want to swap in the
LiveKit transport for testing.

```bash
docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
  -e LIVEKIT_KEYS="devkey: secret" \
  livekit/livekit-server --dev
```

To switch the pipeline to LiveKit, mint a token with `livekit-cli` and set:

```bash
JOHNNY_TRANSPORT=livekit
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_TOKEN=<token>
LIVEKIT_ROOM=johnny-dev
LIVEKIT_IDENTITY=johnny-bot
```

Smoke test: `uv run pytest -k livekit_smoke -v` from `backend/`.

LiveKit CLI installer: <https://github.com/livekit/livekit-cli#installation>.

---

## 14. Connect Google accounts

1. Open <http://localhost:5173/settings>
2. Click **Add account → user**
3. Sign in with **your personal Gmail** and approve Calendar scopes
4. Click **Add account → bot**
5. Sign in with **`johnny-bot-...@gmail.com`** and approve Calendar scopes

Both accounts should now appear on the Settings page with email, role label,
and a Disconnect button.

---

## 15. First-run smoke test (Listen-only)

1. **Create a test meeting** on your personal Google Calendar (or any calendar
   you connected as `user`). Set the start time to 5 minutes from now. Add a
   Google Meet link (Calendar usually adds one automatically).
2. **Invite the bot account** (`johnny-bot-...@gmail.com`) to the event so it
   appears on the bot's calendar and the Meet permits it to join.
3. **Wait up to 5 minutes** (calendar polling cadence) — or click **Refresh**
   on <http://localhost:5173/calendar>. The event should appear.
4. **Click the event** → in the detail panel:
   - Toggle **Enable Johnny** on
   - Profile template: `Listen-only standup` (seeded on first run)
   - Identity: `johnny-bot-...` (the bot account)
   - Mode: **Listen only**
   - Click **Save**
5. **Join the Meet yourself** in your browser as the user account at the
   start time.
6. **Within ~60 seconds of the meeting start**, the scheduler spawns the
   meet-worker container. Watch:

```bash
docker ps --filter "label=johnny.session" --format "table {{.Names}}\t{{.Status}}"
```

A `meet-worker-session-<id>` row appears once the container starts.

7. The bot joins the Meet silently. Open
   <http://localhost:5173/sessions> and click the active session. The transcript
   pane should fill with text as you speak.
8. End the meeting (or click **End session**). The container exits, logs are
   tail-copied to `bot_sessions.logs`, and the session moves to History.
9. Open <http://localhost:5173/history> → click the session → verify the full
   transcript is persisted.

If everything above worked, your local stack is complete. Switch the mode to
`Approval required` for the next test to exercise the answer LLM and Piper
TTS path.

---

## Troubleshooting

### `pactl: command not found` / "PulseAudio not found"

The error originates inside the meet-worker container. Confirm the image
exists and run the self-check:

```bash
docker images johnny-meet-worker
docker run --rm johnny-meet-worker:latest    # → "self-check OK"
```

If the self-check fails, rebuild without cache:

```bash
docker compose --profile meet-worker build --no-cache meet-worker
```

### "model file missing" / faster-whisper or Piper cannot load

The meet-worker container mounts the named volumes `whisper_models` and
`piper_models`. Confirm the files are there:

```bash
docker run --rm -v johnny_whisper_models:/m alpine ls -la /m
docker run --rm -v johnny_piper_models:/m alpine ls -la /m
```

For Piper, both `en_US-amy-medium.onnx` **and** `en_US-amy-medium.onnx.json`
must be present. If either is missing, re-run the download command in §10.

For faster-whisper, the layout is
`/var/lib/johnny/whisper-models/models--Systran--faster-whisper-<size>/...`.
If empty, re-run the pre-warm command in §8 — or just start a session and let
the adapter fetch on demand.

### OAuth redirect failure / "redirect_uri_mismatch"

Desktop OAuth clients accept loopback (`http://localhost`) redirects without
explicit registration, but the URI **must** match what the API sends. Verify
`.env`:

```
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/auth/google/callback
```

If you front the API on a different port or host, update both `.env` and the
URI string in `backend/app/api/auth.py` (search for `redirect_uri`).

Also verify the **OAuth consent screen has both your personal email and
`johnny-bot-...@gmail.com` in the Test users list**. While the app is in
"Testing" mode, only listed users may sign in.

### "access_denied" in OAuth flow

Either the bot account is not in the Test users list, or the scopes you ticked
on the consent screen don't include `calendar.readonly` and
`calendar.events.readonly`. Re-open the consent screen
(<https://console.cloud.google.com/apis/credentials/consent>) and add both.

### Container OOM (out of memory) — meet-worker, vLLM, or Ollama

Symptoms: container exits with code 137; `docker logs` shows the process
killed; sessions fail to start. Apple Silicon Macs running Docker Desktop
default to 8 GB; raise it:

- **Docker Desktop → Settings → Resources → Memory** → 12 GB or more
  (16 GB if you run Ollama Q4 7B alongside the stack)
- Linux: increase swap, or run a smaller Whisper model (`base.en` instead of
  `large-v3`) and a smaller LLM (`qwen2.5:3b` instead of `7b`)

### Bot fails to join Meet / "Join request denied"

- The Meet host has restricted joining to people in the calendar invite. Make
  sure the bot account is invited to the calendar event.
- For Workspace organisations, the host may require manual admit. Have the
  user-account participant admit the bot from the Meet UI.
- Cookies may be stale. Disconnect and reconnect the bot account from
  <http://localhost:5173/settings>.

### Frontend can't reach the API

```bash
curl -i http://localhost:8000/health
docker compose logs api --tail 50
```

The frontend reads `VITE_API_BASE` (default `http://localhost:8000`). If you
remap ports, set it in `frontend/.env`.

### Local LLM works in `curl` but the provider Test fails

The provider connects from *inside* the Compose network, so `localhost` from
the container is **not** your host. Use `host.docker.internal` (Mac/Windows)
or the host's LAN IP (Linux). Verify from inside the api container:

```bash
docker compose exec api curl http://host.docker.internal:11434/api/tags
```

If that fails on Linux, add to the api/worker services in
`docker-compose.yml`:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

### Calendar events do not appear

```bash
docker compose exec worker tail -n 50 /var/lib/johnny/worker/heartbeat
docker compose logs worker --tail 100 | grep -i calendar
```

If the worker has no recent heartbeat, restart: `docker compose restart worker`.
The polling cadence is `JOHNNY_CALENDAR_POLL_INTERVAL_SECONDS` (default 300s).
Click **Refresh** on the Calendar page for an on-demand sync.

---

## Where to go next

- Switch any meeting to **Approval required** mode to exercise router decisions
  and Piper TTS via browser push approval
- Add an additional Whisper model (`medium.en`) and an additional Piper voice
  in the Providers UI — both can sit side by side; only the one you Activate
  is used by the next session
- Read the PRD: `tasks/prd-johnny-google-meet-ai-meeting-bot.md`
- Backend internals & quality gates: `README.md`
