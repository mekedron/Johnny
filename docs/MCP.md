# MCP connector (Johnny-trt.36)

MCP servers are the **third capability source** in the three-layer model:
TOOLS execute (core `sandbox.exec` + MCP-contributed), SKILLS instruct
(SKILL.md packages), MCP servers *contribute tools*. Every tool a configured
server exposes becomes a delegatable kind named `mcp__<server>__<tool>`,
flowing through the same catalog → router → worker-executor chain as skills
and vetted by the same capability policy (`docs/CAPABILITY-POLICY.md` —
`mcp__shady__*` in a deny list hides a whole server).

## Config model — DB rows, not a JSON file

One `mcp_servers` row per server (the provider-settings pattern), managed
over `GET/POST/PATCH/DELETE /mcp-servers`:

| Field | Meaning |
| --- | --- |
| `name` | Lowercase slug (`a-z0-9-`, max 64, **no underscores**) — prefixes every contributed kind, so `mcp__<name>__<tool>` parses unambiguously |
| `transport` | `stdio` (command spawned **inside the skills-sandbox**) or `http` (streamable-HTTP URL dialed directly) |
| `command`, `args` | stdio only — argv, never a shell line |
| `env`, `headers` | Write-only secrets, Fernet-encrypted at rest in one blob (`secrets_encrypted`); responses surface key names only (`env_keys` / `header_keys`) |
| `url` | http only — `http(s)://…` |
| `enabled` | Disabled servers contribute nothing anywhere |
| `tool_include` / `tool_exclude` | Per-server glob filters (`fnmatch`); `include=null` ⇒ all, empty list ⇒ none, exclude wins. Applied at read time — editing filters needs no re-probe |
| `connect_timeout_s` / `call_timeout_s` / `idle_ttl_s` | Clamped 1–600 s (TTL 10–3600 s). The worker's own per-task ceiling (`JOHNNY_TASK_EXEC_TIMEOUT_SECONDS`) still bounds the whole execution |
| `tools_cache`, `last_probe_*` | The probe's persisted verdict (below) |

Validation is one truth: the API constructs the same
`johnny.mcp.config.McpServerConfig` value object the runtime uses; an
invalid shape is a 422 with the operator-facing message.

## Probe — the add → probe → enable flow

`POST /mcp-servers/{id}/probe` connects with the row's **live** config
(works for disabled rows on purpose), initializes, runs `tools/list`, and
persists the verdict on the row:

* **success** — `tools_cache` refreshed with the full (unfiltered) tool
  list; the response reports every tool with its filter verdict and the
  qualified catalog kind it will contribute.
* **failure** — `ok=false` + the operator-facing error (`last_probe_error`),
  and the **stale `tools_cache` survives**: the catalog keeps rendering the
  tools as *unavailable with the reason* (Johnny-trt.55) instead of letting
  them silently vanish.

## Catalog & router

Session assembly never connects to MCP servers. It reads the rows' cached
view (one small SELECT on the sinks' shared session) and merges entries in
resolution order **internal → skills → mcp** (duplicate kinds keep the
earlier source — the one that will actually run). A server that has never
been probed contributes nothing: probe first. Entries from a probe-failed
server carry `available=false` + a spoken-form reason so the router declines
honestly. Policy filtering (trt.38) then applies exactly as for skills.
Catalog changes need no restart: assembly reads rows fresh per session, the
worker reads them fresh per claimed task.

## Execution — lazy lifecycle in the worker

The worker's resolution chain (trt.24) is internal guard → skills → **mcp**
→ fail-fast stub. For a claimed `mcp__…` kind:

1. Enabled configs are re-read from the DB **per execution** (the trt.38
   no-restart pattern); unknown/disabled servers and filtered-out tools
   settle `failed` with honest speech before any connection.
2. `McpClientManager` connects **lazily on first tool reference** and
   caches the connection per server while the config fingerprint
   (command/args/env/url/headers — and the resolved sandbox for stdio)
   matches; filter/timeout/TTL edits apply live without a reconnect.
3. Task args ride as the MCP tool-call arguments. Success speaks the
   result's text content (capped); a tool-level `isError` settles `failed`
   with generic honest speech and the detail in `error`.
4. Idle connections are evicted after the server's `idle_ttl_s` on the
   worker's sweep cadence; the next use reconnects transparently. A
   connection that errors or times out mid-call is evicted immediately.
5. Every failure leg settles the task with spoken-form text — an MCP server
   being down/misconfigured/slow can never crash the executor pass.

## Where stdio servers run — the sandbox bridge

Stdio MCP servers spawn **inside the skills-sandbox container** (the same
security boundary as CLI skills — never on the host, never in the
api/worker containers). The sandbox daemon (`sandbox/execd.py`) exposes a
minimal stdio bridge the worker/api pump newline-delimited JSON-RPC over:

```
POST /mcp/start  {"argv": [...], "env"?: {...}}      -> {"sid"}
POST /mcp/send   {"sid", "line"}                      -> {"ok": true}
GET  /mcp/recv?sid=…&timeout=20                       -> {"line"} | {"line": null, "exited", …}
POST /mcp/stop   {"sid"}                              -> {"ok", "exit_code"}
```

Caps (env-tunable on the sandbox service): `SANDBOX_MCP_MAX_SESSIONS` (8),
`SANDBOX_MCP_LINE_CAP_BYTES` (1 MiB — an oversized line kills the session),
`SANDBOX_MCP_IDLE_MAX_S` (900 s reaper, the backstop for a crashed worker).
The protocol brain stays in Johnny: `johnny.mcp.client.sandbox_stdio_client`
mirrors the SDK's `stdio_client` over this bridge, so the official `mcp`
SDK's `ClientSession` drives both transports identically. HTTP servers use
the SDK's streamable-HTTP transport straight from the worker/api process.

Note: the server *command* is operator-trusted config (like installing a
skill) and is not subject to the exec-bin policy — the capability policy
governs MCP at the **tool** level (`mcp__<server>__<tool>` globs), and the
sandbox container is the blast-radius boundary for the process itself.

## Reference fixture

The sandbox image bakes a known-good stdio server at
`/opt/sandbox/mcp_fixture_server.py` (tools: `echo`, `add`, `always-fail`).
Register it to verify the plumbing end-to-end:

```bash
curl -s -X POST localhost:8000/mcp-servers -H 'content-type: application/json' -d '{
  "name": "fixture",
  "transport": "stdio",
  "command": "python3",
  "args": ["/opt/sandbox/mcp_fixture_server.py"]
}'
curl -s -X POST localhost:8000/mcp-servers/1/probe
# → ok: true, catalog_kinds: ["mcp__fixture__echo", "mcp__fixture__add", …]
```

The integration suite (`tests/integration/test_mcp_sandbox.py`) drives the
same fixture through the real bridge; the hermetic twin
(`tests/mcp/test_client_bridge.py`) runs the full SDK chain against an
in-process fake bridge.

## Clean install

Everything arrives via the canonical `./stop.sh && ./run.sh` cycle: the
`mcp` SDK is a main dependency in `backend/pyproject.toml` (+ `uv.lock`),
the bridge and fixture are baked into the sandbox image, and the
`mcp_servers` table is migration `0031` (applied at boot). No runtime
installs, no hand-patched containers.
