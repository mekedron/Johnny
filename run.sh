#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

# Piper voices, Whisper, and Parakeet STT models live in host bind
# mounts under ~/.johnny so the user can `ls` them, drop files in
# manually, and not lose them across `docker compose down -v` resets.
# session-audio holds Johnny's captured reply WAVs (one dir per session)
# for History / live playback — same survive-a-reset reasoning.
# skills holds skill packages (<name>/SKILL.md) and sandbox-home the
# skills-sandbox user's home (gog auth state) — both Johnny-trt.35,
# same survive-a-reset reasoning.
# Create idempotently on first boot so the very first run does not
# fail mounting a missing directory.
# workspaces holds one dir per workspace
# (~/.johnny/workspaces/<slug>/.johnny/skills — Johnny-wks.3 per-workspace skill
# packages, relocated under .johnny/ by Johnny-hp1; wks.4 keeps the gog keyring
# at <slug>/gog next to it), same survive-a-reset
# reasoning. The DEFAULT workspace (slug `default`) is lazy-launched like
# finance/ops now (Johnny-etu.5), so it gets its OWN dir here too — its
# skills + gog state no longer live in the legacy shared sandbox-home.
#
# One-time relocation (Johnny-hp1): per-workspace skills moved from
# ~/.johnny/workspaces/<slug>/skills to ~/.johnny/workspaces/<slug>/.johnny/skills
# so a single .johnny/ dir holds all Johnny-managed per-workspace state (skills
# + the new .mcp.json). Idempotent: only moves when the OLD dir exists and the
# NEW one does not, so fresh installs and re-runs are no-ops. The host bind
# mount survives `docker compose down -v`, so this converges once per install.
if [[ -d "${HOME}/.johnny/workspaces" ]]; then
  for ws in "${HOME}/.johnny/workspaces"/*/; do
    [[ -d "${ws}" ]] || continue
    if [[ -d "${ws}skills" && ! -e "${ws}.johnny/skills" ]]; then
      echo "[run.sh] Relocating ${ws}skills -> ${ws}.johnny/skills (Johnny-hp1)" >&2
      mkdir -p "${ws}.johnny"
      mv "${ws}skills" "${ws}.johnny/skills"
    fi
  done
fi

mkdir -p \
  "${HOME}/.johnny/piper-models" \
  "${HOME}/.johnny/whisper-models" \
  "${HOME}/.johnny/parakeet-models" \
  "${HOME}/.johnny/parakeet-packages" \
  "${HOME}/.johnny/kokoro-models" \
  "${HOME}/.johnny/kitten-models" \
  "${HOME}/.johnny/session-audio" \
  "${HOME}/.johnny/skills" \
  "${HOME}/.johnny/workspaces" \
  "${HOME}/.johnny/workspaces/default/.johnny/skills" \
  "${HOME}/.johnny/workspaces/default/gog" \
  "${HOME}/.johnny/sandbox-home"

# Seed the n8n MCP server into the default workspace's MCP config (Johnny-hp1).
# Insert-only: copy the repo template to the host bind mount ONLY when the
# operator has no file yet, so UI edits / added servers are never clobbered.
# The bind mount survives `docker compose down -v`, so a clean
# ./stop.sh && ./run.sh keeps the operator's servers; a brand-new ~/.johnny
# gets n8n-mcp seeded so it renders in the default workspace's MCP panel.
if [[ -f config/seed/default.mcp.json \
   && ! -f "${HOME}/.johnny/workspaces/default/.johnny/.mcp.json" ]]; then
  echo "[run.sh] Seeding default workspace MCP servers (n8n-mcp) into .johnny/.mcp.json" >&2
  cp config/seed/default.mcp.json \
    "${HOME}/.johnny/workspaces/default/.johnny/.mcp.json"
fi

# Ensure the first-party demo MCP connectors (Johnny-3gx: demo-fixture /
# demo-tools / demo-http) are present even when the operator already has a
# .mcp.json — the copy above is insert-only on a brand-new file, but ~/.johnny
# survives `docker compose down -v`, so a pre-existing install would otherwise
# never see the new demos. Insert-only PER SERVER: a demo key absent from the
# operator's file is added; every existing key, secret, UI edit, and non-demo
# server is left untouched. Mirrors the first-party-skills "repo wins" re-seed.
# Best-effort: a hiccup here never blocks the stack.
target_mcp="${HOME}/.johnny/workspaces/default/.johnny/.mcp.json"
if [[ -f config/seed/default.mcp.json && -f "${target_mcp}" ]] \
   && command -v python3 >/dev/null 2>&1; then
  python3 - "config/seed/default.mcp.json" "${target_mcp}" <<'PY' || \
    echo "[run.sh] WARN: could not merge demo MCP servers (continuing)" >&2
import json, sys

seed_path, target_path = sys.argv[1], sys.argv[2]
with open(seed_path) as f:
    seed = json.load(f)
with open(target_path) as f:
    target = json.load(f)
seed_servers = seed.get("mcpServers", {})
servers = target.setdefault("mcpServers", {})
added = []
for name, entry in seed_servers.items():
    if name.startswith("demo-") and name not in servers:
        servers[name] = entry
        added.append(name)
if added:
    with open(target_path, "w") as f:
        f.write(json.dumps(target, indent=2) + "\n")
    print("[run.sh] Added demo MCP servers: " + ", ".join(added), file=sys.stderr)
PY
fi

# Seed the first-party skill packages (Johnny-trt.23) into BOTH the shared
# skills volume (the skills-sandbox image-build + image-contract test target
# + legacy no-stamp fallback) AND the default workspace's OWN skills dir
# (Johnny-etu.5: the default lazy-launches `johnny-workspace-1` and mounts
# ~/.johnny/workspaces/default/skills at /skills, exactly like finance/ops).
# The repo ./skills tree is the source of truth, so a clean checkout boots
# with the gog skill present in the default workspace — re-copied on every
# start (repo wins for first-party dirs); operator-added skill dirs are never
# touched.
#
# Retired first-party skills (Johnny-etu.9: the calendar-only `google-calendar`
# skill was replaced by the general `gog` skill). The seed loop only COPIES, so
# a dir removed from the repo would otherwise linger forever in the host bind
# mount (it survives `docker compose down -v` — it is not a compose volume).
# Remove retired first-party names from both dests so an existing install
# converges to the repo on the next start. Only exact first-party names are
# swept; operator-added skills have different names and are untouched.
RETIRED_SKILLS=("google-calendar")
for dest in "${HOME}/.johnny/skills" "${HOME}/.johnny/workspaces/default/.johnny/skills"; do
  for retired in "${RETIRED_SKILLS[@]}"; do
    rm -rf "${dest:?}/${retired}"
  done
done
if [[ -d skills ]]; then
  for dest in "${HOME}/.johnny/skills" "${HOME}/.johnny/workspaces/default/.johnny/skills"; do
    find skills -mindepth 1 -maxdepth 1 -type d \
      -exec cp -Rf {} "${dest}/" \;
  done
fi

# Legacy migration hint: older installs kept the models in named Docker
# volumes (johnny_piper_models / johnny_whisper_models). Detect them and
# print a one-line ``docker cp``-style migration command so the user
# does not silently lose previously downloaded voices/weights.
if command -v docker >/dev/null 2>&1; then
  for legacy in johnny_piper_models johnny_whisper_models johnny_parakeet_models; do
    if docker volume inspect "${legacy}" >/dev/null 2>&1; then
      case "${legacy}" in
        johnny_piper_models) host_target="${HOME}/.johnny/piper-models" ;;
        johnny_whisper_models) host_target="${HOME}/.johnny/whisper-models" ;;
        johnny_parakeet_models) host_target="${HOME}/.johnny/parakeet-models" ;;
      esac
      cat <<EOF >&2
[run.sh] Detected legacy Docker volume "${legacy}".
[run.sh] To migrate previously downloaded files into the new host dir:
[run.sh]   docker run --rm -v ${legacy}:/from -v ${host_target}:/to alpine cp -an /from/. /to/
[run.sh] After verifying the files arrived, remove the legacy volume:
[run.sh]   docker volume rm ${legacy}
EOF
    fi
  done
fi

# The frontend MUST come from the compose `frontend` service — never from
# a host-side `pnpm dev` in ./frontend. A stray host vite survives terminal
# close (PPID becomes 1) and silently steals port 5173 from the dockerized
# frontend, so `docker compose up` then fails to bind. Sweep any such stray
# host dev-server still holding 5173 before bringing the stack up.
#
# ALLOWLIST, not denylist: only a recognized host dev-server (vite/esbuild
# run as `node`; a `pnpm`/`npm` parent) is killed. Everything else on :5173
# is LEFT ALONE — most importantly Docker Desktop's own port forwarder, which
# publishes the dockerized frontend's 5173. The earlier denylist (`com.docker.*`)
# silently missed it because macOS `ps -o comm=` returns the FULL path
# (`/Applications/Docker.app/.../com.docker.backend`), so the sweep killed
# Docker Desktop's backend and took the daemon down (bug Johnny-9ph). An
# allowlist makes that impossible: an unrecognized process is never killed —
# at worst `up -d --build` below prints a clear "address already in use".
for pid in $(lsof -nP -t -iTCP:5173 -sTCP:LISTEN 2>/dev/null || true); do
  cmd=$(ps -p "$pid" -o comm= 2>/dev/null || true)
  case "$(basename "${cmd:-/unknown}")" in
    node|vite|pnpm|npm|esbuild)
      echo "[run.sh] Killing stray host dev-server on :5173 (pid $pid, $cmd) — the dockerized frontend will take over." >&2
      kill "$pid" 2>/dev/null || true
      ;;
    *) ;;  # Docker port-forwarder or anything unrecognized — never touched.
  esac
done

# meet-worker is gated behind a compose profile, so `up --build` skips it.
# Build it explicitly so per-session containers (spawned by the api via the
# Docker SDK) pick up the latest backend code.
docker compose --profile meet-worker build meet-worker

# Detached so the terminal is free after start. The user can tail logs
# explicitly when needed — keeps repeated `./run.sh` cycles from hanging
# the shell.
docker compose up -d --build "$@"

# Boot every available host sidecar (Parakeet STT / Piper / Kokoro TTS) so a
# saved sidecar-runtime works without a separate manual launch. Each sidecar is
# a soft dependency: a missing toolchain (no swift / no uv) SKIPS just that one,
# never fails the stack. Opt out per-sidecar with JOHNNY_DISABLED_SIDECARS
# (comma-separated keys, e.g. parakeet-coreml,kokoro-mlx — see .env.example).
# `|| true` keeps a single sidecar failure from aborting the whole bring-up.
"$(dirname "$0")/scripts/start-sidecars.sh" start || true

cat <<'EOF' >&2
[run.sh] Stack started in the background.
[run.sh]   Frontend:     http://localhost:5173
[run.sh]   API:          http://localhost:8000
[run.sh]   Sidecars:     ./scripts/start-sidecars.sh status   (per-sidecar state)
[run.sh]   Sidecar logs: tail -f .validation/<provider>-<backend>-sidecar.log
[run.sh] Tail logs:   docker compose logs -f
[run.sh] Stop stack:  ./stop.sh
EOF
