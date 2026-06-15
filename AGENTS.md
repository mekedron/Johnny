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

### Never upload screenshots without an explicit request

Capturing screenshots locally for validation is REQUIRED. **Uploading them is NOT.** Default: keep screenshots inside the gitignored `.validation/<task-id>/` tree and nowhere else. "Upload" here means any of: `git add` into a tracked path, attaching to a PR description / comment, pasting into Slack / email / external service, copying into `docs/`, `README.md`, `frontend/static/`, or any path that is checked in.

- Do NOT commit screenshots to tracked paths. No new `docs/screenshots/`, no PR-attached PNGs, no README hero images — unless the user explicitly asked for that specific upload in this conversation.
- Do NOT attach screenshots to GitHub PRs / issues / comments. Reference the local `.validation/<task-id>/NN-*.png` path for the reviewer instead.
- Do NOT include screenshots in artifacts you generate for the user (chat replies, summaries) when they did not ask for one.
- The user must say it: "commit this screenshot", "add this to the docs", "attach it to the PR", "use this as the hero image" — only then is uploading allowed, and only for the exact file(s) they pointed at.

If you think an upload would help, ASK first. Default to the local-path note.

---

## Top rule: Docker is the only runtime — never run services on the host

Every long-running service in this project (frontend, api, worker, postgres, redis, meet-worker) is defined in `docker-compose.yml` and **must** be started, stopped, and exec'd through the compose stack. Do not run `pnpm dev`, `npm install`, `pip install`, `uvicorn`, `pytest`, `psql`, or any other "I'll just run it directly to test" shortcut on the host.

Why this rule exists: a host-side `pnpm dev` for the frontend survives terminal close (its PPID becomes 1), silently steals port 5173 from the dockerized frontend, and `./run.sh` then fails to bind — a stray vite ran orphaned for 24 hours before anyone noticed it was masquerading as the "real" UI. Host language-runtime versions also drift from the container images and produce subtle "works on my machine" bugs. Both classes of mistake have shipped broken code to the user before; the rule exists because of it.

**Use these — and only these — to interact with the stack:**

- `./run.sh` — starts the full stack in **production-shape** mode (source baked into images via `COPY`; code changes require an image rebuild). `docker compose up -d --build`. Also sweeps any host orphan on 5173 before bringing the dockerized frontend up.
- `./run-dev.sh` — starts the full stack in **hot-reload mode**. Layers `docker-compose.dev.yml` on top of the base file, bind-mounts `./frontend` + `./backend` into the containers, swaps the api command for `uvicorn --reload` and the worker command for `watchfiles ... python -m app.worker`. Saves on the host trigger a reload in seconds — no rebuild needed for source changes. Dependency changes (`pyproject.toml` / `package.json`) still need `./run-dev.sh` to rerun, since they're installed at image-build time.
- `./stop.sh` — full `docker compose down -v` reset. Also kills `meet-worker-session-*` orphan containers and any host process still listening on 5173. Works for both `./run.sh` and `./run-dev.sh` stacks.
- `docker compose exec <service> <cmd>` — for any one-off command inside a running service. Examples:
  - Backend tests: `docker compose exec api pytest` (run against the `./run-dev.sh` stack — `tests/` reaches the container via the bind mount; the prod image deliberately excludes it via `.dockerignore`)
  - Frontend tests / build / lint: `docker compose exec frontend pnpm test` (or `pnpm build`, etc.)
  - DB shell: `docker compose exec postgres psql -U johnny johnny`
  - Redis CLI: `docker compose exec redis redis-cli`
  - Open a shell: `docker compose exec api bash`
- `docker compose logs -f [service]` — tail logs (omit the service name to follow all).
- `docker compose build <service>` then `docker compose up -d <service>` — rebuild a single service after a dependency change without restarting everything.

**Allowed on the host:** `git`, `bd`, `bv`, `gh`, file edits in the source tree, `docker` / `docker compose` itself, and the `start-chrome.sh` helper (per the browser-validation rule).

**Choosing between `./run.sh` and `./run-dev.sh`**: use `./run-dev.sh` for normal day-to-day work — saves on the host reload in the container in seconds with no rebuild. Use `./run.sh` when you need to verify the production-shape image (e.g. before a release, or to confirm a fix lands in the baked image rather than relying on a bind mount). For either mode, dependency changes (`pyproject.toml`, `package.json`, `uv.lock`, `pnpm-lock.yaml`) still require rerunning the script so compose can rebuild the affected image layer.

**Not allowed on the host:** `pnpm` / `npm` / `pip` / `python` / `uvicorn` / `pytest` against project code, or a locally-installed `postgres` / `redis` / `node` used as a substitute for the container. If the stack is broken in a way that tempts you to bypass Docker, **stop and fix the compose-side problem** — bypassing it just ships bugs back to the user, and any host process you leave behind will fight the next `./run.sh` for ports and volumes.

---

## Top rule: clean-install reproducibility — never "fix" by hot-patching a running container

Every fix you ship has to survive the canonical clean-install cycle the operator runs:

```bash
./stop.sh    # docker compose down -v + sweep orphans
./run.sh     # fresh `docker compose up -d --build`
# now use the feature
```

If a feature requires a package, a model file, a config blob, a downloaded asset, or any other piece of state that did not exist on a fresh checkout, **it has to be added at the layer that `./run.sh` rebuilds from**:

- **A new Python package** the api / worker needs at runtime → add it to `backend/pyproject.toml` (under the right optional extra) AND make sure `backend/Dockerfile` installs that extra, OR adopt the Parakeet-style runtime-install pattern (`POST /providers/{id}/package/install` + `~/.johnny/<name>-packages` bind-mount). Do not `docker compose exec api pip install …`; that change vanishes the next `./stop.sh`.
- **A new sidecar dependency** (sidecar venv, Swift package, model checkpoint) → pin it in the sidecar's `pyproject.toml` / `Package.swift` / install script. The sidecar launcher (`scripts/start-*-sidecar.sh`) is the only place that should produce the runnable artifact; first launch on a clean checkout must produce a working sidecar without any extra `pip install` / `swift build` / `brew install` the operator runs by hand.
- **A new frontend dep** → add to `frontend/package.json`; the dev-mode bind mount and prod-mode image build both pick it up automatically once `pnpm install` runs in the image. Never `docker compose exec frontend pnpm add …`.
- **A new env var, a new bind-mount, a new model directory** → wire it in `docker-compose.yml`, `.env.example`, and `./run.sh` (which creates the host bind-mount dirs idempotently before bringing the stack up). Do not assume the operator will `mkdir` something themselves.

**Verification before closing any task that touches runtime deps:**

```bash
./stop.sh
./run.sh
# then exercise the feature end-to-end from the UI (chrome-devtools MCP)
# capture the screenshot / log line under .validation/ for the PR description
```

If that cycle does not reproduce success on your machine, you are not done. If you close a bd issue and the operator hits the same error on their next clean install, you hot-patched a running container instead of fixing the source-of-truth artifact — that is the failure mode this rule exists to prevent.

Why this rule exists: closed bd issues that "worked when the agent tested" have repeatedly re-failed for the operator on the next `./stop.sh && ./run.sh` because the agent ran `pip install` inside the running api container or `pip install` inside the running sidecar venv, neither of which survives a compose rebuild. The operator does not SSH into containers to fix their machine; the stack has to come up clean from the scripts on every host.

