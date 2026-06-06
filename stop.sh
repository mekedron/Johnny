#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Per-session meet-worker containers are spawned by the api via the Docker
# SDK, so they sit outside compose's lifecycle and `down` would leave them
# orphaned. Stop any that are still running before tearing the stack down.
sessions=$(docker ps -q --filter "name=meet-worker-session-")
if [[ -n "$sessions" ]]; then
  docker stop $sessions
fi

exec docker compose down "$@"
