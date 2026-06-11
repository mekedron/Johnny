#!/usr/bin/env bash
# google-calendar skill runner — executes INSIDE the Johnny skills-sandbox
# container (invoked by the skill executor as `bash /skills/google-calendar/run.sh`).
#
# Contract (skills/README.md): exit 0 -> stdout is the speech-ready result;
# non-zero exit -> stdout (when present) is the spoken failure copy and
# stderr the diagnostic detail. Format for the ear: no JSON, no IDs, no URLs.
set -uo pipefail

DAYS=7
MAX_EVENTS=10
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1) Connected-account check — the graceful "not connected" leg.
auth_output="$(gog auth list --no-input 2>&1)" || {
  echo "I couldn't check the Google account connection for the calendar."
  printf 'gog auth list failed: %s\n' "$auth_output" >&2
  exit 1
}
if printf '%s' "$auth_output" | grep -qi 'no tokens stored'; then
  echo "I can't see the Google calendar yet — no Google account is connected to my tools. Connect one with 'gog auth add' in the skills sandbox, then ask me again."
  exit 2
fi

# 2) Fetch the upcoming events as JSON.
err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT
events_json="$(gog calendar events list --days "$DAYS" --max "$MAX_EVENTS" --json --no-input 2>"$err_file")"
rc=$?
if [ $rc -ne 0 ]; then
  if grep -qi 'missing --account' "$err_file"; then
    echo "More than one Google account is connected and none is the default — set one with 'gog auth manage' in the skills sandbox, then ask me again."
  else
    echo "I couldn't fetch the calendar just now — the lookup failed on my side."
  fi
  cat "$err_file" >&2
  exit 1
fi

# 3) Speech-ready summary.
printf '%s' "$events_json" | python3 "$HERE/format_events.py" --days "$DAYS"
