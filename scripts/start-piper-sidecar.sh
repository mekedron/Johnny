#!/usr/bin/env bash
# Start the Piper HTTP TTS sidecar on the macOS host (outside Docker).
#
# Usage:
#   ./scripts/start-piper-sidecar.sh start    # build venv + launch on :8775
#   ./scripts/start-piper-sidecar.sh status   # show whether the sidecar is up
#   ./scripts/start-piper-sidecar.sh stop     # stop a running sidecar
#
# The api container talks to it via http://host.docker.internal:8775. Pick the
# runtime in Settings -> Providers -> Local Piper -> Runtime (http-sidecar).
#
# Why a sidecar — piper-tts 1.x has no --http server of its own; this is a thin
# FastAPI wrapper around the PiperVoice library. See sidecars/piper-http/README.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIPER_DIR="${REPO_ROOT}/sidecars/piper-http"
PIPER_PORT="${PIPER_SIDECAR_PORT:-8775}"
PIPER_LOG="${REPO_ROOT}/.validation/piper-http-sidecar.log"
PIPER_PID="${REPO_ROOT}/.validation/piper-http-sidecar.pid"

mkdir -p "${REPO_ROOT}/.validation"

usage() {
    sed -n '3,15p' "$0"
    exit 1
}

start_piper() {
    if [[ -f "$PIPER_PID" ]] && kill -0 "$(cat "$PIPER_PID")" 2>/dev/null; then
        echo "[start-piper-sidecar] sidecar already running (pid $(cat "$PIPER_PID")) on :$PIPER_PORT"
        return 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
        echo "[start-piper-sidecar] uv not found. Install with: brew install uv" >&2
        exit 1
    fi
    if lsof -nP -iTCP:"$PIPER_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "[start-piper-sidecar] port $PIPER_PORT already in use by another process — stop it first" >&2
        exit 1
    fi
    echo "[start-piper-sidecar] preparing sidecar venv at $PIPER_DIR/.venv ..."
    (
        cd "$PIPER_DIR"
        if [[ ! -d .venv ]]; then
            uv venv --python 3.12
        fi
        # `uv pip install -e .` reads pyproject.toml and installs the sidecar's
        # deps (piper-tts, fastapi, uvicorn) into the venv.
        uv pip install -e . >> "$PIPER_LOG" 2>&1
    )
    echo "[start-piper-sidecar] launching sidecar on :$PIPER_PORT (log: $PIPER_LOG)"
    (
        cd "$PIPER_DIR"
        PIPER_SIDECAR_PORT="$PIPER_PORT" \
        nohup .venv/bin/python server.py >> "$PIPER_LOG" 2>&1 &
        echo $! > "$PIPER_PID"
    )
    sleep 1
    echo "[start-piper-sidecar] sidecar pid $(cat "$PIPER_PID") — first voice load takes ~1 s, then health: curl http://localhost:$PIPER_PORT/health"
}

stop_piper() {
    if [[ -f "$PIPER_PID" ]] && kill -0 "$(cat "$PIPER_PID")" 2>/dev/null; then
        kill "$(cat "$PIPER_PID")"
        rm -f "$PIPER_PID"
        echo "[start-piper-sidecar] stopped sidecar"
    else
        echo "[start-piper-sidecar] no running sidecar to stop"
    fi
}

status_piper() {
    echo "[start-piper-sidecar] Piper sidecar status:"
    if [[ -f "$PIPER_PID" ]] && kill -0 "$(cat "$PIPER_PID")" 2>/dev/null; then
        echo "  piper-http: running (pid $(cat "$PIPER_PID")) on :$PIPER_PORT"
    else
        echo "  piper-http: stopped"
    fi
}

case "${1:-}" in
    start)
        start_piper
        ;;
    stop)
        stop_piper
        ;;
    status)
        status_piper
        ;;
    "")
        usage
        ;;
    *)
        echo "[start-piper-sidecar] unknown command: $1" >&2
        usage
        ;;
esac
