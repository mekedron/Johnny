#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

# Piper voices and Whisper STT models live in host bind mounts under
# ~/.johnny so the user can `ls` them, drop files in manually, and not
# lose them across `docker compose down -v` resets. Create idempotently
# on first boot so the very first run does not fail mounting a missing
# directory.
mkdir -p "${HOME}/.johnny/piper-models" "${HOME}/.johnny/whisper-models"

# Legacy migration hint: older installs kept the models in named Docker
# volumes (johnny_piper_models / johnny_whisper_models). Detect them and
# print a one-line ``docker cp``-style migration command so the user
# does not silently lose previously downloaded voices/weights.
if command -v docker >/dev/null 2>&1; then
  for legacy in johnny_piper_models johnny_whisper_models; do
    if docker volume inspect "${legacy}" >/dev/null 2>&1; then
      case "${legacy}" in
        johnny_piper_models) host_target="${HOME}/.johnny/piper-models" ;;
        johnny_whisper_models) host_target="${HOME}/.johnny/whisper-models" ;;
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

# meet-worker is gated behind a compose profile, so `up --build` skips it.
# Build it explicitly so per-session containers (spawned by the api via the
# Docker SDK) pick up the latest backend code.
docker compose --profile meet-worker build meet-worker

exec docker compose up --build "$@"
