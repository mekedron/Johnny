#!/usr/bin/env bash
# gog skill runner — executes INSIDE the Johnny skills-sandbox container
# (invoked by the skill executor as `bash /skills/gog/run.sh`).
#
# Contract (skills/README.md): exit 0 -> stdout is the speech-ready result;
# non-zero exit -> stdout (when present) is the spoken failure copy and
# stderr the diagnostic detail. Format for the ear: no JSON, no IDs, no URLs.
#
# All the work (auth check, task-arg -> gog argv, read-only policy, run,
# speech formatting) lives in gog_run.py so it is unit-testable; this wrapper
# only locates it and hands off (exec preserves the exit code).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/gog_run.py"
