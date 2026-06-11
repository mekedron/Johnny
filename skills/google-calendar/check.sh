#!/usr/bin/env bash
# google-calendar availability check — executes INSIDE the Johnny skills-sandbox
# container (invoked at session assembly and again at claim time as
# `bash /skills/google-calendar/check.sh`, Johnny-trt.55).
#
# Contract (metadata.johnny.availability): exit 0 -> the capability is usable
# now; non-zero exit -> unavailable, with stdout as the spoken-form actionable
# reason (the same words the router declines with and the claim-time
# correction speaks). stderr stays diagnostic-only.
set -uo pipefail

auth_output="$(gog auth list --no-input 2>&1)" || {
  echo "I couldn't verify the Google account connection for the calendar right now."
  printf 'gog auth list failed: %s\n' "$auth_output" >&2
  exit 1
}
if printf '%s' "$auth_output" | grep -qi 'no tokens stored'; then
  echo "I can't see the Google calendar yet — no Google account is connected to my tools. Connect one with 'gog auth add' in the skills sandbox, then ask me again."
  exit 2
fi
exit 0
