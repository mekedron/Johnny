#!/usr/bin/env bash
# start-sidecars.sh — umbrella launcher for every Johnny sidecar.
#
# Wraps the per-provider scripts/start-<provider>-sidecar.sh launchers behind
# one entry point so ./run.sh can boot every available sidecar with a single
# call, and ./stop.sh can drain them before tearing the Docker stack down.
#
# COMMANDS
#   ./scripts/start-sidecars.sh start      Start every enabled sidecar (idempotent).
#   ./scripts/start-sidecars.sh stop       Stop every running sidecar.
#   ./scripts/start-sidecars.sh restart    stop + start.
#   ./scripts/start-sidecars.sh status     One line per sidecar:
#                                           running | stopped | disabled | unavailable.
#   ./scripts/start-sidecars.sh --help     This help.
#
# ENV
#   JOHNNY_DISABLED_SIDECARS  Comma-separated sidecar keys to skip (default: none).
#                             Keys: <provider>-<backend>, e.g. parakeet-coreml,
#                             kokoro-mlx. Unknown keys are warned about, not fatal.
#   JOHNNY_SIDECAR_LOG_DIR    Where each launcher writes .log/.pid (default: .validation/).
#
# A missing toolchain (no swift / no uv) makes that one sidecar SKIPPED, never a
# hard failure — the rest still come up. This script contains no per-provider
# logic beyond globbing which launcher scripts exist; every backend-specific
# decision lives in the per-provider launcher.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
JOHNNY_DISABLED_SIDECARS="${JOHNNY_DISABLED_SIDECARS:-}"

log() { echo "[start-sidecars] $*"; }
err() { echo "[start-sidecars] $*" >&2; }

# Every per-provider launcher: scripts/start-<provider>-sidecar.sh.
launchers() {
    local f found=1
    for f in "${SCRIPTS_DIR}"/start-*-sidecar.sh; do
        [ -f "$f" ] || continue
        echo "$f"
        found=0
    done
    return $found
}

provider_of() {
    local base; base="$(basename "$1")"; base="${base#start-}"; echo "${base%-sidecar.sh}"
}

# Membership test against the comma-separated JOHNNY_DISABLED_SIDECARS list.
is_disabled() {
    local key="$1" d rest="$JOHNNY_DISABLED_SIDECARS"
    while [ -n "$rest" ]; do
        d="${rest%%,*}"
        case "$rest" in *,*) rest="${rest#*,}" ;; *) rest="" ;; esac
        d="$(echo "$d" | tr -d '[:space:]')"
        [ "$d" = "$key" ] && return 0
    done
    return 1
}

# Print every known key (provider-backend), one per line.
all_keys() {
    local launcher provider backend
    while IFS= read -r launcher; do
        provider="$(provider_of "$launcher")"
        while IFS= read -r backend; do
            [ -n "$backend" ] && echo "${provider}-${backend}"
        done < <("$launcher" backends 2>/dev/null)
    done < <(launchers)
}

# Warn about disabled keys that match no real sidecar (forward-compatible:
# a key for a sidecar that has not shipped yet is a warning, not an error).
warn_unknown_disabled() {
    local known d rest="$JOHNNY_DISABLED_SIDECARS"
    [ -n "$rest" ] || return 0
    known="$(all_keys)"
    while [ -n "$rest" ]; do
        d="${rest%%,*}"
        case "$rest" in *,*) rest="${rest#*,}" ;; *) rest="" ;; esac
        d="$(echo "$d" | tr -d '[:space:]')"
        [ -n "$d" ] || continue
        echo "$known" | grep -qx "$d" || err "warning: JOHNNY_DISABLED_SIDECARS lists unknown sidecar '$d' (ignored)"
    done
}

# rc -> human outcome word for the start summary.
outcome_word() {
    case "$1" in
        0) echo "ok" ;;
        3) echo "SKIPPED (toolchain unavailable)" ;;
        4) echo "PORT CONFLICT" ;;
        *) echo "FAILED" ;;
    esac
}

cmd_start() {
    warn_unknown_disabled
    local launcher provider backend key rc port worst=0
    local -a summary
    while IFS= read -r launcher; do
        provider="$(provider_of "$launcher")"
        while IFS= read -r backend; do
            [ -n "$backend" ] || continue
            key="${provider}-${backend}"
            if is_disabled "$key"; then
                summary+=( "  ${key} DISABLED" )
                continue
            fi
            "$launcher" start "$backend"; rc=$?
            if [ "$rc" -eq 0 ]; then
                port="$("$launcher" port "$backend" 2>/dev/null)"
                summary+=( "  ${key} :${port} ok" )
            else
                [ "$rc" -eq 1 ] && worst=1
                summary+=( "  ${key} $(outcome_word "$rc")" )
            fi
        done < <("$launcher" backends 2>/dev/null)
    done < <(launchers)
    log "Sidecar start summary:"
    local line
    for line in "${summary[@]}"; do echo "$line"; done
    return "$worst"
}

cmd_stop() {
    local launcher
    while IFS= read -r launcher; do
        "$launcher" stop || true
    done < <(launchers)
}

cmd_status() {
    warn_unknown_disabled
    local launcher provider backend key word port
    log "Sidecar status:"
    while IFS= read -r launcher; do
        provider="$(provider_of "$launcher")"
        while IFS= read -r backend; do
            [ -n "$backend" ] || continue
            key="${provider}-${backend}"
            if is_disabled "$key"; then
                echo "  ${key}: disabled"
                continue
            fi
            word="$("$launcher" probe "$backend" 2>/dev/null)"
            if [ "$word" = "running" ]; then
                port="$("$launcher" port "$backend" 2>/dev/null)"
                echo "  ${key}: running on :${port}"
            else
                echo "  ${key}: ${word:-unknown}"
            fi
        done < <("$launcher" backends 2>/dev/null)
    done < <(launchers)
}

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
    if ! launchers >/dev/null; then
        err "no per-provider launchers found under ${SCRIPTS_DIR} (start-*-sidecar.sh)"
        exit 1
    fi
    case "${1:-}" in
        start) cmd_start ;;
        stop) cmd_stop ;;
        restart) cmd_stop; cmd_start ;;
        status) cmd_status ;;
        -h|--help|help) usage ;;
        "") usage >&2; exit 2 ;;
        *) err "unknown command: $1"; usage >&2; exit 2 ;;
    esac
}

main "$@"
