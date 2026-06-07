#!/usr/bin/env bash
# Start a Kokoro TTS sidecar process on the host (outside Docker).
#
# Usage:
#   ./scripts/start-kokoro-sidecar.sh mlx       # Apple MLX (Metal GPU), :8772
#   ./scripts/start-kokoro-sidecar.sh http      # generic Kokoro (CPU/CUDA), :8773
#   ./scripts/start-kokoro-sidecar.sh status    # show which sidecars are up
#   ./scripts/start-kokoro-sidecar.sh stop      # stop any running sidecars
#
# The api container talks to whichever you start via
# http://host.docker.internal:<port>. Default ports: 8772 (mlx), 8773 (http).
# Pick the runtime in Settings -> Providers -> Kokoro -> Runtime.
#
# Why two backends — mlx runs Kokoro on Apple's Metal GPU via mlx-audio; http
# runs the upstream model (CPU, or a CUDA GPU on a Linux host). See
# sidecars/kokoro-{mlx,http}/README.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MLX_DIR="${REPO_ROOT}/sidecars/kokoro-mlx"
HTTP_DIR="${REPO_ROOT}/sidecars/kokoro-http"
MLX_PORT="${KOKORO_MLX_PORT:-8772}"
HTTP_PORT="${KOKORO_HTTP_PORT:-8773}"
MLX_LOG="${REPO_ROOT}/.validation/kokoro-mlx-sidecar.log"
HTTP_LOG="${REPO_ROOT}/.validation/kokoro-http-sidecar.log"
MLX_PID="${REPO_ROOT}/.validation/kokoro-mlx-sidecar.pid"
HTTP_PID="${REPO_ROOT}/.validation/kokoro-http-sidecar.pid"

mkdir -p "${REPO_ROOT}/.validation"

usage() {
    sed -n '3,18p' "$0"
    exit 1
}

start_one() {
    local label="$1"
    local dir="$2"
    local port="$3"
    local log="$4"
    local pidfile="$5"
    local port_env="$6"

    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "[start-kokoro-sidecar] $label sidecar already running (pid $(cat "$pidfile")) on :$port"
        return 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
        echo "[start-kokoro-sidecar] uv not found. Install with: brew install uv" >&2
        exit 1
    fi
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "[start-kokoro-sidecar] port $port already in use by another process — stop it first" >&2
        exit 1
    fi
    echo "[start-kokoro-sidecar] preparing $label sidecar venv at $dir/.venv ..."
    (
        cd "$dir"
        if [[ ! -d .venv ]]; then
            uv venv --python 3.12
        fi
        # `uv pip install -e .` reads pyproject.toml and installs the sidecar's
        # deps (mlx-audio or kokoro, plus fastapi/uvicorn/numpy) into the venv.
        uv pip install -e . >> "$log" 2>&1
    )
    echo "[start-kokoro-sidecar] launching $label sidecar on :$port (log: $log)"
    (
        cd "$dir"
        env "$port_env=$port" \
        nohup .venv/bin/python server.py >> "$log" 2>&1 &
        echo $! > "$pidfile"
    )
    sleep 1
    echo "[start-kokoro-sidecar] $label sidecar pid $(cat "$pidfile") — first load downloads ~330 MB + can take ~30 s, then health: curl http://localhost:$port/health"
}

stop_one() {
    local label="$1"
    local pidfile="$2"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        kill "$(cat "$pidfile")"
        rm -f "$pidfile"
        echo "[start-kokoro-sidecar] stopped $label sidecar"
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
        start_one "mlx" "$MLX_DIR" "$MLX_PORT" "$MLX_LOG" "$MLX_PID" "KOKORO_MLX_PORT"
        ;;
    http)
        start_one "http" "$HTTP_DIR" "$HTTP_PORT" "$HTTP_LOG" "$HTTP_PID" "KOKORO_HTTP_PORT"
        ;;
    stop)
        stop_one "mlx" "$MLX_PID"
        stop_one "http" "$HTTP_PID"
        ;;
    status)
        echo "[start-kokoro-sidecar] Kokoro sidecar status:"
        status_one "mlx" "$MLX_PID" "$MLX_PORT"
        status_one "http" "$HTTP_PID" "$HTTP_PORT"
        ;;
    "")
        usage
        ;;
    *)
        echo "[start-kokoro-sidecar] unknown command: $1" >&2
        usage
        ;;
esac
