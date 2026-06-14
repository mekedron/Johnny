#!/usr/bin/env bash
# Node.js + n8n-mcp for the vendored n8n connector (Johnny-hp1).
#
# Runs as root at IMAGE-BUILD time (the setup.d hook), so it survives every
# ./stop.sh && ./run.sh clean-install cycle — the n8n MCP server (stdio,
# spawned in this workspace's sandbox) needs `node` + `npx`, and debian
# bookworm-slim ships neither. The two launcher .mjs files are COPYed in by
# the Dockerfile at /opt/sandbox/n8n/.
set -euo pipefail

# NodeSource needs curl + ca-certificates (already in LAYER 1) + gnupg.
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg

# Node.js 20 LTS (bundles npm + npx). NodeSource's setup script adds the repo.
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y --no-install-recommends nodejs
rm -rf /var/lib/apt/lists/*

# Pre-install n8n-mcp so the launcher's `npx -y n8n-mcp` never pays a runtime
# npm fetch (the probe's connect timeout is short, and the package bundles a
# sizeable node-catalog DB). CRITICAL: the npm/npx cache must live OUTSIDE
# /home/sandbox — that path is a runtime bind mount (sandbox-home / the
# per-workspace home volume) that would shadow any cache under the sandbox
# user's home, forcing a re-download on first probe.
export NPM_CONFIG_CACHE=/opt/npm-cache
# The npm-published MCP servers the seeded connectors launch via npx:
#   n8n-mcp    — the n8n connector's stdio server
#   mcp-remote — the stdio<->remote-SSE bridge the Metabase connector uses
#                (`npx mcp-remote <url> --header …`)
npm install -g n8n-mcp mcp-remote
# Warm the npx cache too (npx may resolve via its own cache rather than the
# global prefix). Run a no-op `node` instead of each server's bin so the build
# resolves+caches the packages without starting (and hanging on) a server.
npm exec --yes --package=n8n-mcp -- node -e "process.exit(0)" || true
npm exec --yes --package=mcp-remote -- node -e "process.exit(0)" || true

# The runtime user is uid 1000 (sandbox); let it read the global modules and
# read/write the shared npx cache (npx writes lockfiles under it). The global
# modules path varies by installer (NodeSource uses /usr/lib, not
# /usr/local/lib), so resolve it via npm; a perms tweak must never fail the build.
npm_global_root="$(npm root -g)"
chmod -R a+rX "${npm_global_root}" || true
chown -R 1000:1000 /opt/npm-cache || true

node --version
npm --version
echo ">>> n8n-mcp + mcp-remote pre-installed (global + npx cache at ${NPM_CONFIG_CACHE})"
