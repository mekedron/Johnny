#!/usr/bin/env bash
# check-sidecar-cli.sh — assert every per-provider sidecar launcher honours the
# shared CLI contract (Johnny-1ge.6 acceptance). Loops over each
# scripts/start-<provider>-sidecar.sh and checks:
#   * --help              exits 0 and prints the shared help-block sections
#   * status              exits 0
#   * stop bogus-backend  exits 2 (bad usage)
# Plus a smoke of the umbrella scripts/start-sidecars.sh (--help, status).
#
# Read-only: it never starts or stops a real sidecar. Exit 0 = all good.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
fails=0

check() {
    local desc="$1" expected="$2"; shift 2
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ "$rc" -ne "$expected" ]; then
        echo "FAIL: ${desc} — expected exit ${expected}, got ${rc}"
        fails=$((fails + 1))
    else
        echo "ok:   ${desc} (exit ${rc})"
    fi
}

# Each section header the shared help block must contain.
HELP_SECTIONS="NAME USAGE COMMANDS BACKENDS ENV"

check_help_block() {
    local launcher="$1" help section
    help="$("$launcher" --help 2>&1)"
    for section in $HELP_SECTIONS; do
        if ! printf '%s\n' "$help" | grep -qE "^${section}$"; then
            echo "FAIL: $(basename "$launcher") --help missing section '${section}'"
            fails=$((fails + 1))
        fi
    done
    if ! printf '%s\n' "$help" | grep -qE "^EXIT CODES$"; then
        echo "FAIL: $(basename "$launcher") --help missing 'EXIT CODES'"
        fails=$((fails + 1))
    fi
    if ! printf '%s\n' "$help" | grep -qE "0=ok +1=failure +2=bad usage +3=toolchain unavailable +4=port conflict"; then
        echo "FAIL: $(basename "$launcher") --help missing the canonical exit-code legend"
        fails=$((fails + 1))
    fi
}

found=0
for launcher in "${SCRIPTS_DIR}"/start-*-sidecar.sh; do
    [ -f "$launcher" ] || continue
    found=1
    name="$(basename "$launcher")"
    echo "--- ${name} ---"
    check "${name} --help" 0 "$launcher" --help
    check "${name} status" 0 "$launcher" status
    check "${name} stop bogus-backend" 2 "$launcher" stop bogus-backend
    check_help_block "$launcher"
done

if [ "$found" -eq 0 ]; then
    echo "FAIL: no start-*-sidecar.sh launchers found"
    exit 1
fi

echo "--- start-sidecars.sh (umbrella) ---"
check "start-sidecars.sh --help" 0 "${SCRIPTS_DIR}/start-sidecars.sh" --help
check "start-sidecars.sh status" 0 "${SCRIPTS_DIR}/start-sidecars.sh" status
check "start-sidecars.sh bogus-cmd" 2 "${SCRIPTS_DIR}/start-sidecars.sh" bogus-cmd

echo
if [ "$fails" -eq 0 ]; then
    echo "ALL SIDECAR CLI CHECKS PASSED"
    exit 0
fi
echo "${fails} CHECK(S) FAILED"
exit 1
