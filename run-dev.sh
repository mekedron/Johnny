#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Thin wrapper around ./run.sh that layers docker-compose.dev.yml on top
# of the base docker-compose.yml. The dev overlay bind-mounts source
# into the api / worker / frontend / migrate containers and replaces the
# api+worker commands with watcher-aware variants so edits on the host
# trigger hot reload without an image rebuild.
#
# Setting COMPOSE_FILE here also suppresses the default auto-load of
# `docker-compose.override.yml` so composition is fully explicit.
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml

./run.sh "$@"

cat <<'EOF' >&2

[run-dev.sh] Hot-reload mode is ON:
[run-dev.sh]   Frontend:  vite HMR — saves to ./frontend reload the browser
[run-dev.sh]   API:       uvicorn --reload — saves to ./backend/app or ./backend/johnny restart the api
[run-dev.sh]   Worker:    watchfiles — saves to ./backend/app or ./backend/johnny restart the worker
[run-dev.sh] No image rebuild needed for code changes.
[run-dev.sh] Dependency changes (pyproject.toml / package.json) still require ./run-dev.sh to rerun the build step.
[run-dev.sh] If saves are not picked up on macOS, uncomment the polling env vars in docker-compose.dev.yml.
EOF
