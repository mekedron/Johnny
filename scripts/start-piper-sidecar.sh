#!/usr/bin/env bash
# start-piper-sidecar.sh — manage the Piper TTS sidecar on the macOS host.
#
# Quick start:
#   ./scripts/start-piper-sidecar.sh start           # build venv + launch :8775
#   ./scripts/start-piper-sidecar.sh status          # is it up?
#   ./scripts/start-piper-sidecar.sh stop            # stop it
#   ./scripts/start-piper-sidecar.sh logs            # tail the log
#   ./scripts/start-piper-sidecar.sh --help          # full help block
#
# The api container talks to it via http://host.docker.internal:8775. Pick the
# runtime in Settings -> Providers -> Local Piper -> Runtime (http-sidecar).
# Why a sidecar — piper-tts 1.x has no --http server of its own; this is a thin
# FastAPI wrapper around the PiperVoice library. See sidecars/piper-http/README.md.

set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/sidecar-common.sh
. "${REPO_ROOT}/scripts/lib/sidecar-common.sh"

PROVIDER="piper"
PROVIDER_DESC="manage the Piper TTS sidecar on the macOS host"
PROVIDER_BACKENDS="http"

sc_dir() { echo "sidecars/piper-http"; }
sc_port_default() { echo 8775; }
sc_kind() { echo python; }
sc_blurb() { echo "FastAPI wrapper around the PiperVoice library"; }
sc_post_launch_hint() {
    local port; port="$(sc_port "$1")"
    echo "first voice load ~1 s, then: curl http://localhost:${port}/health"
}

sc_main "$@"
