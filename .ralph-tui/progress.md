# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Frontend layout owns `<main>`**: `frontend/src/routes/+layout.svelte` wraps `{@render children()}` in `<main class="content">`. Route pages MUST NOT render their own `<main>` element (invalid nested landmarks). Use `<section>`, `<div>`, or just bare elements inside route `+page.svelte` files.
- **Active-route detection**: Use `import { page } from '$app/state'` (Svelte 5 runes API in SvelteKit 2.16+). Compare `page.url.pathname` with `href`. Apply both a class (`class:active`) and `aria-current="page"` for a11y.
- **Browser verification fallback**: `.mcp.json` declares `chrome-devtools-mcp` for future sessions, but MCPs load at session start so they can't be used in the same iteration they're added. For one-shot verification, `npx playwright` works: `npm install playwright` in `/tmp`, then `npx playwright install chromium`, then a small `.mjs` script that captures screenshots to `.ralph-tui/reports/`.
- **Frontend quality gates**: `pnpm typecheck` runs `svelte-kit sync && svelte-check`; `pnpm lint` runs ESLint. Both must be invoked from `frontend/`.
- **Backend quality gates**: `uv run pytest`, `uv run ruff check`, `uv run mypy` from `backend/`.

---

## 2026-06-05 - Johnny-kgc.4
- Implemented SvelteKit application shell: sidebar nav (Calendar/Templates/Providers/History/Settings), header with brand + account placeholder, active-route highlighting via `$app/state`, mobile-responsive collapse (≤720px) with hamburger toggle and backdrop click-to-close.
- Created placeholder routes: `calendar/`, `templates/`, `providers/`, `history/`, `settings/` — each renders an `<h1>` matching its label and a one-line description.
- Updated home `+page.svelte` to drop its own `<main>` wrapper (layout now owns `<main>`); kept backend-health check there.
- Added `.mcp.json` registering `chrome-devtools-mcp` for the project (per user global rule). Used `npx playwright` as a one-shot fallback for this session since MCPs load at session start.
- Verified all 6 routes return 200 via curl, correct `<title>` and `<h1>` per route, `aria-current="page"` set on the active nav item, and "Not connected" account placeholder rendered on every page. Captured desktop (1280x800) + mobile (390x800, collapsed and open) screenshots to `.ralph-tui/reports/us-004-screenshots/`.
- Files changed: `frontend/src/routes/+layout.svelte`, `frontend/src/routes/+page.svelte`, `frontend/src/routes/{calendar,templates,providers,history,settings}/+page.svelte`, `.mcp.json`, `.ralph-tui/progress.md`.
- **Learnings:**
  - SvelteKit 2.16+ exposes `$app/state` (runes-based) alongside legacy `$app/stores`. Prefer `$app/state` in this codebase since `svelte.config.js` forces runes mode.
  - chrome-devtools-mcp isn't installed globally for this user — `.mcp.json` makes it project-scoped. The agent can write the file but can't load MCPs mid-session.
  - The default vite dev server only binds to `localhost`; pass `--host 127.0.0.1 --port 5173` for predictable curl/Playwright targeting.
  - Replace existing `<main>` elements in route pages whenever introducing a layout that wraps children in `<main>` — silent invalid-HTML otherwise.
---
