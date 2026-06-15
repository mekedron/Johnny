# Session-View Re-Imagining — working folder

**Self-contained workspace for the redesign of the session/history "What the bot is thinking" view into three
correlated views: router decisions · what the bot delivered (and to which request) · background tasks.**

> All work for this initiative lives **here**, not scattered across `docs/`, so multiple agents can collaborate
> without colliding with the rest of the documentation. Add new artifacts to this folder.

## Contents

| File | What it is |
|---|---|
| [`PRD.md`](./PRD.md) | **Build from this.** The **unified v2 PRD** (senior-architect reconciliation of both proposals): phased rollout (UI-win-first) with quality gates, user stories per phase, functional requirements, non-goals. Operator decisions resolved 2026-06-15. |
| [`DESIGN.md`](./DESIGN.md) | **Current-state reference.** Architecture, why the UI fails, the seams, solution directions A/B/C, §10 resolved decisions, data-model gaps. Facts verified against code + live DB on 2026-06-15. |
| [`RED-TEAM-REVIEW.md`](./RED-TEAM-REVIEW.md) | Independent cross-check of both proposals; the five decisions it raised are answered in the unified PRD. |
| [`OPTIONS-SCORED.md`](./OPTIONS-SCORED.md) | Adversarially-scored P1–P4 architecture options (they stack, not compete). |
| [`SYNTHESIS-RECOMMENDATION.md`](./SYNTHESIS-RECOMMENDATION.md) | Second-opinion synthesis (request_id + inline-on-`agent_tasks`); kept for the record. |
| [`SESSION-WORKSTREAMS-PRD.codex.md`](./SESSION-WORKSTREAMS-PRD.codex.md) | **Superseded.** The original Codex "Proposal 2" (new `agent_workstreams` entity), merged into the unified PRD. |
| `research/SYNTHESIS.md`, `research/*.md` | The 8 raw subsystem investigation reports + their synthesis. |

## Status

- ✅ Investigation complete (8-agent workflow + first-hand browser trace of session 3 + live DB verification).
- ✅ Current-state + problem + options documented (`DESIGN.md`).
- ✅ Two parallel proposals (this folder's DESIGN/PRD + the Codex `agent_workstreams` PRD) reviewed, judged, and
  **unified by a senior-architect pass** (2026-06-15); operator decisions resolved (`DESIGN.md` §10).
- ✅ **Unified v2 PRD written (`PRD.md`)** — phased rollout, UI-win-first, full v1 scope, new `agent_workstreams`
  table (as an envelope over `agent_tasks`) + `request_id` correlation + opt-in off-turn behavior.
- ⏳ **Next:** execute Phase 0 from `PRD.md` (scenario harness + `agent_workstreams` schema + `request_id`
  correlation), then proceed phase-by-phase (each gated on the prior phase's capstone).

## Conventions for agents working here

- **Documentation & specs** → this folder.
- **Browser-validation artifacts** (screenshots, snapshots, API captures) → `.validation/session-view-refactor/`
  (gitignored). **Do not** copy screenshots into this committed folder — reference them by local path.
- Keep `DESIGN.md` as the single source of truth for current-state facts; cite `file:line` / `table.column` /
  `Johnny-xxx` issue ids.
