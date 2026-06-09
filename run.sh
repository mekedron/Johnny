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
  "${HOME}/.johnny/parakeet-packages" \
  "${HOME}/.johnny/kokoro-models" \
  "${HOME}/.johnny/kitten-models"

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
# frontend, so `docker compose up` then fails to bind. Sweep any such stray
# host dev-server still holding 5173 before bringing the stack up.
#
# ALLOWLIST, not denylist: only a recognized host dev-server (vite/esbuild
# run as `node`; a `pnpm`/`npm` parent) is killed. Everything else on :5173
# is LEFT ALONE — most importantly Docker Desktop's own port forwarder, which
# publishes the dockerized frontend's 5173. The earlier denylist (`com.docker.*`)
# silently missed it because macOS `ps -o comm=` returns the FULL path
# (`/Applications/Docker.app/.../com.docker.backend`), so the sweep killed
# Docker Desktop's backend and took the daemon down (bug Johnny-9ph). An
# allowlist makes that impossible: an unrecognized process is never killed —
# at worst `up -d --build` below prints a clear "address already in use".
for pid in $(lsof -nP -t -iTCP:5173 -sTCP:LISTEN 2>/dev/null || true); do
  cmd=$(ps -p "$pid" -o comm= 2>/dev/null || true)
  case "$(basename "${cmd:-/unknown}")" in
    node|vite|pnpm|npm|esbuild)
      echo "[run.sh] Killing stray host dev-server on :5173 (pid $pid, $cmd) — the dockerized frontend will take over." >&2
      kill "$pid" 2>/dev/null || true
      ;;
    *) ;;  # Docker port-forwarder or anything unrecognized — never touched.
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

# Boot every available host sidecar (Parakeet STT / Piper / Kokoro TTS) so a
# saved sidecar-runtime works without a separate manual launch. Each sidecar is
# a soft dependency: a missing toolchain (no swift / no uv) SKIPS just that one,
# never fails the stack. Opt out per-sidecar with JOHNNY_DISABLED_SIDECARS
# (comma-separated keys, e.g. parakeet-coreml,kokoro-mlx — see .env.example).
# `|| true` keeps a single sidecar failure from aborting the whole bring-up.
"$(dirname "$0")/scripts/start-sidecars.sh" start || true

cat <<'EOF' >&2
[run.sh] Stack started in the background.
[run.sh]   Frontend:     http://localhost:5173
[run.sh]   API:          http://localhost:8000
[run.sh]   Sidecars:     ./scripts/start-sidecars.sh status   (per-sidecar state)
[run.sh]   Sidecar logs: tail -f .validation/<provider>-<backend>-sidecar.log
[run.sh] Tail logs:   docker compose logs -f
[run.sh] Stop stack:  ./stop.sh
EOF
