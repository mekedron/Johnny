# MCP connectors (Johnny-trt.36 · Johnny-hp1)

MCP servers are the **third capability source** in the three-layer model:
TOOLS execute (core `sandbox.exec` + MCP-contributed), SKILLS instruct
(SKILL.md packages), MCP servers *contribute tools*. Every tool a configured
server exposes becomes a delegatable kind named `mcp__<server>__<tool>`,
flowing through the same catalog → router → worker-executor chain as skills
and vetted by the same capability policy (`docs/CAPABILITY-POLICY.md` —
`mcp__shady__*` in a deny list hides a whole server).

## Config model — a per-workspace JSON file, not a DB table

MCP servers are **owned by a workspace** and stored as a
[FastMCP `mcpServers` file](https://gofastmcp.com/integrations/mcp-json-configuration)
on the host — there is **no DB table** (the old `mcp_servers` table was
dropped in migration `0039`):

```
~/.johnny/workspaces/<slug>/.johnny/
├── skills/          # the workspace's skill packages (<name>/SKILL.md)
├── .mcp.json        # ← MCP server config (the source of truth)
└── .mcp-state.json  # ← derived probe verdicts + tool cache (not config)
```

A single `.johnny/` dir holds everything Johnny manages for a workspace.
`~/.johnny/workspaces/<slug>/` is a host bind mount, so these files survive
`./stop.sh` (`docker compose down -v`) — they are operator data, like the
`gog` Google credentials beside them.

### `.mcp.json` format

Standard `mcpServers` shape (FastMCP-compatible for stdio; the de-facto
`type`/`url`/`headers` superset for http), plus a `johnny` block carrying the
policy knobs FastMCP/Claude ignore:

```json
{
  "mcpServers": {
    "fixture": {
      "type": "stdio",
      "command": "python3",
      "args": ["/opt/sandbox/mcp_fixture_server.py"],
      "env": { "SOME_TOKEN": "${SOME_TOKEN}", "PLAIN": "literal" },
      "johnny": {
        "enabled": true,
        "tool_include": null,
        "tool_exclude": [],
        "connect_timeout_s": 10.0,
        "call_timeout_s": 60.0,
        "idle_ttl_s": 300.0
      }
    },
    "remote": {
      "type": "http",
      "url": "https://mcp.example.com/sse",
      "headers": { "Authorization": "Bearer ${REMOTE_TOKEN}" },
      "johnny": { "enabled": true }
    }
  }
}
```

| Field | Meaning |
| --- | --- |
| *key* (`"fixture"`) | The server **name** = lowercase slug (`a-z0-9-`, max 64, **no underscores**) — prefixes every contributed kind, so `mcp__<name>__<tool>` parses unambiguously. It is the identity (there is no surrogate id). |
| `type` | `stdio` (command spawned **inside the workspace sandbox**) or `http` (streamable-HTTP URL dialed directly). `transport` is accepted as an alias; inferred from `url` when omitted. |
| `command`, `args` | stdio only — argv, never a shell line. |
| `env`, `headers` | Plaintext or `${VAR}` placeholders (below). The API masks values to key names (`env_keys` / `header_keys`); it never echoes a value back. |
| `url` | http only — `http(s)://…`. |
| `johnny.enabled` | Disabled servers contribute nothing anywhere (but stay probe-able — the add → probe → enable flow). |
| `johnny.tool_include` / `tool_exclude` | Per-server glob filters (`fnmatch`); `include=null` ⇒ all, empty list ⇒ none, exclude wins. Applied at read time — editing filters needs no re-probe. |
| `johnny.connect_timeout_s` / `call_timeout_s` / `idle_ttl_s` | Clamped 1–600 s (TTL 10–3600 s). The worker's own per-task ceiling (`JOHNNY_TASK_EXEC_TIMEOUT_SECONDS`) still bounds the whole execution. |

`.mcp-state.json` (sibling, keyed by name) holds the derived probe state —
`{ "<name>": { "last_probe_at", "last_probe_ok", "last_probe_error", "tools_cache" } }`
— written by the probe, never hand-edited.

Validation is one truth: every entry is constructed into the same
`johnny.mcp.config.McpServerConfig` value object the runtime uses; an invalid
shape (stdio without a command, http with a command, a bad name) is a 422 with
the operator-facing message. A malformed *entry* in a hand-edited file is
skipped with a log — one bad server never breaks the others, the catalog, or a
claim pass.

### Secrets — plaintext or `${VAR}`

Values in `env` / `headers` are stored **plaintext on disk** (the file lives
under `~/.johnny` on the operator's host) OR as `${VAR}` placeholders. At
*connect* time (the api on a probe, the worker on an exec) `${VAR}` is expanded
from **that process's environment** — the same semantics FastMCP/Claude use —
and the resolved values are passed to the server (for stdio, through the
sandbox bridge's env overlay; the sandbox container itself needs no secrets).
An unset `${VAR}` expands to empty. So a secret can live in your `.env` / a
secrets manager and be referenced by name, never written into the file.

To make a `${VAR}` reachable, set it on the **api and worker** containers — add
it to the shared `x-backend-env` anchor in `docker-compose.yml` and to
`.env(.example)`.

## Managing servers

### From the UI (recommended)

`/workspaces/<id>` → **MCP servers** section. Add → fill the form → **Probe** →
**Enable**. Each card shows the transport, command/url, the stored secret key
names (values never shown), and the latest probe verdict. Edit / Disable /
Delete are per-server. The frontend talks to the API below.

### From the API

Routes are per-workspace and **name-keyed**:

```
GET    /workspaces/{wid}/mcp-servers              # list (masked)
POST   /workspaces/{wid}/mcp-servers              # create  → 201 | 409 dup | 422 invalid
GET    /workspaces/{wid}/mcp-servers/{name}
PATCH  /workspaces/{wid}/mcp-servers/{name}       # patch; omit env/headers to keep them; `name` renames
DELETE /workspaces/{wid}/mcp-servers/{name}
POST   /workspaces/{wid}/mcp-servers/{name}/probe
```

### By editing the file

Because `.mcp.json` *is* the source of truth, you can hand-edit it and the
change bites the next session/claim with no restart (the worker re-reads it per
claim, assembly per session). Probe from the UI/API afterwards so the catalog
learns the tools.

## Probe — the add → probe → enable flow

`POST …/{name}/probe` connects with the server's **live** config (works for
disabled servers on purpose), initializes, runs `tools/list`, and persists the
verdict to `.mcp-state.json`:

* **success** — `tools_cache` refreshed with the full (unfiltered) tool list;
  the response reports every tool with its filter verdict and the qualified
  catalog kind it will contribute.
* **failure** — `ok=false` + the operator-facing error, and the **stale
  `tools_cache` survives**: the catalog keeps rendering the tools as
  *unavailable with the reason* (Johnny-trt.55) instead of letting them
  silently vanish.

A stdio probe lazily ensures the workspace's own sandbox container
(`johnny-workspace-<id>`) first; an unreachable sandbox degrades the probe to
`ok=false` with a reason, never an error response.

## Catalog, router & execution

Unchanged by the storage cutover. Session assembly never connects to MCP
servers — it reads the workspace's `.mcp.json` + `.mcp-state.json` and merges
entries in resolution order **internal → skills → mcp**, scoped to the agent's
workspace via `slug_for_stamp`. The worker re-reads configs **per claimed
task**, connects **lazily on first tool reference** via `McpClientManager`
(cached per server while the config fingerprint matches; filter/timeout/TTL
edits apply live), runs the tool, and settles `done`/`failed` with spoken-form
text. Every failure leg speaks honestly — a server being
down/misconfigured/slow can never crash the executor pass.

## Where stdio servers run — the sandbox bridge

Stdio MCP servers spawn **inside the workspace's sandbox container** (the same
`johnny-skills-sandbox` image, the same security boundary as CLI skills — never
on the host, never in the api/worker containers). The sandbox daemon
(`sandbox/execd.py`) exposes a stdio bridge the worker/api pump
newline-delimited JSON-RPC over (`POST /mcp/start|send`, `GET /mcp/recv`,
`POST /mcp/stop`); `johnny.mcp.client.sandbox_stdio_client` mirrors the SDK's
`stdio_client` over it so the official `mcp` SDK's `ClientSession` drives both
transports identically. HTTP servers use the SDK's streamable-HTTP transport
straight from the worker/api process.

**Consequence: a stdio server's `command` must exist in the sandbox image.**
The baseline image ships `bash/coreutils/curl/jq/git/python3/ripgrep/gog` +
Node 20 (see below). To run a server that needs another binary, add it to the
sandbox image (`sandbox/Dockerfile` LAYER 3, or a `sandbox/setup.d/*.sh` hook)
and rebuild with `./run.sh` — never `docker compose exec … install`, which
vanishes on the next rebuild.

The server *command* is operator-trusted config (like installing a skill) and
is not subject to the exec-bin policy — the capability policy governs MCP at
the **tool** level (`mcp__<server>__<tool>` globs), and the sandbox container
is the blast-radius boundary for the process itself.

## Worked example — the n8n connector (vendored into the image)

The default workspace ships an `n8n-mcp` stdio connector, end-to-end runnable
in the dockerized sandbox:

* **Launcher vendored into the image** — `sandbox/n8n/{n8n-mcp-launcher.mjs,
  n8n-cf-proxy.mjs}` are `COPY`d to `/opt/sandbox/n8n/`, and
  `sandbox/setup.d/30-install-node.sh` installs Node 20 + pre-installs the
  `n8n-mcp` npm package (with the npx cache at `/opt/npm-cache`, off the
  bind-mounted home so it isn't shadowed at runtime). So `command: node`,
  `args: ["/opt/sandbox/n8n/n8n-mcp-launcher.mjs"]` resolves with no host path
  and no runtime download.
* **Secrets via `${VAR}`** — the entry's env references `${N8N_API_KEY}`,
  `${CF_ACCESS_CLIENT_ID}`, `${CF_ACCESS_CLIENT_SECRET}`; compose passes those
  three to the api+worker (the `x-backend-env` anchor), and `.env.example`
  carries them as 1Password references. Launch so `op` resolves them:

  ```bash
  op run --env-file=.env -- ./run.sh
  ```

  Without the secrets the connector still renders in the panel; its probe
  reports `ok=false` with the launcher's own "CF_ACCESS… must be set" message
  (proof Node + the launcher ran). With them, the probe lists the n8n tools.
* **Seeded, reproducibly** — `run.sh` copies `config/seed/default.mcp.json` to
  `~/.johnny/workspaces/default/.johnny/.mcp.json` if absent (insert-only;
  operator edits are never clobbered).

## Reference fixture

The sandbox image bakes a known-good stdio server at
`/opt/sandbox/mcp_fixture_server.py` (tools: `echo`, `add`, `always-fail`) —
needs no secrets. Add it to any workspace, probe it, and you should get
`ok: true` with `catalog_kinds: ["mcp__fixture__echo", "mcp__fixture__add"]`,
verifying the file → config → bridge → probe path end-to-end. The integration
suite (`tests/integration/test_mcp_sandbox.py`) drives the same fixture through
the real bridge; `tests/mcp/test_store.py` covers the file store, and
`tests/api/test_mcp_servers.py` the CRUD + probe API.

## Clean install

Everything arrives via the canonical `./stop.sh && ./run.sh` cycle: the `mcp`
SDK is a main dependency in `backend/pyproject.toml`, the bridge + fixture +
Node + the n8n launcher are baked into the sandbox image, migration `0039`
drops the legacy table at boot, and `run.sh` relocates any pre-`hp1`
`<slug>/skills` to `<slug>/.johnny/skills` and seeds the default `.mcp.json`.
No runtime installs, no hand-patched containers.
