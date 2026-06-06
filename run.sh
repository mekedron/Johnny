#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

# Piper voices, Whisper, and Parakeet STT models live in host bind
# mounts under ~/.johnny so the user can `ls` them, drop files in
# manually, and not lose them across `docker compose down -v` resets.
# Create idempotently on first boot so the very first run does not
# fail mounting a missing directory.
mkdir -p \
  "${HOME}/.johnny/piper-models" \
  "${HOME}/.johnny/whisper-models" \
  "${HOME}/.johnny/parakeet-models" \
  "${HOME}/.johnny/parakeet-packages"

# Legacy migration hint: older installs kept the models in named Docker
# volumes (johnny_piper_models / johnny_whisper_models). Detect them and
# print a one-line ``docker cp``-style migration command so the user
# does not silently lose previously downloaded voices/weights.
if command -v docker >/dev/null 2>&1; then
  for legacy in johnny_piper_models johnny_whisper_models johnny_parakeet_models; do
    if docker volume inspect "${legacy}" >/dev/null 2>&1; then
      case "${legacy}" in
        johnny_piper_models) host_target="${HOME}/.johnny/piper-models" ;;
        johnny_whisper_models) host_target="${HOME}/.johnny/whisper-models" ;;
        johnny_parakeet_models) host_target="${HOME}/.johnny/parakeet-models" ;;
      esac
      cat <<EOF >&2
[run.sh] Detected legacy Docker volume "${legacy}".
[run.sh] To migrate previously downloaded files into the new host dir:
[run.sh]   docker run --rm -v ${legacy}:/from -v ${host_target}:/to alpine cp -an /from/. /to/
[run.sh] After verifying the files arrived, remove the legacy volume:
[run.sh]   docker volume rm ${legacy}
EOF
    fi
  done
fi

# The frontend MUST come from the compose `frontend` service — never from
# a host-side `pnpm dev` in ./frontend. A stray host vite survives terminal
# close (PPID becomes 1) and silently steals port 5173 from the dockerized
# frontend, so `docker compose up` then fails to bind. Sweep any host
# process still holding 5173 before bringing the stack up. Compose's own
# port forwarder (docker-proxy / com.docker.* / vpnkit) is skipped — if
# it is listening, that just means the stack is already partly up and
# `up -d --build` below will reconcile it.
for pid in $(lsof -nP -t -iTCP:5173 -sTCP:LISTEN 2>/dev/null || true); do
  cmd=$(ps -p "$pid" -o comm= 2>/dev/null || true)
  case "$cmd" in
    com.docker.*|*vpnkit*|*docker-proxy*) ;;
    *)
      echo "[run.sh] Killing host process on :5173 (pid $pid, $cmd) — the dockerized frontend will take over." >&2
      kill "$pid" 2>/dev/null || true
      ;;
  esac
done

# meet-worker is gated behind a compose profile, so `up --build` skips it.
# Build it explicitly so per-session containers (spawned by the api via the
# Docker SDK) pick up the latest backend code.
docker compose --profile meet-worker build meet-worker

# Detached so the terminal is free after start. The user can tail logs
# explicitly when needed — keeps repeated `./run.sh` cycles from hanging
# the shell.
docker compose up -d --build "$@"

cat <<'EOF' >&2
[run.sh] Stack started in the background.
[run.sh]   Frontend:  http://localhost:5173
[run.sh]   API:       http://localhost:8000
[run.sh] Tail logs:   docker compose logs -f
[run.sh] Stop stack:  ./stop.sh
EOF
