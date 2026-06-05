#!/usr/bin/env bash
# meet-worker container entrypoint.
#
# Starts Xvfb (virtual X server), then PulseAudio with a virtual sink
# and a virtual source, then execs the user-supplied command (defaults
# to the selfcheck). Subsequent stories (Playwright join, audio bridge)
# rely on:
#   * DISPLAY=:99 reachable
#   * sink   "$JOHNNY_SINK_NAME"   capturing browser audio
#   * source "$JOHNNY_SOURCE_NAME" feeding browser microphone

set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
export DISPLAY

JOHNNY_SINK_NAME="${JOHNNY_SINK_NAME:-johnny_speaker}"
JOHNNY_SOURCE_NAME="${JOHNNY_SOURCE_NAME:-johnny_mic}"

# PulseAudio refuses to run without a writable runtime dir.
XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-johnny}"
export XDG_RUNTIME_DIR
mkdir -p "$XDG_RUNTIME_DIR"
chmod 0700 "$XDG_RUNTIME_DIR"

log() {
    printf '[entrypoint] %s\n' "$*" >&2
}

cleanup() {
    log "shutting down"
    if [[ -n "${PULSE_PID:-}" ]] && kill -0 "$PULSE_PID" 2>/dev/null; then
        kill "$PULSE_PID" 2>/dev/null || true
    fi
    if [[ -n "${XVFB_PID:-}" ]] && kill -0 "$XVFB_PID" 2>/dev/null; then
        kill "$XVFB_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# --- Xvfb ---------------------------------------------------------------
log "starting Xvfb on $DISPLAY"
Xvfb "$DISPLAY" -screen 0 1280x720x24 -nolisten tcp &
XVFB_PID=$!

# Wait for the display to be ready (xdpyinfo polls the X server).
for _ in $(seq 1 50); do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    log "ERROR: Xvfb did not become ready"
    exit 1
fi
log "Xvfb ready (pid=$XVFB_PID)"

# --- PulseAudio ---------------------------------------------------------
log "starting PulseAudio"
# --disallow-exit / --exit-idle-time=-1: keep daemon up even with no clients.
pulseaudio \
    --daemonize=no \
    --disallow-exit \
    --exit-idle-time=-1 \
    --log-target=stderr \
    --log-level=warn \
    >/tmp/pulseaudio.log 2>&1 &
PULSE_PID=$!

# Wait for pactl to talk to the daemon.
for _ in $(seq 1 50); do
    if pactl info >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
if ! pactl info >/dev/null 2>&1; then
    log "ERROR: PulseAudio did not become ready; recent log:"
    tail -n 40 /tmp/pulseaudio.log >&2 || true
    exit 1
fi
log "PulseAudio ready (pid=$PULSE_PID)"

# --- Virtual sink (browser plays here; we capture from .monitor) -------
pactl load-module module-null-sink \
    sink_name="$JOHNNY_SINK_NAME" \
    sink_properties="device.description=Johnny_Speaker" \
    >/dev/null
log "loaded null sink: $JOHNNY_SINK_NAME"

# --- Virtual source (browser reads as mic; we feed it via the loopback sink) -
# Pattern: a second null sink whose monitor we remap into a source the
# browser sees as a real microphone. The remap step gives us a stable
# source name independent of the loopback sink's monitor naming.
pactl load-module module-null-sink \
    sink_name="${JOHNNY_SOURCE_NAME}_loopback" \
    sink_properties="device.description=Johnny_Mic_Loopback" \
    >/dev/null
pactl load-module module-remap-source \
    source_name="$JOHNNY_SOURCE_NAME" \
    master="${JOHNNY_SOURCE_NAME}_loopback.monitor" \
    source_properties="device.description=Johnny_Mic" \
    >/dev/null
log "loaded remap source: $JOHNNY_SOURCE_NAME"

# Make our virtual devices the system defaults so Chromium picks them up
# automatically when it queries the OS for default audio in/out.
pactl set-default-sink "$JOHNNY_SINK_NAME"
pactl set-default-source "$JOHNNY_SOURCE_NAME"
log "set defaults; A/V environment ready"

exec "$@"
