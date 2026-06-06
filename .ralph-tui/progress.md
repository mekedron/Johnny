# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Page padding lives in the layout, not in pages
`<main>` in `frontend/src/routes/+layout.svelte` owns the desktop/mobile page
padding via a wrapper `<div class="px-4 py-8 md:px-8 md:py-12">`. Individual
page wrappers should ONLY set `max-w-*`, `mx-auto`, `flex-col`, `gap-*` — do
NOT add `p-6`, `py-12`, or any padding utility, or you'll double-pad and the
DESIGN.md ~48px top breathing room becomes inconsistent. The matched DESIGN.md
values: 32px sides + 48px top/bot on desktop (≥768px), 16px sides + 32px
top/bot on mobile.

### Tabs without a shadcn Tabs primitive
shadcn-svelte Tabs is not installed in this repo. To build tab strips, use
a plain `<div role="tablist">` with `<button role="tab" aria-selected={...}
aria-controls=...>` triggers; style the active state with
`border-b-2 border-foreground text-foreground`, hover with
`text-muted-foreground hover:text-foreground`. Pair each panel with
`<div role="tabpanel" id=... aria-labelledby=...>`. This pattern lives
in `frontend/src/routes/history/[id]/+page.svelte` and meets ARIA expectations
without a wrapper component.

### Status tone helpers
For surface tinting tied to a status enum, prefer a small helper that
returns `border-{tone}/40 bg-{tone}/10 text-foreground`. Reuse `success`,
`warning`, `info`, `destructive`, `border` tones — never reach for yellow
on status pills. Pattern used identically in `sessions/[id]/+page.svelte`
and `history/+page.svelte`.

### Search-with-icon input
For sleek embedded search affordances, wrap a bare `<input type="search">`
in a `flex` row with a leading `<SearchIcon class="size-4 text-muted-foreground">`
and a trailing close button (Lucide `XIcon`) that appears only when the
search is active. Use `has-focus-visible:border-ring` on the wrapper so
the whole row picks up focus state. Avoid wrapping in `<Input>` because
the shadcn Input component owns the border.

---

## 2026-06-07 - Johnny-fe.7

- Reimagined `/history` list and `/history/[id]` detail per DESIGN.md (cyberpunk-yellow signal on near-black operator deck) and PRODUCT.md (composed, deliberate, signal).
- Replaced 442+847 lines of bespoke CSS with shadcn-svelte primitives (`Button`, `Alert`) + Tailwind utilities tied to design tokens.
- **History list** information-architecture changes:
  - Search lifted to the top with a leading SearchIcon and inline clear (X) button. Single primary Search CTA in Signal Yellow.
  - Bespoke session table replaced with a token-styled `<table>`: status as toned badges (teal = ended, red = failed, blue = scheduled/joining), monospace numeric columns, hairline `--separator` row borders.
  - Empty state now uses a single Lucide `ArchiveIcon` at 32px in `--ink-subtle` + one-sentence operator copy, per DESIGN.md §Imagery guidance.
  - Pager moved from chrome-heavy text buttons to compact `outline` Buttons with chevron icons.
- **History detail** information-architecture changes:
  - Replaced the 3-pane shell with a TAB strip (Transcript / Decisions / Utterances). Counts appear inline next to tab labels. Each tab is now a single full-width panel that breathes, instead of three cramped columns.
  - Header reduced to a flex strip: back link → Export JSON (outline) → Delete (confirm-in-place). No more side-stripe alert clutter for error rows; everything uses the `Alert.Root` primitive with a Lucide icon.
  - Meta moved out of a card grid and into a single horizontal meta strip below the title (Started · Ended · Duration · Container) — meta is signal, not chrome.
  - Search panel kept for in-session queries with the same icon-leading row pattern as the list page.
- Backend fix piggybacked: `HistorySessionRead.meeting_config_id` was typed as required `int` but the DB column allows NULL, producing a 500 on every detail fetch. Changed to `int | None` (and the matching frontend `HistorySessionRecord` type), which unblocked detail-page validation.
- Files changed:
  - `frontend/src/routes/history/+page.svelte` — full rewrite
  - `frontend/src/routes/history/[id]/+page.svelte` — full rewrite
  - `frontend/src/lib/history.ts` — `meeting_config_id: number | null`
  - `backend/app/api/history.py` — `meeting_config_id: int | None`
  - `.validation/Johnny-fe.7/*.png` — 17 before/after browser captures via chrome-devtools MCP
- Quality gates: `pnpm check` → 0 errors / 0 warnings; `pnpm lint` → 0 errors. Both list and detail pages verified in light + dark mode against real session data via chrome-devtools MCP.
- **Learnings:**
  - Frontend container does NOT bind-mount source (per `johnny-frontend-no-bindmount` memory). After every edit, `docker compose build frontend && docker compose up -d frontend` is required before Vite serves the change. Same applies to backend api.
  - Use `<button role="tab">` + `role="tabpanel"` for a tab UI when no shadcn primitive exists — cleaner than reaching for a third-party package.
  - The Signal Yellow ≤3-per-viewport rule held cleanly: Search CTA + History sidebar accent + active session dot = 3. No yellow on status badges (teal `--success` and red `--destructive` carry meaning instead).
  - `flex-wrap` on the detail-page action row handled narrow viewports without breaking the delete-confirm inline flow.

---

## 2026-06-07 - Johnny-fe.11

- Audited every page in dark mode via chrome-devtools MCP and addressed the user's complaint that "they look ugly" / "not enough top padding".
- **Universal top-padding fix**: `<main>` in `+layout.svelte` now wraps `{children}` in a `<div class="px-4 py-8 md:px-8 md:py-12">`. Before this change, six of nine pages had ZERO top padding — their `<h1>` was touching the viewport top edge. With one edit, every page now sits below ~48px of breathing room on desktop, matching DESIGN.md §Layout. Removed redundant page-level padding from `+page.svelte` (`p-6`) and `providers/+page.svelte` (`px-8 py-12`) so they don't double-pad.
- **Home page reimagined** as an operator console: replaced the lone "Backend health" card with a 3-up grid of quick-nav tiles (Calendar / Playground / Templates / Providers / History / Settings) plus a compact backend status strip with a colored health dot. Title scale reduced from `text-4xl` to `text-2xl` matching the rest of the app.
- **Settings "Default" badge** moved off the brand yellow surface. Per DESIGN.md §Yellow discipline, the Signal Yellow is reserved for live-now signal, primary CTA, focus ring, and active-nav state — NOT for a static "this is the default account" marker. Switched to the teal-success token: `border-success/40 bg-success/10` with a `text-success` check icon.
- **Providers** h1 bumped from `text-xl` to `text-2xl` for consistency with every other page header.
- Files changed:
  - `frontend/src/routes/+layout.svelte` — `<main>` wraps children in padded `<div>`
  - `frontend/src/routes/+page.svelte` — rewritten as nav-tiles + backend strip
  - `frontend/src/routes/providers/+page.svelte` — strip own padding, bump h1 size
  - `frontend/src/routes/settings/+page.svelte` — Default badge → success tone
  - `.validation/Johnny-fe.11/00-08*.png` — before screenshots (one per page)
  - `.validation/Johnny-fe.11/30-38*.png` — after screenshots (one per page)
- Quality gates: `pnpm check` → 0 errors / 0 warnings; `pnpm lint` → clean.
- **Learnings:**
  - The top-padding bug had a single root cause — the layout's `<main>` had no `pt-*`, and only providers/home set their own. Fixing it at the layout level cascades to every page; never re-introduce per-page top padding or you'll double-pad and regress.
  - Don't put brand yellow on long-lived attribute markers (the old "Default" badge). DESIGN.md is explicit: yellow is a *signal*, not a *texture*. Test by covering it with neutral gray — if the screen still works, the yellow was decorative.
  - `mcp__chrome-devtools__resize_page` can fail silently if the underlying Chrome window has been shrunk by the user. Restarting Chrome via `./scripts/start-chrome.sh` (after `pkill -f user-data-dir=…/.chrome-profile`) was the unblocker — the script is idempotent so it's safe to re-run.
  - The frontend container does NOT bind-mount source (per `johnny-frontend-no-bindmount` memory). After every CSS/Svelte edit, `docker compose build frontend && docker compose up -d frontend` is required.

---
