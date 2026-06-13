#!/usr/bin/env bash
#
# gog-auth-workspace.sh — authenticate a Google account into a Johnny
# *workspace sandbox container*'s gog keyring, with NO published host port.
#
# Why this exists:
#   The per-workspace sandbox containers (johnny-workspace-<id>) do NOT publish
#   a host port, so gog's loopback browser-callback flow can't be reached from
#   your browser. This script uses gog's *browserless* two-step remote OAuth
#   flow instead, all in one run so the pending-auth state never expires and
#   the container can't be idle-swept between the two steps:
#
#       step 1  gog builds a Google consent URL   (this script opens it for you)
#       you     approve in your browser as the target account
#       step 2  you paste the redirect URL back; gog exchanges it for a refresh
#               token stored in the container's bind-mounted GOG_HOME — which
#               survives container restarts, idle-sweeps and clean installs.
#
#   Your HOST gog (~/Library/Application Support/gogcli) is never touched: every
#   gog command runs *inside the container* via `docker exec`.
#
# Usage:
#   scripts/gog-auth-workspace.sh <container> <email> [options]
#
# Options:
#   --creds <path>     Google OAuth client_secret_*.json (Desktop client).
#                      Needed only the first time, if the container has no
#                      client credentials stored yet.
#   --services <list>  Services to authorize (default: all). e.g. calendar,tasks
#                      or gmail,drive,docs — see `gog auth services`.
#   --readonly         Request read-only scopes where available.
#   --timeout <dur>    Consent window before step-1 state expires (default 30m).
#   --no-open          Print the URL but do not auto-open a browser.
#   -h, --help         Show this help.
#
# Examples:
#   scripts/gog-auth-workspace.sh johnny-workspace-1 nikita.rabykin@aikamatkat.fi
#   scripts/gog-auth-workspace.sh johnny-workspace-2 me@corp.com \
#       --creds ~/Downloads/client_secret_123.apps.googleusercontent.com.json \
#       --services calendar,tasks,gmail
#
set -euo pipefail

usage() { sed -n '3,46p' "$0"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*" >&2; }

# Read one possibly-very-long line from the terminal. Interactive terminals run
# in canonical mode with a ~1024-byte line limit (MAX_CANON); a multi-KB OAuth
# redirect URL overflows it, the buffer locks, and Enter appears to do nothing.
# Disabling canonical mode for the read removes the limit. When stdin is not a
# tty (piped/tested) we just read it directly.
read_long() {
  local saved="" line=""
  if [ -t 0 ]; then
    saved="$(stty -g 2>/dev/null || true)"
    [ -n "$saved" ] && stty -icanon min 1 time 0 2>/dev/null || true
    IFS= read -r line || true
    [ -n "$saved" ] && stty "$saved" 2>/dev/null || true
  else
    IFS= read -r line || true
  fi
  printf '%s' "${line%$'\r'}"
}

clip_avail() {
  command -v pbpaste >/dev/null 2>&1 || command -v wl-paste >/dev/null 2>&1 \
    || command -v xclip >/dev/null 2>&1 || command -v xsel >/dev/null 2>&1
}
clip_read() {
  if   command -v pbpaste  >/dev/null 2>&1; then pbpaste 2>/dev/null
  elif command -v wl-paste >/dev/null 2>&1; then wl-paste 2>/dev/null
  elif command -v xclip    >/dev/null 2>&1; then xclip -o -selection clipboard 2>/dev/null
  elif command -v xsel     >/dev/null 2>&1; then xsel -b 2>/dev/null
  fi
}

CONTAINER="" EMAIL="" CREDS="" SERVICES="all" READONLY="" TIMEOUT="30m" OPEN=1
REDIRECT_URI="http://localhost"

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)   usage; exit 0 ;;
    --creds)     CREDS="${2:-}"; shift 2 ;;
    --services)  SERVICES="${2:-}"; shift 2 ;;
    --timeout)   TIMEOUT="${2:-}"; shift 2 ;;
    --readonly)  READONLY="--readonly"; shift ;;
    --no-open)   OPEN=0; shift ;;
    --)          shift; break ;;
    -*)          die "unknown option: $1 (try --help)" ;;
    *)
      if   [ -z "$CONTAINER" ]; then CONTAINER="$1"
      elif [ -z "$EMAIL" ];     then EMAIL="$1"
      else die "unexpected argument: $1"; fi
      shift ;;
  esac
done

[ -n "$CONTAINER" ] || { usage; echo; die "missing <container> (e.g. johnny-workspace-1)"; }
[ -n "$EMAIL" ]     || die "missing <email> (e.g. you@example.com)"
command -v docker >/dev/null 2>&1 || die "docker not found on PATH"

# --- preflight: the container must be running -------------------------------
status="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)"
case "$status" in
  running) ;;
  "")  die "container '$CONTAINER' does not exist. Start its workspace first
       (open it in the Johnny UI, or trigger a dispatch/task), then re-run." ;;
  *)   info "container '$CONTAINER' is '$status' — starting it"
       docker start "$CONTAINER" >/dev/null || die "could not start '$CONTAINER'" ;;
esac

dexec() { docker exec "$CONTAINER" "$@"; }

dexec sh -lc 'command -v gog >/dev/null 2>&1' || die "gog is not installed inside '$CONTAINER'"

gog_home="$(dexec sh -lc 'printf %s "${GOG_HOME:-<default XDG under \$HOME>}"' 2>/dev/null || true)"
info "container=$CONTAINER  account=$EMAIL  GOG_HOME=$gog_home"

if ! dexec sh -lc '[ -n "${GOG_KEYRING_PASSWORD:-}" ]'; then
  info "warning: GOG_KEYRING_PASSWORD is unset in the container — the file keyring"
  info "         may prompt and block. Set it in .env (GOG_KEYRING_PASSWORD=...)."
fi

# --- 1. file keyring backend (headless container) ---------------------------
info "ensuring file keyring backend"
dexec gog -y auth keyring file >/dev/null || die "could not set the file keyring backend"

# --- 2. OAuth client credentials --------------------------------------------
creds_out="$(dexec gog auth credentials list 2>&1 || true)"
if printf '%s' "$creds_out" | grep -qiE 'no .*credentials'; then
  [ -n "$CREDS" ] || die "no OAuth client credentials in '$CONTAINER' yet.
       Re-run with --creds <client_secret_*.json> (download a Desktop OAuth
       client from Google Cloud Console)."
  [ -f "$CREDS" ] || die "--creds file not found: $CREDS"
  info "installing client credentials from $(basename "$CREDS")"
  docker cp "$CREDS" "$CONTAINER:/tmp/gog-client.$$.json"
  dexec gog auth credentials set --insecure "/tmp/gog-client.$$.json" >/dev/null \
    || die "gog auth credentials set failed"
  docker exec -u root "$CONTAINER" rm -f "/tmp/gog-client.$$.json" 2>/dev/null || true
else
  info "OAuth client credentials already present"
fi

# clear any stale pending-auth state so step 1 starts clean (old aborted
# attempts leave per-state files behind and only add confusion)
dexec sh -lc 'for d in ${GOG_HOME:+"$GOG_HOME/config"} "$HOME/.config/gogcli"; do rm -f "$d"/oauth-manual-state-*.json 2>/dev/null || true; done' || true

# --- 3. remote step 1: build the consent URL --------------------------------
info "requesting consent URL (step 1; ${TIMEOUT} window)"
# shellcheck disable=SC2086
step1="$(dexec gog auth add --remote --step 1 \
           --timeout "$TIMEOUT" --redirect-uri "$REDIRECT_URI" \
           $READONLY --services "$SERVICES" "$EMAIL" 2>&1)" \
  || die "step 1 failed:
$step1"

auth_url="$(printf '%s\n' "$step1" | grep -oE 'https://accounts\.google\.com/o/oauth2/auth[^[:space:]]+' | head -1 || true)"
[ -n "$auth_url" ] || die "could not parse a consent URL from step 1 output:
$step1"
# reuse the exact --services list gog echoed, so step 2 scopes stay consistent
svc2="$(printf '%s\n' "$step1" | sed -n 's/.*--services \([^[:space:]]*\).*/\1/p' | head -1 || true)"
[ -n "$svc2" ] || svc2="$SERVICES"
# force the intended account + a fresh consent (guarantees a refresh token)
open_url="${auth_url}&login_hint=${EMAIL}&prompt=consent"

echo                                                                    >&2
echo "────────────────────────────────────────────────────────────────" >&2
echo "Approve as ${EMAIL} at this URL:"                                  >&2
echo                                                                    >&2
echo "$open_url"                                                         >&2
echo                                                                    >&2
echo "────────────────────────────────────────────────────────────────" >&2
if [ "$OPEN" -eq 1 ]; then
  if   command -v open     >/dev/null 2>&1; then open "$open_url"     >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$open_url" >/dev/null 2>&1 || true
  fi
fi

# --- 4. get the redirect URL ------------------------------------------------
# The redirect URL is multi-KB, which a terminal won't accept as a normal typed
# paste. Primary path: you COPY it and we read the clipboard. Direct paste also
# works (canonical mode is disabled for the read).
echo                                                                            >&2
echo "After approving, the browser redirects to ${REDIRECT_URI}/?...&code=..."  >&2
echo "(the page fails to load — that's expected; the URL is what we need)."     >&2
echo                                                                            >&2
if clip_avail; then
  echo "COPY that URL to your clipboard, then press Enter here."                >&2
  echo "(Direct paste + Enter also works — long pastes are handled.)"          >&2
  printf 'Press Enter once copied (or paste + Enter): '                        >&2
else
  echo "Paste the FULL URL (or at least the part with state= and code=)."       >&2
  printf 'Redirect URL: '                                                      >&2
fi
redirect="$(read_long)"
case "$redirect" in
  *code=*) ;;                                   # direct paste arrived intact
  *)                                            # empty/short → read clipboard
    clip="$(clip_read || true)"
    case "$clip" in *code=*) redirect="$clip"; info "read redirect URL from clipboard" ;; esac ;;
esac
case "$redirect" in
  *code=*) ;;
  *) die "no 'code=' found in your input or clipboard.
       Copy the full ${REDIRECT_URI}/?...code=... URL, then re-run and press Enter." ;;
esac

# Browsers upgrade the loopback redirect to https:// (HSTS / "always use HTTPS"),
# but gog stored redirect_uri=http://localhost and reports the scheme difference
# as "manual auth state mismatch". Downgrade to match the stored value.
case "$redirect" in
  https://*) redirect="http://${redirect#https://}"; info "normalized redirect scheme https→http" ;;
esac

# Refresh the pending-state TTL right before exchange. gog expires the step-1
# state a few minutes after created_at (a slow browser approval -> "manual auth
# state missing"), and --timeout is not persisted into the state file.
rstate="$(printf '%s' "$redirect" | grep -oE 'state=[A-Za-z0-9_-]+' | head -1 | cut -d= -f2 || true)"
if [ -n "$rstate" ]; then
  dexec sh -lc '
    rs="$1"; now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    for d in ${GOG_HOME:+"$GOG_HOME/config"} "$HOME/.config/gogcli"; do
      f="$d/oauth-manual-state-${rs}.json"
      [ -f "$f" ] && { sed -i "s#\"created_at\": \"[^\"]*\"#\"created_at\": \"$now\"#" "$f"; exit 0; }
    done
  ' _ "$rstate" 2>/dev/null || true
fi

# --- 5. remote step 2: exchange the code ------------------------------------
info "exchanging authorization code (step 2)"
# shellcheck disable=SC2086
dexec gog auth add --remote --step 2 \
  --auth-url "$redirect" --redirect-uri "$REDIRECT_URI" \
  $READONLY --services "$svc2" "$EMAIL" \
  || die "step 2 failed — the code may have expired or the wrong account approved.
       Just re-run the script."

# --- 6. verify --------------------------------------------------------------
echo >&2
info "stored accounts in '$CONTAINER':"
dexec gog auth list
echo >&2
info "done — ${EMAIL} is authenticated inside ${CONTAINER}."
