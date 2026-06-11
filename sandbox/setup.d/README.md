# Sandbox setup hooks

Drop `*.sh` scripts into this directory to customize the skills-sandbox
image without editing the Dockerfile. Every `*.sh` file here runs at
**image-build time**, in sorted filename order, **as root**, inside the
sandbox image (debian bookworm-slim).

After adding or editing a script, rerun `./run-dev.sh` (or `./run.sh`) from
the repo root — both rebuild the image, so your tools survive every
`./stop.sh && ./run.sh` clean-install cycle. Never install tools with
`docker compose exec skills-sandbox apt-get install ...`; that vanishes on
the next rebuild.

Example — `10-install-imagemagick.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends imagemagick
rm -rf /var/lib/apt/lists/*
```

Conventions:

- Prefix with a number (`10-`, `20-`, ...) to control run order.
- `set -euo pipefail` so a failed install fails the build loudly instead of
  shipping a half-working image.
- Clean apt lists at the end of each script to keep the image small.
- Verify your tool landed: after the stack is up,
  `curl 'http://skills-sandbox:8088/bins?names=yourtool'` from the api
  container (or `docker compose exec skills-sandbox which yourtool`).

This README is the only file shipped here, so a clean checkout builds with
the hook loop as a no-op.
