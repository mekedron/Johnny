#!/usr/bin/env bash
# start-kitten-sidecar.sh — manage the KittenTTS sidecar on the host.
#
# Quick start:
#   ./scripts/start-kitten-sidecar.sh start          # start the http backend
#   ./scripts/start-kitten-sidecar.sh status         # one line per backend
#   ./scripts/start-kitten-sidecar.sh stop           # stop the backend
#   ./scripts/start-kitten-sidecar.sh logs           # tail the log
#   ./scripts/start-kitten-sidecar.sh --help         # full help block
#
# The api container talks to it via http://host.docker.internal:8771.
# Pick the runtime in Settings -> Providers -> KittenTTS -> Runtime.
# Why a sidecar — KittenTTS is a tiny CPU-only ONNX model, so this is NOT a GPU
# path (there is no MLX/CoreML build); it just isolates synthesis in its own
# process / host, or keeps onnxruntime out of the api image. See
# sidecars/kitten-tts/README.md.

set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/sidecar-common.sh
. "${REPO_ROOT}/scripts/lib/sidecar-common.sh"

PROVIDER="kitten"
PROVIDER_DESC="manage the KittenTTS sidecar on the host"
PROVIDER_BACKENDS="http"

sc_dir() { case "$1" in http) echo "sidecars/kitten-tts" ;; esac; }
sc_port_default() { case "$1" in http) echo 8771 ;; esac; }
sc_kind() { echo python; }
sc_blurb() { case "$1" in http) echo "KittenTTS (CPU, ONNX)" ;; esac; }
sc_post_launch_hint() {
    local port; port="$(sc_port "$1")"
    echo "first load downloads the model (~tens of MB), then: curl http://localhost:${port}/health"
}

sc_main "$@"
