#!/usr/bin/env bash
# start-kokoro-sidecar.sh — manage the Kokoro TTS sidecar(s) on the host.
#
# Quick start:
#   ./scripts/start-kokoro-sidecar.sh start          # start every backend
#   ./scripts/start-kokoro-sidecar.sh start mlx      # just the MLX backend
#   ./scripts/start-kokoro-sidecar.sh status         # one line per backend
#   ./scripts/start-kokoro-sidecar.sh stop           # stop every backend
#   ./scripts/start-kokoro-sidecar.sh logs http      # tail one log
#   ./scripts/start-kokoro-sidecar.sh --help         # full help block
#
# The api container talks to whichever you start via
# http://host.docker.internal:<port>. Defaults: 8772 (mlx), 8773 (http).
# Pick the runtime in Settings -> Providers -> Kokoro -> Runtime.
# Why two backends — mlx runs on Apple's Metal GPU via mlx-audio; http runs the
# upstream model (CPU/CUDA). See sidecars/kokoro-{mlx,http}/README.md.

set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/sidecar-common.sh
. "${REPO_ROOT}/scripts/lib/sidecar-common.sh"

PROVIDER="kokoro"
PROVIDER_DESC="manage the Kokoro TTS sidecar(s) on the macOS host"
PROVIDER_BACKENDS="mlx http"

sc_dir() {
    case "$1" in
        mlx) echo "sidecars/kokoro-mlx" ;;
        http) echo "sidecars/kokoro-http" ;;
    esac
}
sc_port_default() { case "$1" in mlx) echo 8772 ;; http) echo 8773 ;; esac; }
sc_kind() { echo python; }
sc_blurb() {
    case "$1" in
        mlx) echo "Apple MLX (Metal GPU) via mlx-audio" ;;
        http) echo "upstream Kokoro (CPU / CUDA)" ;;
    esac
}
sc_post_launch_hint() {
    local port; port="$(sc_port "$1")"
    echo "first load downloads ~330 MB + can take ~30 s, then: curl http://localhost:${port}/health"
}

sc_main "$@"
