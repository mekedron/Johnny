# Workspaces — first-class execution environments attached to agents (Johnny-wks)

A **workspace** is a named execution environment: one skills-sandbox
container instance + one host state directory (skill packages, tool state,
and the optional `gog` keyring a developer may set up by hand). Agents
attach to exactly one workspace (`agents.workspace_id`); everything a session
or delegated task does in the sandbox — which skills exist, which binaries
run, and (if `gog` is configured) *as whom* it talks to Google — happens
inside the attached agent's workspace and nowhere else.

Two axes, deliberately orthogonal:

- **Policy** ([CAPABILITY-POLICY.md](CAPABILITY-POLICY.md)) is the **OFFER**
  axis — what a session may run (catalog kinds, exec bins). It encodes no
  identity.
- **The workspace** is the **STATE** axis — *as whom* and *against whose
  state* an allowed capability acts (skill packages, ledgers, keyrings).
  Credentials are state, not permissions.

Sharing is the feature, not a leak: every agent attached to a workspace
uses the same skill install (and, where a developer has configured `gog`,
the same keyring) — one setup, many agents. Isolation is the complement:
nothing in a workspace is reachable from an agent attached to a different
one, at any layer (host paths, container mounts, rendered catalogs, prompt
blocks).

## The entity (wks.1)

`workspaces` table (migration 0032): `name` (unique, renameable), `slug`
(**frozen at create** — it keys the storage convention below), description,
`is_default` (partial-unique). A non-deletable **Default** workspace is
seeded at boot; `agents.workspace_id` is a nullable FK where **NULL means
the default workspace** (the NULL-inherits-default convention — pre-existing
agents keep byte-identical behavior). The effective workspace is resolved at
**dispatch time** (`app/services/workspaces.py::resolve_agent_workspace`),
stamped into `bot_sessions.agent_snapshot` (`workspace_id` + `workspace
{id,name,slug,is_default}`) and into each delegated task's `request_json` —
turn-time and worker code read the stamp, never the DB.

CRUD: `GET/POST /workspaces`, `GET/PATCH/DELETE /workspaces/{id}`. Renames
keep the slug; deleting the default or an attached workspace is a 409;
delete retires the container always, and removes the state volume + host
gog dir only with the explicit `?remove_volume=true`.

## Execution environments (wks.2)

| | Default workspace | Named workspace |
| --- | --- | --- |
| Container | the always-on compose `skills-sandbox` service | `johnny-workspace-<id>`, **lazily launched** from the same `johnny-skills-sandbox:latest` image on first need (dispatch, worker claim, capabilities GET, manual Start) |
| Sandbox URL | `SANDBOX_URL` env (compose service) | `http://johnny-workspace-<id>:8088` |
| Home (`/home/sandbox`) | host bind `~/.johnny/sandbox-home` | named volume `johnny-workspace-<id>-home` |
| Lifecycle | compose up/down | idle-TTL sweep: stopped + removed after `JOHNNY_WORKSPACE_IDLE_TTL_SECONDS` (1800) idle, checked every `JOHNNY_WORKSPACE_SWEEP_INTERVAL_SECONDS` (60); volume untouched, next ensure restarts transparently |

Launcher containers carry `johnny.workspace-id` / slug labels, `init: true`,
the compose service's cpu/mem/pids caps, and join `johnny_default`. The
manager is `app/services/workspace_containers.py::WorkspaceContainerManager`
(gated by `JOHNNY_USE_DOCKER_LAUNCHER`); container start/stop/retire publish
`johnny.workspace.sandbox-changed` events so the worker invalidates exactly
that workspace's registry snapshot — no restarts anywhere.

**Factory-reset continuity**: launcher volumes are not compose-declared, so
`./stop.sh`'s `down -v` cannot delete them, and host dirs survive
everything. A clean install (`./stop.sh && ./run.sh`) wipes the DB; recreate
workspaces in the same order and they re-adopt their volumes and host dirs
(same-id / same-slug adoption — the documented continuity).

## Storage convention (wks.3/wks.4)

```
~/.johnny/
  skills/                      # DEFAULT workspace skill packages
                               # (repo ./skills re-seeded by run.sh; operator dirs kept)
  sandbox-home/                # DEFAULT workspace home (XDG gog state lives here)
  workspaces/<slug>/           # one dir per NAMED workspace (slug is frozen)
    skills/                    # its skill packages — mounted ro at /skills
    gog/                       # its gog identity — mounted rw at /home/sandbox/gog
```

Every container sees **its own** skill set at the same `/skills` path (the
mount *source* swaps, never the path), so `SKILL.md` argv like
`bash /skills/<name>/run.sh` stays relocatable across workspaces. The
api/worker/agent-worker discover all trees through one parent mount
(`~/.johnny/workspaces` → `JOHNNY_WORKSPACES_DIR=/workspaces`).

Skill installs go through `POST /capabilities/skills/install`
(`workspace_id` targets the volume; `null` = default) — strict 422s, 409 +
`overwrite`, and the package lands in **that workspace's tree only**. The
next capabilities GET / session assembly / worker claim re-scans; no reload
call exists because none is needed.

## Routing (wks.1/wks.3)

Three resolver pairs key everything off the snapshot stamp — session side in
`johnny/agent/job_session.py`, worker side in
`app/services/task_worker.py`:

| What | Session resolver | Worker resolver |
| --- | --- | --- |
| Sandbox URL | `resolve_session_sandbox_url` | `resolve_sandbox_url` |
| Skills dir | `resolve_session_skills_dir` | `resolve_skills_dir` |

Default/legacy stamps resolve to the env-configured URL/dir byte-identically
(pre-workspaces behavior); a non-default stamp without a slug yields an
**empty registry** — promise nothing rather than guess. An unreachable
workspace endpoint degrades to unavailable/ineligible skills with honest
spoken declines, never a crash (`load_skill_registry` never raises).

The capabilities API is workspace-keyed
(`GET /capabilities/skills|tools?workspace_id=`,
`/tools?agent_id=` derives the agent's workspace exactly like dispatch), so
the UI inventory, the router catalog, and the executor all answer from the
same per-workspace truth. See
[ROUTING.md](ROUTING.md) §2 for how the catalog reaches the prompts.

## gog identity (optional, developer-configured)

Per-workspace `gog` state is **one bind + one env var**: the workspace's
`gog/` dir is mounted rw at `/home/sandbox/gog` and announced via
`GOG_HOME`, so every exec'd process — skills included — reads that
workspace's keyring with zero skill changes. The default workspace keeps
its XDG state under `sandbox-home/` unchanged.

`gog` itself is **optional and not an app feature**: the app neither manages
nor depends on it. A developer configures it by hand inside the container
(`gog auth add`, documented in [sandbox/README.md](../sandbox/README.md)),
and the keyring then survives idle-TTL restarts and factory resets (it's a
host dir). Skills like `google-calendar` call `gog` at runtime and report
themselves *unavailable* until it is set up. There is **no account UI and no
backend** that connects, lists, or removes gog accounts.

> The app's **only** managed Google identity is the **meeting-bot account** —
> the identity an agent signs in as to *join* a Meet — and it lives on the
> AGENT (`agents.meeting_bot_account_id`, managed on the agent edit page),
> not on the workspace. It is unrelated to `gog`.

## UI (wks.5)

`/workspaces` lists workspaces with container state (Always on / Running /
Stopped / Never started — "stopped vs never-started" is decided by volume
existence), storage paths, attached-agent chips, create/rename/delete.
`/workspaces/{id}` adds container Start/Stop and the per-workspace inventory
(probe-gated when the container is idle, because the capabilities GET
lazily starts it). The agent edit page carries the workspace attachment
picker (default preselected; explicit null = back to default) and the
meeting-bot account picker (the agent's Google identity for joining Meets).

## The canonical least-privilege scenario (wks.6)

The operator's scenario, live: a **finance** workspace holds the
`financial-reports` skill, its ledger data file, and its credential; the
**management-meeting agent** attaches to it. The **progress-meeting agent**
stays on the default workspace with a per-agent policy allowing only
calendar + tasks. Asking the progress agent for financials yields a spoken
decline naming the reason (the kind is absent from its catalog and prompt
blocks — and its policy wouldn't offer it either); asking the management
agent delegates to the skill and speaks the ledger numbers. Attaching a
second agent to finance inherits the skill **and** the credential with zero
re-install/re-auth — sharing is the abstraction's win.

Reproduce it against a running stack with
[`scripts/finance_workspace_capstone.py`](../scripts/finance_workspace_capstone.py):

```bash
# land the fixture package (skill + ledger + credential) in the finance workspace
./scripts/finance_workspace_capstone.py install --workspace finance

# structural isolation/sharing assertions across every layer
./scripts/finance_workspace_capstone.py assert \
    --finance-workspace finance \
    --progress-agent "Progress Meeting" \
    --management-agent "Management Meeting" \
    --analyst-agent "Finance Analyst" \
    --progress-session <id> --management-session <id> --analyst-session <id>
```

The assert pass checks host paths (`~/.johnny/workspaces/finance` holds the
package; the default trees and every other workspace hold none of it),
rendered catalogs (default vs workspace-keyed skills, per-agent tools),
policy attribution (`POST /capability-policies/resolve` names the denying
layer for the progress agent), and — given session ids — the snapshot
stamps, the decision rows' rendered router context, the delegated task, and
the spoken figures. The recorded run for Johnny-wks.6 lives under
`.validation/Johnny-wks.6/`.

## Cross-references

- [CAPABILITY-POLICY.md](CAPABILITY-POLICY.md) — the offer axis this state
  axis composes with.
- [ROUTING.md](ROUTING.md) — catalog assembly, availability probes,
  delegation degrade order.
- [TASK-ENGINE.md](TASK-ENGINE.md) — the worker that claims tasks and execs
  them in the stamped workspace's sandbox.
- [MCP.md](MCP.md) — MCP tools join the same catalog/policy namespace.
