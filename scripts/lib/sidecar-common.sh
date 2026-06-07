# shellcheck shell=bash
# Shared library for Johnny per-provider sidecar launchers.
#
# Every scripts/start-<provider>-sidecar.sh sources this file to get an
# identical command-line interface, exit-code contract, env-var convention
# and log/PID layout. The umbrella scripts/start-sidecars.sh relies on that
# uniformity to drive every launcher without per-provider branching.
#
# A sourcing launcher MUST set these before `source`-ing this file:
#   PROVIDER            provider slug, lowercase, e.g. "parakeet"
#   PROVIDER_DESC       one-line human description for the NAME help section
#   PROVIDER_BACKENDS   space-separated backend names, e.g. "mlx coreml"
#   REPO_ROOT           absolute path to the repo root
#
# ...and define these hook functions (case on the backend name):
#   sc_dir <backend>          echo the sidecar source dir (abs, or rel to REPO_ROOT)
#   sc_port_default <backend>  echo the default bind port
#   sc_kind <backend>         echo "python" (uv venv + server.py) or "swift"
#                             (swift build -c release + a built binary)
#   sc_blurb <backend>        echo a short one-line description (help text)
#   sc_binary <backend>       (swift only) echo the built binary path,
#                             relative to the sidecar dir
#   sc_post_launch_hint <backend>  (optional) echo a hint printed after launch
#
# Then call:  sc_main "$@"
#
# Exit codes (identical across every launcher):
#   0  success, or already in the requested state
#   1  generic failure (build error, model download failed, port bind failed)
#   2  bad usage (unknown command, unknown backend, conflicting flags)
#   3  toolchain unavailable (no uv, no swift) — the umbrella prints SKIPPED
#   4  sidecar already running on a different port than this invocation asked for

# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

sc_logdir() {
    printf '%s' "${JOHNNY_SIDECAR_LOG_DIR:-${REPO_ROOT}/.validation}"
}

sc_logfile() { printf '%s/%s-%s-sidecar.log' "$(sc_logdir)" "$PROVIDER" "$1"; }
sc_pidfile() { printf '%s/%s-%s-sidecar.pid' "$(sc_logdir)" "$PROVIDER" "$1"; }

# Uppercase a slug into an env-var fragment: "parakeet" -> "PARAKEET",
# "kokoro-mlx" -> "KOKORO_MLX". bash 3.2 has no ${var^^}.
sc_upper() { printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_'; }

# Env-var prefix for a backend, e.g. (parakeet, mlx) -> "PARAKEET_MLX".
sc_env_prefix() { printf '%s_%s' "$(sc_upper "$PROVIDER")" "$(sc_upper "$1")"; }

# Read env var named by $1, falling back to $2 when unset/empty. eval keeps it
# safe under `set -u` (indirect ${!name} of an unset var would abort); the name
# is always one of our own derived slugs, never user input.
sc_env_or() {
    local name="$1" fallback="$2" value
    eval "value=\${$name:-}"
    [ -n "$value" ] && printf '%s' "$value" || printf '%s' "$fallback"
}

sc_port() { sc_env_or "$(sc_env_prefix "$1")_PORT" "$(sc_port_default "$1")"; }
sc_host() { sc_env_or "$(sc_env_prefix "$1")_HOST" "127.0.0.1"; }
sc_model() { sc_env_or "$(sc_env_prefix "$1")_MODEL" ""; }

# Toolchain a backend needs, derived from its kind.
sc_toolchain() {
    case "$(sc_kind "$1")" in
        swift) printf 'swift' ;;
        *) printf 'uv' ;;
    esac
}

sc_is_backend() {
    local want="$1" b
    for b in $PROVIDER_BACKENDS; do
        [ "$b" = "$want" ] && return 0
    done
    return 1
}

# pid of a backend if its pidfile names a live process, else empty.
sc_live_pid() {
    local pidfile pid
    pidfile="$(sc_pidfile "$1")"
    [ -f "$pidfile" ] || return 1
    pid="$(cat "$pidfile" 2>/dev/null)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    printf '%s' "$pid"
}

# The TCP port a pid is currently LISTENing on (first one), else empty.
# -F n emits one `n<addr>:<port>` line per socket — robust to column drift.
sc_running_port() {
    lsof -nP -iTCP -sTCP:LISTEN -a -p "$1" -F n 2>/dev/null \
        | sed -n 's/^n.*:\([0-9][0-9]*\)$/\1/p' | head -n1
}

sc_log() { echo "[start-${PROVIDER}-sidecar] $*"; }
sc_err() { echo "[start-${PROVIDER}-sidecar] $*" >&2; }

# ---------------------------------------------------------------------------
# start / stop / status / logs, one backend at a time
# ---------------------------------------------------------------------------

# Returns: 0 ok, 1 failure, 3 toolchain missing, 4 port conflict.
sc_start_backend() {
    local b="$1"
    local dir port host model tool kind pid running_port pidfile logfile
    dir="$(sc_dir "$b")"
    case "$dir" in /*) : ;; *) dir="${REPO_ROOT}/${dir}" ;; esac
    port="$(sc_port "$b")"
    host="$(sc_host "$b")"
    model="$(sc_model "$b")"
    tool="$(sc_toolchain "$b")"
    kind="$(sc_kind "$b")"
    pidfile="$(sc_pidfile "$b")"
    logfile="$(sc_logfile "$b")"

    if pid="$(sc_live_pid "$b")"; then
        running_port="$(sc_running_port "$pid")"
        if [ -z "$running_port" ] || [ "$running_port" = "$port" ]; then
            sc_log "$b already running (pid $pid) on :${running_port:-$port}"
            return 0
        fi
        sc_err "$b already running (pid $pid) on :$running_port, not the requested :$port — stop it first"
        return 4
    fi

    if ! command -v "$tool" >/dev/null 2>&1; then
        sc_err "$b SKIPPED: $tool not on PATH ($(sc_toolchain_hint "$tool"))"
        return 3
    fi

    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        sc_err "$b: port $port already in use by another process — stop it first"
        return 1
    fi

    mkdir -p "$(sc_logdir)"

    local -a launch
    if [ "$kind" = "swift" ]; then
        sc_log "building $b sidecar (first build can take ~2 min, log: $logfile) ..."
        if ! ( cd "$dir" && swift build -c release >> "$logfile" 2>&1 ); then
            sc_err "$b: swift build failed — see $logfile"
            return 1
        fi
        launch=( "${dir}/$(sc_binary "$b")" )
    else
        sc_log "preparing $b sidecar venv at $dir/.venv (log: $logfile) ..."
        if ! (
            cd "$dir" || exit 1
            [ -d .venv ] || uv venv --python 3.12 >> "$logfile" 2>&1
            uv pip install -e . >> "$logfile" 2>&1
        ); then
            sc_err "$b: dependency install failed — see $logfile"
            return 1
        fi
        launch=( "${dir}/.venv/bin/python" "server.py" )
    fi

    local prefix; prefix="$(sc_env_prefix "$b")"
    local -a env_args
    env_args=( "${prefix}_PORT=$port" "${prefix}_HOST=$host" )
    [ -n "$model" ] && env_args+=( "${prefix}_MODEL=$model" )

    sc_log "launching $b sidecar on :$port (log: $logfile)"
    (
        cd "$dir" || exit 1
        env "${env_args[@]}" nohup "${launch[@]}" >> "$logfile" 2>&1 &
        echo $! > "$pidfile"
    )
    sleep 1
    if ! pid="$(sc_live_pid "$b")"; then
        sc_err "$b: process exited immediately — see $logfile"
        return 1
    fi
    local hint; hint="$(sc_post_launch_hint "$b" 2>/dev/null || true)"
    sc_log "$b sidecar pid $pid${hint:+ — $hint}"
    return 0
}

sc_stop_backend() {
    local b="$1" pid pidfile
    pidfile="$(sc_pidfile "$b")"
    if pid="$(sc_live_pid "$b")"; then
        kill "$pid" 2>/dev/null || true
        rm -f "$pidfile"
        sc_log "stopped $b sidecar (pid $pid)"
    else
        rm -f "$pidfile"
        sc_log "$b sidecar not running"
    fi
    return 0
}

# One machine word for the umbrella: running | stopped | unavailable.
sc_probe_backend() {
    if sc_live_pid "$1" >/dev/null; then
        printf 'running'
    elif ! command -v "$(sc_toolchain "$1")" >/dev/null 2>&1; then
        printf 'unavailable'
    else
        printf 'stopped'
    fi
}

sc_status_backend() {
    local b="$1" pid port word
    word="$(sc_probe_backend "$b")"
    case "$word" in
        running)
            pid="$(sc_live_pid "$b")"
            port="$(sc_running_port "$pid")"
            printf '  %s-%s: running (pid %s) on :%s\n' "$PROVIDER" "$b" "$pid" "${port:-$(sc_port "$b")}"
            ;;
        unavailable)
            printf '  %s-%s: unavailable (%s not installed)\n' "$PROVIDER" "$b" "$(sc_toolchain "$b")"
            ;;
        *)
            printf '  %s-%s: stopped\n' "$PROVIDER" "$b"
            ;;
    esac
}

sc_logs_backend() {
    local b f files
    files=""
    if [ -n "$1" ]; then
        f="$(sc_logfile "$1")"
        [ -f "$f" ] && files="$f"
    else
        for b in $PROVIDER_BACKENDS; do
            f="$(sc_logfile "$b")"
            [ -f "$f" ] && files="$files $f"
        done
    fi
    if [ -z "$files" ]; then
        sc_err "no log files yet under $(sc_logdir)"
        return 1
    fi
    # shellcheck disable=SC2086
    tail -n 50 -f $files
}

# Best-effort hint for a missing toolchain.
sc_toolchain_hint() {
    case "$1" in
        uv) printf 'install with: brew install uv' ;;
        swift) printf 'install Xcode command-line tools: xcode-select --install' ;;
        *) printf 'install %s' "$1" ;;
    esac
}

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

sc_help() {
    local sname b pfx
    sname="$(basename "$0")"
    cat <<EOF
NAME
    ${sname} — ${PROVIDER_DESC}

USAGE
    ./scripts/${sname} <command> [<backend>] [flags]

COMMANDS
    start [<backend>]    Start a backend (or all backends if omitted).
    stop  [<backend>]    Stop a backend (or all).
    restart [<backend>]  stop + start.
    status               One line per backend: running | stopped | disabled | unavailable.
    logs [<backend>]     Tail the matching sidecar log file.

BACKENDS
EOF
    for b in $PROVIDER_BACKENDS; do
        printf '    %-9s %s (default port: %s)\n' "$b" "$(sc_blurb "$b")" "$(sc_port_default "$b")"
    done
    printf '\nENV\n'
    for b in $PROVIDER_BACKENDS; do
        pfx="$(sc_env_prefix "$b")"
        printf '    %-30s bind port (default: %s)\n' "${pfx}_PORT" "$(sc_port_default "$b")"
        printf '    %-30s bind host (default: 127.0.0.1)\n' "${pfx}_HOST"
        printf '    %-30s model override\n' "${pfx}_MODEL"
    done
    printf '    %-30s log/PID directory (default: .validation/)\n' "JOHNNY_SIDECAR_LOG_DIR"
    cat <<'EOF'

EXIT CODES
    0=ok  1=failure  2=bad usage  3=toolchain unavailable  4=port conflict
EOF
}

# ---------------------------------------------------------------------------
# Multi-backend command runners (aggregate exit code = worst severity seen)
# ---------------------------------------------------------------------------

# Severity rank so an aggregate run surfaces the most serious outcome.
sc_rank() { case "$1" in 0) echo 0 ;; 3) echo 1 ;; 4) echo 2 ;; 2) echo 3 ;; *) echo 4 ;; esac; }

sc_run_each() {
    local fn="$1" backend="$2" b rc worst worst_rank rank
    worst=0; worst_rank=0
    if [ -n "$backend" ]; then
        "$fn" "$backend"
        return $?
    fi
    for b in $PROVIDER_BACKENDS; do
        "$fn" "$b"; rc=$?
        rank="$(sc_rank "$rc")"
        if [ "$rank" -gt "$worst_rank" ]; then worst_rank="$rank"; worst="$rc"; fi
    done
    return "$worst"
}

# Validate an optional backend arg; exit 2 on an unknown one.
sc_require_backend() {
    if [ -n "$1" ] && ! sc_is_backend "$1"; then
        sc_err "unknown backend: $1 (valid: $PROVIDER_BACKENDS)"
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

sc_main() {
    local cmd="${1:-}"; shift || true

    # Transitional alias: a bare backend name means `start <backend>`.
    if [ -n "$cmd" ] && sc_is_backend "$cmd"; then
        sc_err "note: '\`${cmd}\`' is deprecated; use 'start ${cmd}'."
        set -- "$cmd" "$@"
        cmd="start"
    fi

    case "$cmd" in
        start)
            sc_require_backend "${1:-}"
            sc_run_each sc_start_backend "${1:-}"
            ;;
        stop)
            sc_require_backend "${1:-}"
            sc_run_each sc_stop_backend "${1:-}"
            ;;
        restart)
            sc_require_backend "${1:-}"
            sc_run_each sc_stop_backend "${1:-}" || true
            sc_run_each sc_start_backend "${1:-}"
            ;;
        status)
            sc_require_backend "${1:-}"
            local b
            sc_log "${PROVIDER} sidecar status:"
            if [ -n "${1:-}" ]; then
                sc_status_backend "$1"
            else
                for b in $PROVIDER_BACKENDS; do sc_status_backend "$b"; done
            fi
            ;;
        probe)
            sc_require_backend "${1:-}"
            [ -n "${1:-}" ] || { sc_err "probe needs a backend"; exit 2; }
            sc_probe_backend "$1"; echo
            ;;
        port)
            sc_require_backend "${1:-}"
            [ -n "${1:-}" ] || { sc_err "port needs a backend"; exit 2; }
            sc_port "$1"; echo
            ;;
        backends)
            local b
            for b in $PROVIDER_BACKENDS; do echo "$b"; done
            ;;
        logs)
            sc_require_backend "${1:-}"
            sc_logs_backend "${1:-}"
            ;;
        -h|--help|help)
            sc_help
            ;;
        "")
            sc_help >&2
            exit 2
            ;;
        *)
            sc_err "unknown command: $cmd"
            sc_help >&2
            exit 2
            ;;
    esac
}

# Default hooks (a launcher may override). Guarded so they never clobber a
# launcher-provided hook regardless of whether the launcher defines it before
# or after sourcing this file. A minimal launcher only has to define
# sc_dir / sc_port_default / sc_kind.
command -v sc_blurb >/dev/null 2>&1 || sc_blurb() { printf '%s backend' "$1"; }
command -v sc_binary >/dev/null 2>&1 || sc_binary() { printf ''; }
command -v sc_post_launch_hint >/dev/null 2>&1 || sc_post_launch_hint() { printf ''; }
