#!/usr/bin/env bash
# Start a persistent Chrome instance with remote debugging enabled so that
# chrome-devtools-mcp can attach via --browserUrl=http://127.0.0.1:9222.
# This lets every Claude Code session and sub-agent share the same profile.
#
# Idempotent: re-running while the proper Chrome is already up is a no-op.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_DIR="${PROJECT_ROOT}/.chrome-profile"
DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}"
CHROME_BIN="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
# Optional extra Chrome flags, e.g. fake-media flags for scripted voice runs
# (docs/LATENCY.md methodology):
#   CHROME_EXTRA_FLAGS="--use-fake-device-for-media-stream --use-fake-ui-for-media-stream --use-file-for-fake-audio-capture=/path/to.wav --disable-features=AudioServiceSandbox" $0
# AudioServiceSandbox must be disabled on macOS or the sandboxed audio service
# silently fails to read the capture WAV and the fake mic stays silent
# (verified 2026-06-10). The WAV restarts (and loops) per getUserMedia stream.
# Note: flags only apply at launch — if Chrome is already running, pkill it
# first (see the locked-profile hint below), then re-run with the env set.
# Restore by pkill + re-running with the env unset.
CHROME_EXTRA_FLAGS="${CHROME_EXTRA_FLAGS:-}"

port_up=false
profile_in_use=false

if curl -sf -m 1 "http://127.0.0.1:${DEBUG_PORT}/json/version" >/dev/null 2>&1; then
  port_up=true
fi

if pgrep -f "user-data-dir=${PROFILE_DIR}" >/dev/null; then
  profile_in_use=true
fi

# Case 1: the proper Chrome (our profile + DevTools port) is already up. No-op.
if $port_up && $profile_in_use; then
  version="$(curl -sf -m 1 "http://127.0.0.1:${DEBUG_PORT}/json/version" | sed -n 's/.*"Browser": *"\([^"]*\)".*/\1/p')"
  echo "Chrome is already running properly:"
  echo "  DevTools : http://127.0.0.1:${DEBUG_PORT}"
  echo "  Profile  : ${PROFILE_DIR}"
  [[ -n "${version}" ]] && echo "  Version  : ${version}"
  exit 0
fi

# Case 2: something else is on the DevTools port but our profile is not in use.
if $port_up && ! $profile_in_use; then
  cat >&2 <<EOF
Error: Port ${DEBUG_PORT} is already serving a different Chrome instance
(not the one using ${PROFILE_DIR}).

Stop the other Chrome first, or set CHROME_DEBUG_PORT to a free port:

  CHROME_DEBUG_PORT=9333 $0

(remember to update .mcp.json --browserUrl to match)
EOF
  exit 1
fi

# Case 3: our profile is locked by a Chrome that isn't exposing the DevTools
# port. Almost always the instance launched by the old chrome-devtools-mcp
# config (--remote-debugging-pipe instead of --remote-debugging-port).
if ! $port_up && $profile_in_use; then
  cat >&2 <<EOF
Error: A Chrome is already using ${PROFILE_DIR} but is not exposing
DevTools on port ${DEBUG_PORT}. This is likely the instance the old
chrome-devtools-mcp config launched via a pipe connection.

Close it, then re-run this script:

  pkill -f "user-data-dir=${PROFILE_DIR}"
  $0
EOF
  exit 1
fi

# Case 4: nothing is running. Launch it.
if [[ ! -x "${CHROME_BIN}" ]]; then
  echo "Error: Chrome not found at ${CHROME_BIN}" >&2
  echo "Override with CHROME_BIN=/path/to/chrome $0" >&2
  exit 1
fi

# shellcheck disable=SC2086 — CHROME_EXTRA_FLAGS is intentionally word-split.
nohup "${CHROME_BIN}" \
  --user-data-dir="${PROFILE_DIR}" \
  --remote-debugging-port="${DEBUG_PORT}" \
  --remote-debugging-address=127.0.0.1 \
  --no-first-run \
  --no-default-browser-check \
  ${CHROME_EXTRA_FLAGS} \
  >/dev/null 2>&1 &

disown

for _ in {1..40}; do
  if curl -sf -m 1 "http://127.0.0.1:${DEBUG_PORT}/json/version" >/dev/null 2>&1; then
    echo "Chrome started:"
    echo "  DevTools : http://127.0.0.1:${DEBUG_PORT}"
    echo "  Profile  : ${PROFILE_DIR}"
    exit 0
  fi
  sleep 0.25
done

echo "Error: Chrome was launched but port ${DEBUG_PORT} did not become available in 10s." >&2
exit 1

