# Capability Policy — layered allow/deny + editable safe-bins (Johnny-trt.38)

Configurable control over **what a session may run**: which task kinds the
router may offer/delegate, which skills exist for an agent, and which
binaries `sandbox.exec` may invoke. Follows openclaw's layered resolution
model (`src/agents/agent-tools.policy.ts` — scope overrides, deny wins at
every merge), Johnny-shaped: DB-backed rows, resolved once per session into
the agent snapshot, re-resolved fresh per claimed task by the worker.

Policy is the **OFFER** axis. It deliberately encodes no identity: *as whom*
an agent acts (gog keyring, credentials) is sandbox **STATE** — Phase 7's
per-agent sandboxes (`agent.sandbox_mode = global | personal`) make the
least-privilege guarantee physical underneath this layer, and the same
policy stays valid in either mode.

## The resolution order (normative)

```
GLOBAL  →  PER-AGENT  →  PER-SESSION-MODE  →  PER-SESSION OVERRIDE
```

Unit-pinned by `backend/tests/skills/test_capability_policy.py`. Merge rules
walking the layers in that order:

| List | Semantics |
| --- | --- |
| `tools_deny` | **Accumulates.** A match at ANY layer denies — deny wins at every merge (a global deny beats an agent/mode/session allow; a mode deny beats a session allow). Earliest matching layer wins attribution. |
| `tools_allow` | Non-empty **redefines** the restrictive allow-list (the openclaw scope-override rule): from that layer on, only matching tools are allowed. Use `tools_deny` for guarantees that must survive lower layers. |
| `tools_also_allow` | **Extends** the allow-list currently in force without replacing it. No-op while everything is still allowed. |
| `bins_deny` | Accumulates like `tools_deny`, over exec binaries (`argv[0]` basenames). |
| `safe_bins` | The editable trt.35 baseline toolset — **global layer only**. Unset = the built-in `BASELINE_BINS`. A removed baseline bin is hard-denied (beats skill `requires.bins` grants); an added bin is granted. Reset-to-default = deleting the key (or the global row). |

Tool names are capability-catalog kinds — internal tools (`meeting.leave`,
`session.end`), skill kinds (`google-calendar`), future MCP tools
(`mcp__<server>__<tool>`). **Per-skill enable/disable is expressed through
the same lists** (deny the skill's kind): a denied skill leaves the catalog,
is refused by the worker, and its `requires.bins` grants leave the exec
allow set. Matching is `fnmatch` globbing (`*`, `?`, `[seq]`) —
`mcp__shady__*` denies a whole server.

The default (no rows) is **unrestricted**: every catalog kind offered, the
built-in baseline + skill grants allowed — exactly the pre-trt.38 behavior.

## Storage

`capability_policies` (migration 0030) — at most one row per scope target
(partial unique indexes; target shape CHECK-enforced):

| scope | target key | meaning |
| --- | --- | --- |
| `global` | — | the install-wide layer (also owns `safe_bins`) |
| `agent` | `agent_id` (CASCADE) | the per-agent layer — the canonical least-privilege home |
| `session_mode` | `session_mode` ∈ `meet`/`browser` | meeting sessions can be stricter than the playground |
| `session` | `bot_session_id` (CASCADE) | one session's override |

`document` is the JSON shape above, canonicalized on write
(`CapabilityPolicyLayer.from_document → to_document`).

## Resolution + freshness (no restarts, no caches)

- `johnny/skills/capability_policy.py` — the pure engine:
  `resolve_policy(layers)` → `ResolvedCapabilityPolicy` with
  `check_tool(name)` / `check_bin(name)` returning
  `PolicyDecision(allowed, layer, rule, detail)` — the deciding layer is
  always named.
- **Dispatch surfaces** (`session_scheduler`, browser session start) resolve
  via `app/services/capability_policies.resolve_capability_policy` and stamp
  `ResolvedCapabilityPolicy.to_payload()` into the trt.41 agent snapshot
  (`capability_policy` key). Turn-time code reads
  `SessionJobConfig.capability_policy()` — **never the policy tables**.
  Legacy/missing payloads degrade to unrestricted.
- **The worker** re-resolves fresh from the DB per claimed task
  (`resolve_policy_for_bot_session`) — a policy edit bites a *running*
  session's next delegation with zero restarts (live-proven:
  `.validation/Johnny-trt.38/02-worker-no-restart-enforcement.txt`). A
  failed policy read fails the task closed with could-not-verify speech
  (the trt.55 probe-failure stance).

## Enforcement points (three, per the bead)

1. **Catalog filtering** (`apply_policy_to_catalog`, applied in
   `build_agent_runtime`): a policy-denied kind becomes a
   `hidden=True, available=False` entry — **absent from the rendered router
   catalog and the answer model's capability notes** (the canonical
   scenario: the progress agent's prompt never even mentions finance
   kinds), while staying in the catalog tuple so the gate's trt.55
   backstop still owns a forced attempt. `keywords=()` keeps the trt.50
   scorer prior silent. Kinds stay in `executor_kinds` so a forced
   delegate hits the *unavailable* degrade (policy-flavored spoken
   decline), never the unknown-kind degrade.
2. **Executor tool dispatch** (`TaskWorker._run_claimed`): the
   freshly-resolved policy refuses a denied kind before any runner work —
   settles `failed` with spoken-form policy copy, error naming the layer.
3. **sandbox.exec argv[0]** (`compute_allowed_bins(policy=…)` +
   `ExecBinPolicy(policy_check=…)`): the edited safe-bins baseline replaces
   `BASELINE_BINS`, denied skills' grants never enter the union, `bins_deny`
   globs and removed baseline bins are filtered out, and a denial is
   attributed (`ExecDenial.policy_layer/rule`) for the event.

## Observability

Every **enforced** denial (never the silent catalog filtering) emits a
`policy_denied` conversation event → `conversation_events` row with
`reason` = **the denying layer** and
`details = {capability, capability_kind: tool|bin, rule, layer_detail,
surface: router_gate|worker|sandbox_exec}`.

- gate surface: `RouterGate._emit_policy_denied` via
  `build_policy_denied_emitter` (fires when a delegate verdict degrades over
  a policy-hidden kind; the `CAPABILITY_GAP_KEY` marker carries the policy
  attribution into the decision row).
- worker / sandbox_exec surfaces: published by the worker on the session
  channel; the status subscriber persists (`apply_conversation_event`).

## API (powers the trt.37 UI)

```
GET    /capability-policies                     # all rows + the built-in baseline
GET    /capability-policies/effective?agent_id&session_mode&bot_session_id
POST   /capability-policies/resolve             # {tool|bin, coordinates} → allowed + deciding layer
PUT    /capability-policies/global              # upsert a layer document
PUT    /capability-policies/agents/{id}
PUT    /capability-policies/session-modes/{meet|browser}
PUT    /capability-policies/sessions/{id}
DELETE … (same paths)                           # reset a layer (global delete = baseline reset)
```

`POST /resolve` is the effective-policy inspector from the acceptance: input
a tool kind or exec binary + scope coordinates, output
`{allowed, layer, rule, detail, layers_consulted}` — exactly what a denial
would record in its event.

## Out of scope (documented extension points)

- **Per-flag bin profiles** (openclaw's grep-stdin-only style): explicitly
  out — the sandbox container is Johnny's security boundary, so bin-level +
  glob control suffices. The hook, should a future bead revisit: the
  `ExecBinPolicy.policy_check` callable / `_denial` seam returns a
  structured verdict per binary and could inspect argv there without
  reshaping callers (see `johnny/skills/policy.py` module docstring).
- **MCP health** (trt.36) joins the *availability* predicate
  (`evaluate_skill_availability`), not this policy; MCP tools join the
  policy namespace as `mcp__<server>__<tool>` kinds automatically.
- **Per-agent sandboxes** (Phase 7) change *where/as-whom*, not this layer;
  the worker's `resolve_sandbox_url` seam and this policy compose unchanged.
