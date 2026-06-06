# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Frontend (as of 2026-06-06, pre-shadcn-svelte migration)

- **Stack:** SvelteKit + Svelte 5 (runes mode forced via `svelte.config.js`), Vite 8, pnpm, adapter-auto.
- **Styling:** Pure scoped CSS in each `.svelte` file. **No** Tailwind, **no** PostCSS, **no** `components.json`, **no** UI component library. ~11K LOC of UI markup + CSS across 8 routes.
- **Routes:** `/`, `/calendar`, `/playground`, `/templates`, `/providers`, `/history`, `/history/[id]`, `/sessions/[id]`, `/settings` — all under `frontend/src/routes/` with `+layout.svelte` as the shell.
- **Layout shell (`+layout.svelte`, 849 LOC):** Provides sidebar nav, active-session status panel, pending-approval panel, OAuth `postMessage` bridge, browser `Notifications` API integration, SSE subscriptions (`subscribeToGlobal` + per-session `subscribeToSession`), 30s session polling, and per-decision approval-expiry timers in `Map`s. Any layout refactor must preserve every one of these lifecycles.
- **Real-browser validation is mandatory** per `CLAUDE.md`: `chrome-devtools` MCP only (never `claude-in-chrome`), driven by `./scripts/start-chrome.sh`.
- **116 `data-testid` attributes** scattered across the 8 pages are the regression contract for the chrome-devtools MCP tour. Preserve every one through any UI refactor.
- **Custom badge / status / template-mode / decision-outcome variants:** 16+ semantic color variants beyond shadcn-svelte's default `default/secondary/destructive/outline`. Maintain in a central `$lib/components/badges.ts` map (recommended) when shadcn-svelte primitives are introduced — keeps `badge.svelte` untouched for clean upstream upgrades.

---

## 2026-06-06 — Johnny-stt.9

### What was implemented

**No production code was changed.** Bead Johnny-stt.9 explicitly mandates plan mode and requires user approval before any code is written ("Get the plan approved before exiting plan mode"). Priority is P2 — user said *"actually right now it's not super important"*. The deliverable for this iteration is the migration plan itself.

A parallel-agent workflow inventoried all 8 pages of the frontend, mapped every bespoke UI element to its shadcn-svelte primitive replacement, fetched the canonical install/config/theming docs verbatim, and synthesized a complete migration plan.

### Files changed

- **Created:** `docs/shadcn-svelte-migration-plan.md` (1,103 lines) — the authoritative migration plan.
  - §1 Summary: 11-phase big-bang strategy on a long-lived `feat/shadcn-svelte-migration` branch, ~10–14 dev-days at P2 cadence.
  - §2 Per-page inventory: complexity rating + primitive mappings for all 10 page files (layout + 8 pages + 1 dynamic detail).
  - §3 Phases 0–10: goals, ordered steps, deliverables, effort estimates.
  - §4 Cutover strategy (`big_bang`, with full tradeoff justification — no current Tailwind means no two-system coexistence risk; in-flight tickets stt.3/.5/.7 already CLOSED; only stt.8's modal pattern needs a Phase 0 freeze decision).
  - §5 Risks & mitigations (10 entries: Tailwind-v4-plugin-order, SSE/audio/mic lifecycle disruption, stt.7 multi-instance regression replay, stt.8 modal pattern conflict, CSS specificity collisions, bundle-size delta, dark-mode story, data-testid loss, ScrollArea viewport ref pattern, Sheet modal default).
  - §6 Validation strategy (concrete chrome-devtools MCP tour per phase per CLAUDE.md, with Phase 10 PR-body artefact list: baseline + post screenshots, bundle delta, testid diff = 0, regression transcript).
  - §7 Open questions (13 items requiring user answers before Phase 0 begins).
  - §8 + Appendix A: verbatim install quickstart (Tailwind v4 + shadcn-svelte init + slate tokens + `mode-watcher` for dark mode) extracted from the canonical docs.
- **Updated:** `.ralph-tui/progress.md` (this file).

### Status of bead Johnny-stt.9

**LEFT OPEN** (status remains `in_progress`). The bead's own description explicitly mandates `"Get the plan approved before exiting plan mode."` In ralph-tui's autonomous-execution context, no user is available to approve. Closing the bead would falsely signal that the migration is complete; leaving it open correctly reflects that the next required action is human review of `docs/shadcn-svelte-migration-plan.md` followed by answers to §7's open questions.

### Next iteration

If `docs/shadcn-svelte-migration-plan.md` already exists when the next ralph-tui iteration picks up Johnny-stt.9:

1. **Do NOT redo the inventory or rebuild the plan** — it is the iteration deliverable and was reviewed by the user (or is awaiting review).
2. Check `git log -- docs/shadcn-svelte-migration-plan.md` for any user edits / answers to the 13 open questions in §7.
3. If §7 answers are merged: proceed to **Phase 0** of §3 (coordination/freeze check + chrome-devtools baseline screenshots). Phase 0 is fully scoped — produces `frontend/.migration/baseline-screenshots/` and `frontend/.migration/testid-inventory.txt`, plus the branch `feat/shadcn-svelte-migration`. No production code is touched yet.
4. If §7 answers are NOT merged: re-check whether the user has changed priority (it was P2 = not urgent at file creation) or has commented on the bead. Otherwise leave the bead in `in_progress` and signal complete.

### Learnings

- **The frontend has zero Tailwind / zero existing component library.** Migration is from pure scoped-CSS Svelte 5 → Tailwind v4 + shadcn-svelte + bits-ui. No "two design systems coexist" concern, but no `components.json` / `app.css` / `utils.ts` either — Phase 1 builds it all from scratch via `pnpm dlx sv add tailwindcss` + `pnpm dlx shadcn-svelte@latest init`.
- **Svelte 5 runes mode is already enforced** project-wide (`svelte.config.js` sets `compilerOptions.runes = true`). shadcn-svelte v1's Svelte-5 components fit cleanly. No flip-day risk.
- **The layout shell (`+layout.svelte`) is the highest-risk migration surface** despite its `medium` complexity rating — it owns every SSE subscription, every approval-timer `Map`, the OAuth `postMessage` bridge, and the browser Notifications API. Strategy: extract all lifecycle code to a new `$lib/layoutLifecycle.svelte.ts` module *before* rewriting the template. Verified in plan §3 / Phase 3.
- **Providers page (2,358 LOC, `very_high`) and Playground (1,504 LOC, `high`) are intentionally migrated last.** They are 25% of the project's frontend LOC, and the smaller pages first build confidence in the primitive mappings + variant maps + form schemas.
- **Cutover decision: big-bang on a feature branch, not staged-to-main.** Justification: (a) no Tailwind today → no half-migrated specificity collisions during a staged rollout; (b) the in-flight UI tickets that *would* have justified staging (stt.3/.5/.7) are already CLOSED; (c) the layout shell is foundational and can't ship in isolation. Tradeoff accepted: bigger single PR, but reverting one phase mid-branch is cheaper than reverting half a system in production.
- **Custom variants are pervasive.** 5 session-status pill colors + 1 source-browser badge + 6 template-mode chips + 5 decision-outcome variants + a "success" Button green = 17 semantic variants beyond shadcn-svelte defaults. Centralize in `$lib/components/badges.ts` (tailwind-variants map) rather than forking `badge.svelte` — keeps shadcn upstream upgrades clean.
- **Toast surface is currently absent.** No app-wide toast library today; introducing Sonner globally in Phase 2 is a UX change worth flagging in §7 (open question: replace inline Alerts everywhere, or only for new transient feedback).
- **Dark mode does not exist today.** `mode-watcher` will be wired in Phase 2 but the toggle UI is deliberately deferred (recommendation in §7) — the `.dark` class stays dormant. Hardcoded color literals throughout existing `<style>` blocks will be replaced with token-aware Tailwind utilities so a future toggle works out of the box.

### Gotchas

- `tailwindcss()` Vite plugin **must precede** `sveltekit()` in `plugins[]` — reversed order breaks `@apply` inside Svelte `<style>` blocks. Verified in plan §5 risks.
- shadcn-svelte `Sheet` defaults to modal (focus trap + inert background). Calendar's bespoke slide-over is `aria-modal="false"` — Phase 7 uses `<Sheet modal={false}>` explicitly to preserve the keep-list-visible-while-editing UX.
- `ScrollArea` imperative scroll-to-bottom must bind to `ScrollArea.Viewport`'s ref, **not** the outer `ScrollArea` root. Required for session detail's transcript auto-scroll (Phase 6), playground's mic-level container (Phase 8), and providers' pip install log streaming (Phase 9).
- Sidebar with `collapsible="offcanvas"` uses CSS-only collapse → child instances are NOT destroyed → approval-timer `Map` entries survive sidebar toggle. Phase 3 includes a chrome-devtools test that asserts a countdown keeps ticking through `SidebarTrigger` clicks.

---
