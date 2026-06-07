#!/usr/bin/env bash
# Start a Parakeet sidecar process on the macOS host (outside Docker).
#
# Usage:
#   ./scripts/start-parakeet-sidecar.sh mlx        # Apple MLX (Metal GPU)
#   ./scripts/start-parakeet-sidecar.sh coreml     # FluidAudio CoreML + ANE
#   ./scripts/start-parakeet-sidecar.sh status     # show which sidecars are up
#   ./scripts/start-parakeet-sidecar.sh stop       # stop any running sidecars
#
# The api container talks to whichever you start via
# http://host.docker.internal:<port>. Default ports: 8765 (mlx), 8766 (coreml).
# Pick the runtime in Settings → Providers → NVIDIA Parakeet → Runtime.
#
# Why two backends — see sidecars/parakeet-{mlx,coreml}/README.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MLX_DIR="${REPO_ROOT}/sidecars/parakeet-mlx"
COREML_DIR="${REPO_ROOT}/sidecars/parakeet-coreml"
MLX_PORT="${PARAKEET_MLX_PORT:-8765}"
COREML_PORT="${PARAKEET_COREML_PORT:-8766}"
MLX_LOG="${REPO_ROOT}/.validation/parakeet-mlx-sidecar.log"
COREML_LOG="${REPO_ROOT}/.validation/parakeet-coreml-sidecar.log"
MLX_PID="${REPO_ROOT}/.validation/parakeet-mlx-sidecar.pid"
COREML_PID="${REPO_ROOT}/.validation/parakeet-coreml-sidecar.pid"

mkdir -p "${REPO_ROOT}/.validation"

usage() {
    sed -n '3,16p' "$0"
    exit 1
}

start_mlx() {
    if [[ -f "$MLX_PID" ]] && kill -0 "$(cat "$MLX_PID")" 2>/dev/null; then
        echo "[start-parakeet-sidecar] mlx sidecar already running (pid $(cat "$MLX_PID")) on :$MLX_PORT"
        return 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
        echo "[start-parakeet-sidecar] uv not found. Install with: brew install uv" >&2
        exit 1
    fi
    if ! lsof -nP -iTCP:"$MLX_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        : # port free, good
    else
        echo "[start-parakeet-sidecar] port $MLX_PORT already in use by another process — stop it first" >&2
        exit 1
    fi
    echo "[start-parakeet-sidecar] preparing mlx sidecar venv at $MLX_DIR/.venv ..."
    (
        cd "$MLX_DIR"
        if [[ ! -d .venv ]]; then
            uv venv --python 3.12
        fi
        # `uv pip install -e .` reads pyproject.toml and installs the
        # sidecar's deps (parakeet-mlx, fastapi, uvicorn, numpy) into the venv.
        uv pip install -e . >> "$MLX_LOG" 2>&1
    )
    echo "[start-parakeet-sidecar] launching mlx sidecar on :$MLX_PORT (log: $MLX_LOG)"
    (
        cd "$MLX_DIR"
        PARAKEET_MLX_PORT="$MLX_PORT" \
        nohup .venv/bin/python server.py >> "$MLX_LOG" 2>&1 &
        echo $! > "$MLX_PID"
    )
    sleep 1
    echo "[start-parakeet-sidecar] mlx sidecar pid $(cat "$MLX_PID") — first load can take ~30 s, then health: curl http://localhost:$MLX_PORT/health"
}

start_coreml() {
    if [[ -f "$COREML_PID" ]] && kill -0 "$(cat "$COREML_PID")" 2>/dev/null; then
        echo "[start-parakeet-sidecar] coreml sidecar already running (pid $(cat "$COREML_PID")) on :$COREML_PORT"
        return 0
    fi
    if ! command -v swift >/dev/null 2>&1; then
        echo "[start-parakeet-sidecar] swift not found. Install Xcode command-line tools: xcode-select --install" >&2
        exit 1
    fi
    if lsof -nP -iTCP:"$COREML_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "[start-parakeet-sidecar] port $COREML_PORT already in use by another process — stop it first" >&2
        exit 1
    fi
    echo "[start-parakeet-sidecar] building coreml sidecar (first build downloads FluidAudio + Hummingbird, ~2 min) ..."
    (
        cd "$COREML_DIR"
        swift build -c release >> "$COREML_LOG" 2>&1
    )
    echo "[start-parakeet-sidecar] launching coreml sidecar on :$COREML_PORT (log: $COREML_LOG)"
    (
        cd "$COREML_DIR"
        PARAKEET_COREML_PORT="$COREML_PORT" \
        nohup .build/release/parakeet-coreml-sidecar >> "$COREML_LOG" 2>&1 &
        echo $! > "$COREML_PID"
    )
    sleep 1
    echo "[start-parakeet-sidecar] coreml sidecar pid $(cat "$COREML_PID") — first load downloads ~150 MB of CoreML models, then health: curl http://localhost:$COREML_PORT/health"
}

stop_one() {
    local label="$1"
    local pidfile="$2"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        kill "$(cat "$pidfile")"
        rm -f "$pidfile"
        echo "[start-parakeet-sidecar] stopped $label sidecar"
    fi
}

status_one() {
    local label="$1"
    local pidfile="$2"
    local port="$3"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "  $label: running (pid $(cat "$pidfile")) on :$port"
    else
        echo "  $label: stopped"
    fi
}

case "${1:-}" in
    mlx)
        start_mlx
        ;;
    coreml)
        start_coreml
        ;;
    stop)
        stop_one "mlx" "$MLX_PID"
        stop_one "coreml" "$COREML_PID"
        ;;
    status)
        echo "[start-parakeet-sidecar] Parakeet sidecar status:"
        status_one "mlx" "$MLX_PID" "$MLX_PORT"
        status_one "coreml" "$COREML_PID" "$COREML_PORT"
        ;;
    "")
        usage
        ;;
    *)
        echo "[start-parakeet-sidecar] unknown command: $1" >&2
        usage
        ;;
esac
