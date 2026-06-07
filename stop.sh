#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Per-session meet-worker containers are spawned by the api via the Docker
# SDK, so they sit outside compose's lifecycle and `down` would leave them
# orphaned. Stop any that are still running so they get a SIGTERM and a
# chance to leave the Google Meet room cleanly before being killed.
running=$(docker ps -q --filter "name=meet-worker-session-")
if [[ -n "$running" ]]; then
  docker stop $running
fi

# Even Exited per-session containers still pin the named volumes they
# mounted (`johnny_google_auth_state` is the usual culprit), so the
# `down -v` below would silently leave those volumes stranded. Remove
# every meet-worker-session container — running just-stopped plus any
# exited from prior crashes — before tearing the stack down.
all_sessions=$(docker ps -aq --filter "name=meet-worker-session-")
if [[ -n "$all_sessions" ]]; then
  docker rm $all_sessions
fi

# Stop every host sidecar (Parakeet / Piper / Kokoro) BEFORE tearing the
# Docker stack down, so an in-flight synthesis/transcription call drains into
# a clean "sidecar stopped" error instead of hanging against a half-torn-down
# api. Idempotent: sidecars that are not running are a no-op.
"$(dirname "$0")/scripts/start-sidecars.sh" stop || true

# `-v` wipes every compose-declared named volume (postgres_data,
# redis_data, google_auth_state). The user has opted into a full factory
# reset on every stop — beads issues, provider configs, session history
# and cached Google OAuth cookies are all gone after this returns.
docker compose down -v "$@"

# Compose only manages volumes it declares. Older installs kept Piper
# voices and Whisper / Parakeet weights in named volumes; run.sh moved
# them to host bind mounts under ~/.johnny. Sweep the legacy names so a
# stale install does not silently keep consuming disk after a reset.
for legacy in johnny_piper_models johnny_whisper_models johnny_parakeet_models; do
  if docker volume inspect "$legacy" >/dev/null 2>&1; then
    docker volume rm "$legacy"
  fi
done

# The frontend is served by the compose `frontend` service (vite dev in
# a container, ports 5173:5173). If someone — usually a developer in a
# hurry — also ran `pnpm dev` from ./frontend on the host, that process
# survives terminal close (PPID becomes 1) and quietly steals port 5173
# from the dockerized frontend on the next `./run.sh`. Compose down has
# already released its own port forwarder, so anything still listening
# here is a host process and is safe to terminate.
for pid in $(lsof -nP -t -iTCP:5173 -sTCP:LISTEN 2>/dev/null || true); do
  cmd=$(ps -p "$pid" -o comm= 2>/dev/null || true)
  case "$cmd" in
    com.docker.*|*vpnkit*|*docker-proxy*) ;;
    *) kill "$pid" 2>/dev/null || true ;;
  esac
done
