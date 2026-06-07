#!/usr/bin/env bash
# start-parakeet-sidecar.sh — manage the Parakeet STT sidecar(s) on the host.
#
# Quick start:
#   ./scripts/start-parakeet-sidecar.sh start          # start every backend
#   ./scripts/start-parakeet-sidecar.sh start mlx      # just the MLX backend
#   ./scripts/start-parakeet-sidecar.sh status         # one line per backend
#   ./scripts/start-parakeet-sidecar.sh stop           # stop every backend
#   ./scripts/start-parakeet-sidecar.sh logs mlx       # tail one log
#   ./scripts/start-parakeet-sidecar.sh --help         # full help block
#
# The api container talks to whichever you start via
# http://host.docker.internal:<port>. Defaults: 8765 (mlx), 8766 (coreml).
# Pick the runtime in Settings -> Providers -> NVIDIA Parakeet -> Runtime.
# Why two backends — see sidecars/parakeet-{mlx,coreml}/README.md.

set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/sidecar-common.sh
. "${REPO_ROOT}/scripts/lib/sidecar-common.sh"

PROVIDER="parakeet"
PROVIDER_DESC="manage the Parakeet STT sidecar(s) on the macOS host"
PROVIDER_BACKENDS="mlx coreml"

sc_dir() {
    case "$1" in
        mlx) echo "sidecars/parakeet-mlx" ;;
        coreml) echo "sidecars/parakeet-coreml" ;;
    esac
}
sc_port_default() { case "$1" in mlx) echo 8765 ;; coreml) echo 8766 ;; esac; }
sc_kind() { case "$1" in mlx) echo python ;; coreml) echo swift ;; esac; }
sc_binary() { case "$1" in coreml) echo ".build/release/parakeet-coreml-sidecar" ;; esac; }
sc_blurb() {
    case "$1" in
        mlx) echo "Apple MLX (Metal GPU)" ;;
        coreml) echo "FluidAudio CoreML + ANE" ;;
    esac
}
sc_post_launch_hint() {
    local port; port="$(sc_port "$1")"
    case "$1" in
        mlx) echo "first load ~30 s, then: curl http://localhost:${port}/health" ;;
        coreml) echo "first load downloads ~150 MB of CoreML models, then: curl http://localhost:${port}/health" ;;
    esac
}

sc_main "$@"
