#!/usr/bin/env bash
# Bot sign-in container entrypoint (Johnny-105).
#
# Brings up Xvfb (virtual X server), x11vnc (VNC over TCP), and
# websockify (WebSocket → TCP bridge) so the API can proxy a noVNC
# session from the user's browser to Chromium running inside this
# container. Once the A/V stack is ready, exec's into the configured
# CMD (default: the supervisor at ``johnny.bot_signin.supervisor``).
#
# Signals propagate cleanly because the supervisor is exec'd as PID 1
# of the entrypoint shell (so SIGTERM from ``docker stop`` reaches the
# Python process). Helper daemons are bash-managed and killed via the
# trap if the supervisor exits before them.

set -euo pipefail

DISPLAY_NUM="${JOHNNY_BOT_SIGNIN_DISPLAY:-99}"
DISPLAY=":${DISPLAY_NUM}"
export DISPLAY

VNC_PORT="${JOHNNY_BOT_SIGNIN_VNC_PORT:-5900}"
WEBSOCKIFY_PORT="${JOHNNY_BOT_SIGNIN_WEBSOCKIFY_PORT:-6080}"
SCREEN_GEOMETRY="${JOHNNY_BOT_SIGNIN_GEOMETRY:-1280x720x24}"

# Background PIDs we manage. Tracked so the trap can clean up if the
# supervisor exits or the container is signalled.
declare -a CHILD_PIDS=()

cleanup() {
    for pid in "${CHILD_PIDS[@]}"; do
        if [ -n "${pid:-}" ] && kill -0 "${pid}" 2>/dev/null; then
            kill -TERM "${pid}" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT INT TERM

echo "[bot-signin] starting Xvfb on ${DISPLAY} (${SCREEN_GEOMETRY})"
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &
CHILD_PIDS+=("$!")

# Wait for Xvfb to be ready. Without this Chromium can race the X server
# and crash before the first frame is rendered.
for _ in $(seq 1 50); do
    if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    echo "[bot-signin] Xvfb failed to start on ${DISPLAY}" >&2
    exit 2
fi
echo "[bot-signin] Xvfb is ready"

echo "[bot-signin] starting x11vnc on TCP ${VNC_PORT}"
# -nopw: no VNC password (the API gates access via an HMAC-signed token
#        on the WebSocket proxy — the VNC port itself is only reachable
#        from inside the docker network).
# -forever: keep accepting clients across disconnects.
# -shared: tolerate concurrent viewers (mostly defensive — the API only
#          opens one connection per session).
# -localhost: bind to 127.0.0.1 so the port is unreachable across the
#             docker network; the websockify proxy on the same loopback
#             is the only way in.
# -quiet: keep stdout focused on errors.
x11vnc \
    -display "${DISPLAY}" \
    -rfbport "${VNC_PORT}" \
    -localhost \
    -nopw \
    -forever \
    -shared \
    -quiet \
    -bg \
    -o /tmp/x11vnc.log \
    -auth /dev/null

echo "[bot-signin] starting websockify on TCP ${WEBSOCKIFY_PORT} -> 127.0.0.1:${VNC_PORT}"
websockify --verbose "${WEBSOCKIFY_PORT}" "127.0.0.1:${VNC_PORT}" &
CHILD_PIDS+=("$!")

# Give websockify a moment to bind. Cheap; the supervisor's Chromium
# launch dominates total startup time anyway.
sleep 0.3

echo "[bot-signin] exec into supervisor: $*"
exec "$@"
