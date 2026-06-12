# Skills sandbox (`skills-sandbox` compose service)

The execution boundary for all skill / CLI tool work (Johnny-trt.35). The
api / worker containers never run skill commands themselves — the tool layer
(`sandbox.exec`, Johnny-trt.23) POSTs to this container's internal exec API
and the command runs **here**: a separate, operator-customizable Docker
container where CLI tools are installed and skills live. Mirrors openclaw's
sandbox model (skills remapped in via a volume, `requires.bins` checked
inside the container, never on the host).

## Guaranteed baseline toolset

Skills may rely on **all** of the following without declaring them in
`requires.bins` — the image ships them on every clean install, and the
integration test `backend/tests/integration/test_skills_sandbox.py`
(`test_bins_baseline_all_present`) is the enforced contract:

| Area | Bins |
| --- | --- |
| Shell | `bash` |
| coreutils | `cat`, `cut`, `head`, `tail`, `tr`, `sort`, `uniq`, `wc` (and the rest of coreutils) |
| Text / search | `grep`, `sed`, `awk` (gawk), `rg` (ripgrep) |
| Files | `find`, `xargs` (findutils), `tar`, `gzip` |
| Network / data | `curl`, `jq` (TLS trust via `ca-certificates`) |
| Dev | `git`, `python3` |
| Google | `gog` ([openclaw/gogcli](https://github.com/openclaw/gogcli), the reference Google CLI) |

## Exec API (internal-only)

HTTP on the compose network at `http://skills-sandbox:8088` — **no published
ports**, so it is reachable from the api / worker containers only, never from
the host. There is deliberately no auth; network topology is the boundary.
The api/worker read the URL from `JOHNNY_SKILLS_SANDBOX_URL`.

```
GET  /health             -> {"status": "ok"}

GET  /bins?names=a,b,c   -> {"bins": {"a": true, ...}, "missing": [...],
                             "all_present": bool}
     Eligibility checks: is this CLI installed *inside the sandbox*?

POST /exec               -> run one command in the sandbox
     {"argv": ["wc", "-l", "/skills/foo/data.txt"]}   # no shell, or:
     {"cmd": "printf 'a\nb\n' | wc -l"}               # via bash -c
     optional: "timeout" (s, default 30, max 300 — above max -> 400),
               "cwd" (default /home/sandbox), "env" ({k: v} overlay)
  -> {"exit_code": 0, "stdout": "...", "stderr": "...",
      "truncated": false, "stdout_truncated": false,
      "stderr_truncated": false, "timed_out": false, "duration_ms": 12}
```

Caps (env-tunable, see `.env.example`): request bodies over 256 KB are
rejected (413); per-stream output is capped at 256 KB and flagged via
`truncated` (the command still completes); a command still running at its
timeout is SIGKILLed with its whole process group and reported with
`timed_out: true`.

Try it from inside the api container:

```bash
docker compose exec api python3 -c "
import json, urllib.request
r = urllib.request.urlopen(urllib.request.Request(
    'http://skills-sandbox:8088/exec',
    data=json.dumps({'cmd': 'gog --version'}).encode(),
    headers={'Content-Type': 'application/json'}))
print(json.load(r))"
```

## Volumes

- **`~/.johnny/skills` → `/skills` (read-only here)** — skill packages
  (`<name>/SKILL.md`, openclaw/AgentSkills-compatible). The same host dir is
  mounted read-write into api/worker at the same `/skills` path, so skill
  paths need no per-container translation. Read-only in the sandbox so a
  runaway command cannot corrupt skill definitions. Created idempotently by
  `run.sh`.
- **`~/.johnny/sandbox-home` → `/home/sandbox`** — the sandbox user's home:
  gog credentials, tool dotfiles, scratch space. A host bind mount, so auth
  state survives image rebuilds **and** `./stop.sh` factory resets (same
  reasoning as the `~/.johnny/*-models` dirs). On a Linux host, chown it to
  uid 1000 (`sudo chown -R 1000 ~/.johnny/sandbox-home`); on macOS Docker
  Desktop this is handled by the file-sharing layer.

Nothing else from the host is mounted — no docker socket, no source tree.

## Security posture

- Runs as the non-root `sandbox` user (uid 1000).
- Compose resource limits: `cpus` (default 2), `mem_limit` (default 1g),
  `pids_limit` 256 (fork-bomb backstop). Tune via `JOHNNY_SANDBOX_CPUS` /
  `JOHNNY_SANDBOX_MEM_LIMIT` in `.env`.
- Network egress is allowed — CLI tools (gog, curl) need it.
- Per-exec timeout + output caps as above.

## Customizing the toolset

Two supported ways — both are image-build-time, so they survive every
`./stop.sh && ./run.sh` clean install (the repo's reproducibility rule):

1. **Edit the marked layer** in `sandbox/Dockerfile` (the
   `OPERATOR-CUSTOMIZABLE LAYER` block) — add `apt-get install` lines or
   binary downloads.
2. **Drop a script into `sandbox/setup.d/`** — every `*.sh` runs at build
   time, sorted, as root. See `setup.d/README.md`.

Then rerun `./run-dev.sh` (or `./run.sh`) to rebuild. Verify with
`GET /bins?names=yourtool` from the api container, or
`docker compose exec skills-sandbox which yourtool`.

Never hot-patch the running container (`docker compose exec skills-sandbox
apt-get install ...`) — it vanishes on the next rebuild.

## Workspace accounts (per-workspace gog auth)

Google identity lives on the **workspace** (Johnny-wks.4): connecting an
account stores its credentials in that workspace's gog **file keyring** —
authorize once and every agent attached to the workspace can use the
account in delegated tasks; no other workspace's container ever sees it.

**Connect from the UI** (the primary flow): the agent edit page →
Capabilities → *Workspace accounts* (the workspaces detail page gains the
same panel with Johnny-wks.5). Enter the email, finish the Google consent
in the tab that opens, done. The only manual prerequisite is the OAuth
client JSON in the default sandbox (steps 1–3 below, once per deployment);
workspace connects copy that app identity automatically.

**Where the state lives:**

| Workspace | gog state | Survives |
| --- | --- | --- |
| default | XDG layout under `~/.johnny/sandbox-home` (this section's flow, unchanged) | rebuilds, `./stop.sh`, clean installs |
| non-default | `~/.johnny/workspaces/<slug>/gog` bind-mounted at `/home/sandbox/gog`, announced via `GOG_HOME` to every process in that container | idle-TTL container restarts, `./stop.sh` (`down -v`), clean installs |

Because each workspace's credentials are a plain host directory,
cross-workspace isolation is checkable from the host:
`ls ~/.johnny/workspaces/<slug>/gog/data/keyring/`. Deleting a workspace
with "remove state" removes its gog dir too (credentials never outlive an
explicit state-removal choice). On a Linux host, chown new workspace dirs
to uid 1000 like sandbox-home (macOS Docker Desktop handles it).

**Callback-port strategy** (the wks.4 design decision): there is **no OAuth
callback listener and no published port**. The api drives gog's
remote/manual flow — `gog auth add --remote --step 1` (exec'd inside the
target workspace's container, lazy-starting it if needed) prints the
consent URL with `--redirect-uri` pointed at the api's
`GET /workspaces/accounts/oauth/callback`; Google redirects the operator's
browser there; the api relays the full redirect URL back into the same
container (`--step 2`), where gog exchanges the code and stores the
refresh token in the workspace's keyring. Connects are **serialized** —
one at a time across all workspaces, held as a Redis pending record with a
10-minute TTL and a visible UI lock (cancel anytime). Constraint inherited
from Google: `http://` redirect URIs are only allowed for loopback
addresses, so drive the UI via `localhost`/`127.0.0.1` (the same constraint
the legacy port-8089 flow had).

The port-8089 publish on this compose service remains only for the legacy
manual flow below; the UI flow never uses it.

## gog auth (one-time)

gog needs OAuth client credentials (the app identity) before any account —
default or workspace — can be connected. Steps 1–3 are the once-per-
deployment setup the UI connect flow depends on; step 4 is the legacy
manual way to add an account to the **default** sandbox (the UI does this
for you now, for any workspace).

**1. Switch to the file keyring backend** (headless container — no system keyring daemon):

```bash
docker compose exec skills-sandbox gog auth keyring file
```

**2. Set `GOG_KEYRING_PASSWORD`** in your `.env` (pick any non-empty string; keep it
consistent across all gog invocations):

```
GOG_KEYRING_PASSWORD=yourpassword
```

**3. Store your OAuth `credentials.json`** (downloaded from Google Cloud Console):

```bash
docker compose exec skills-sandbox gog auth credentials set --insecure \
  /home/sandbox/.config/gogcli/credentials.json
```

The file must be reachable inside the container. The easiest path: copy it to
`~/.johnny/sandbox-home/.config/gogcli/` on the host (that directory is
bind-mounted to `/home/sandbox/.config/gogcli/` in the container).

**4. Add your Google account:**

```bash
docker compose exec -it skills-sandbox \
  gog auth add --listen-addr=0.0.0.0:8089 your@email.com
```

Port `8089` is published to `127.0.0.1:8089` on the host (see `docker-compose.yml`),
so when Google redirects to `http://127.0.0.1:8089/oauth2/callback?...` your
browser reaches the callback server inside the container directly — no manual
curl step needed.

Auth state persists across rebuilds and `./stop.sh` factory resets via the
`~/.johnny/sandbox-home` bind mount.
