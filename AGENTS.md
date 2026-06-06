# Agent Instructions

## Top rule: real-browser validation is mandatory

Anything that *can* be verified through a real browser **must** be verified through a real browser, via the **chrome-devtools MCP server** (`mcp__chrome-devtools__*`). No exceptions, no matter how small the change.

This applies to every UI surface in this project — provider settings, voice catalog browser, Test / Sample buttons, session list, leave-now button, calendar import, sign-in flow, error toasts, every form, every dropdown. A passing unit/integration test is necessary but **not sufficient**; the change is not done until it has been driven in the real browser and seen working.

Rules:
- Use **chrome-devtools MCP only**. Do **not** use `claude-in-chrome` (see `/Users/nikita/.claude/rules/common/browser-automation.md`).
- Load the tools you need via ToolSearch before calling them (e.g. `select:mcp__chrome-devtools__navigate_page,mcp__chrome-devtools__take_snapshot,mcp__chrome-devtools__click,mcp__chrome-devtools__list_network_requests`).
- For each user-visible change, the verification trace must include at minimum: navigate to the page, take a snapshot, drive the relevant interaction, assert the resulting DOM/network state, and capture a screenshot for the PR description.
- "I read the code and it looks right" is not validation. "The unit test passes" is not validation. Only a recorded chrome-devtools MCP run is.
- If the change literally cannot be browser-tested (pure backend migration with no UI surface, a cron-only worker), state that explicitly in the PR description — otherwise assume browser validation is required.

You can run and test anything in Chrome browser using `chrome-devtools` MCP server. NEVER use `claude-in-chrome` MCP — always use `chrome-devtools` MCP exclusively for all browser automation.

The `chrome-devtools` MCP is configured to attach to a long-lived Chrome on `http://127.0.0.1:9222` (see `.mcp.json`), so the same profile is shared across every Claude Code session and sub-agent. Before the first `chrome-devtools` tool call in a session, run `./scripts/start-chrome.sh` from the project root. It is idempotent — if the proper Chrome is already up it exits 0 with no side effects, so it is safe to run every time. If it reports that the profile is locked by an old Chrome instance, follow the `pkill` instruction it prints and re-run it.

This rule sits at the top of this file because skipping it has repeatedly shipped "fixes" that don't actually work for the user.

### Where to save browser-validation artifacts

All chrome-devtools MCP outputs (screenshots, snapshots, console dumps, network captures) MUST be written under a single gitignored root:

```
.validation/<task-id>/NN-short-description.<ext>
```

- `<task-id>` is the bd issue or PR slug you're working on (e.g. `Johnny-uzz`, `pages-deploy`, `voice-catalog-307-fix`). One sub-folder per task — do NOT create a new top-level `.validation-*` directory per session.
- `NN-short-description.png` keeps captures ordered by step (`01-hero.png`, `02-modes.png`, `03-mobile-menu.png`). Use jpg/webp only when png would be wastefully large.
- The whole `.validation/` tree is gitignored (`.gitignore` rules: `.validation/` and the legacy `.validation-*/`). **Never `git add` anything in there.** If a screenshot is genuinely needed in the repo (e.g. README hero image), copy it into the proper docs path with an intentional filename — don't promote a validation scratch file.
- Reference these paths in your PR description if useful, but link to them as local-path notes for the reviewer; do not check them in.
- When `mcp__chrome-devtools__take_screenshot` complains that the path is outside the workspace roots, use the `.validation/<task-id>/...` path inside the repo (it's in the workspace root and works).

The legacy `.validation-*-artifacts/` convention from earlier sessions is deprecated. Existing dirs were untracked from git and the pattern is gitignored — keep all new captures under `.validation/<task-id>/` only.

---

## Top rule: Docker is the only runtime — never run services on the host

Every long-running service in this project (frontend, api, worker, postgres, redis, meet-worker) is defined in `docker-compose.yml` and **must** be started, stopped, and exec'd through the compose stack. Do not run `pnpm dev`, `npm install`, `pip install`, `uvicorn`, `pytest`, `psql`, or any other "I'll just run it directly to test" shortcut on the host.

Why this rule exists: a host-side `pnpm dev` for the frontend survives terminal close (its PPID becomes 1), silently steals port 5173 from the dockerized frontend, and `./run.sh` then fails to bind — a stray vite ran orphaned for 24 hours before anyone noticed it was masquerading as the "real" UI. Host language-runtime versions also drift from the container images and produce subtle "works on my machine" bugs. Both classes of mistake have shipped broken code to the user before; the rule exists because of it.

**Use these — and only these — to interact with the stack:**

- `./run.sh` — starts the full stack in **production-shape** mode (source baked into images via `COPY`; code changes require an image rebuild). `docker compose up -d --build`. Also sweeps any host orphan on 5173 before bringing the dockerized frontend up.
- `./run-dev.sh` — starts the full stack in **hot-reload mode**. Layers `docker-compose.dev.yml` on top of the base file, bind-mounts `./frontend` + `./backend` into the containers, swaps the api command for `uvicorn --reload` and the worker command for `watchfiles ... python -m app.worker`. Saves on the host trigger a reload in seconds — no rebuild needed for source changes. Dependency changes (`pyproject.toml` / `package.json`) still need `./run-dev.sh` to rerun, since they're installed at image-build time.
- `./stop.sh` — full `docker compose down -v` reset. Also kills `meet-worker-session-*` orphan containers and any host process still listening on 5173. Works for both `./run.sh` and `./run-dev.sh` stacks.
- `docker compose exec <service> <cmd>` — for any one-off command inside a running service. Examples:
  - Backend tests: `docker compose exec api pytest`
  - Frontend tests / build / lint: `docker compose exec frontend pnpm test` (or `pnpm build`, etc.)
  - DB shell: `docker compose exec postgres psql -U postgres johnny`
  - Redis CLI: `docker compose exec redis redis-cli`
  - Open a shell: `docker compose exec api bash`
- `docker compose logs -f [service]` — tail logs (omit the service name to follow all).
- `docker compose build <service>` then `docker compose up -d <service>` — rebuild a single service after a dependency change without restarting everything.

**Allowed on the host:** `git`, `bd`, `bv`, `gh`, file edits in the source tree, `docker` / `docker compose` itself, and the `start-chrome.sh` helper (per the browser-validation rule).

**Choosing between `./run.sh` and `./run-dev.sh`**: use `./run-dev.sh` for normal day-to-day work — saves on the host reload in the container in seconds with no rebuild. Use `./run.sh` when you need to verify the production-shape image (e.g. before a release, or to confirm a fix lands in the baked image rather than relying on a bind mount). For either mode, dependency changes (`pyproject.toml`, `package.json`, `uv.lock`, `pnpm-lock.yaml`) still require rerunning the script so compose can rebuild the affected image layer.

**Not allowed on the host:** `pnpm` / `npm` / `pip` / `python` / `uvicorn` / `pytest` against project code, or a locally-installed `postgres` / `redis` / `node` used as a substitute for the container. If the stack is broken in a way that tempts you to bypass Docker, **stop and fix the compose-side problem** — bypassing it just ships bugs back to the user, and any host process you leave behind will fight the next `./run.sh` for ports and volumes.

---

This project uses **bd** (beads) for issue tracking. Run `bd prime` for full workflow context.

> **Architecture in one line:** Issues live in a local Dolt database
> (`.beads/dolt/`); cross-machine sync uses `bd dolt push/pull` (a
> git-compatible protocol), stored under `refs/dolt/data` on your git
> remote — separate from `refs/heads/*` where your code lives.
> `.beads/issues.jsonl` is a passive export, not the wire protocol.
>
> See [SYNC_CONCEPTS.md](https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md)
> for the one-screen overview and anti-patterns (don't treat JSONL as the
> source of truth; don't `bd import` during normal operation; don't
> reach for third-party Dolt hosting before trying the default).

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work atomically
bd close <id>         # Complete work
bd dolt push          # Push beads data to remote
```

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->

<!-- bv-agent-instructions-v2 -->

---

## Beads Workflow Integration

This project uses [beads](https://github.com/gastownhall/beads) (`bd`) for issue tracking and [beads_viewer](https://github.com/Dicklesworthstone/beads_viewer) (`bv`) for graph-aware triage. Issues are stored in `.beads/` and tracked in git.

### Using bv as an AI sidecar

bv is a graph-aware triage engine for Beads projects (.beads/beads.jsonl). Instead of parsing JSONL or hallucinating graph traversal, use robot flags for deterministic, dependency-aware outputs with precomputed metrics (PageRank, betweenness, critical path, cycles, HITS, eigenvector, k-core).

**Scope boundary:** bv handles *what to work on* (triage, priority, planning). `bd` handles creating, modifying, and closing beads.

**CRITICAL: Use ONLY --robot-* flags. Bare bv launches an interactive TUI that blocks your session.**

#### The Workflow: Start With Triage

**`bv --robot-triage` is your single entry point.** It returns everything you need in one call:
- `quick_ref`: at-a-glance counts + top 3 picks
- `recommendations`: ranked actionable items with scores, reasons, unblock info
- `quick_wins`: low-effort high-impact items
- `blockers_to_clear`: items that unblock the most downstream work
- `project_health`: status/type/priority distributions, graph metrics
- `commands`: copy-paste shell commands for next steps

```bash
bv --robot-triage        # THE MEGA-COMMAND: start here
bv --robot-next          # Minimal: just the single top pick + claim command

# Token-optimized output (TOON) for lower LLM context usage:
bv --robot-triage --format toon
```

Before claiming, verify current state with `bd show <id> --json` or `bd ready --json`. `recommendations` can include graph-important blocked or assigned work; only `quick_ref.top_picks` and non-empty `claim_command` fields represent claimable work.

#### Other bv Commands

| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with unblocks lists |
| `--robot-priority` | Priority misalignment detection with confidence |
| `--robot-insights` | Full metrics: PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core |
| `--robot-alerts` | Stale issues, blocking cascades, priority mismatches |
| `--robot-suggest` | Hygiene: duplicates, missing deps, label suggestions, cycle breaks |
| `--robot-diff --diff-since <ref>` | Changes since ref: new/closed/modified issues |
| `--robot-graph [--graph-format=json\|dot\|mermaid]` | Dependency graph export |

#### Scoping & Filtering

```bash
bv --robot-plan --label backend              # Scope to label's subgraph
bv --robot-insights --as-of HEAD~30          # Historical point-in-time
bv --recipe actionable --robot-plan          # Pre-filter: ready to work (no blockers)
bv --recipe high-impact --robot-triage       # Pre-filter: top PageRank scores
```

### bd Commands for Issue Management

```bash
bd ready              # Show issues ready to work (no blockers)
bd list --status=open # All open issues
bd show <id>          # Full issue details with dependencies
bd create --title="..." --type=task --priority=2
bd update <id> --status=in_progress
bd close <id> --reason="Completed"
bd close <id1> <id2>  # Close multiple issues at once
bd export             # Export issues to JSONL
```

### Workflow Pattern

1. **Triage**: Run `bv --robot-triage` to find the highest-impact actionable work
2. **Claim**: Use `bd update <id> --status=in_progress`
3. **Work**: Implement the task
4. **Complete**: Use `bd close <id>`
5. **Sync**: Always run `bd export` at session end

### Key Concepts

- **Dependencies**: Issues can block other issues. `bd ready` shows only unblocked work.
- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog (use numbers 0-4, not words)
- **Types**: task, bug, feature, epic, chore, docs, question
- **Blocking**: `bd dep add <issue> <depends-on>` to add dependencies

### Session Protocol

```bash
git status              # Check what changed
git add <files>         # Stage code changes
bd export               # Export beads changes to JSONL
git commit -m "..."     # Commit everything
git push                # Push to remote
```

<!-- end-bv-agent-instructions -->
