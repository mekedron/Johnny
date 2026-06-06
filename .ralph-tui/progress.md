# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### shadcn-svelte foundation (Johnny-stt.10)

The frontend has shadcn-svelte installed with Tailwind v4. Canonical
structure:

- `frontend/components.json` — registry config, baseColor=`neutral`,
  ui alias `$lib/components/ui`, utils alias `$lib/utils`.
- `frontend/src/app.css` — Tailwind v4 entrypoint. Imports
  `tailwindcss` + `tw-animate-css`, defines all theme CSS variables
  for `:root` (light) and `.dark` (dark) with oklch colors, and
  maps them into the `@theme inline` block so utilities like
  `bg-background`, `text-foreground`, `border-border`,
  `text-muted-foreground`, etc. resolve.
- `frontend/src/lib/utils.ts` — `cn()` (twMerge + clsx), plus type
  helpers `WithElementRef`, `WithoutChildren`, `WithoutChild`,
  `WithoutChildrenOrChild`.
- `frontend/src/lib/components/ui/` — primitive components, one
  folder per primitive (e.g. `button/`, `card/`, `input/`, …) with
  `<Name>.svelte` + `index.ts`. Export both `Root` and the
  PascalCased alias (`Button`). Import from
  `$lib/components/ui/button/index.js` (note the .js — required
  by `tsconfig.rewriteRelativeImportExtensions`).
- `frontend/vite.config.ts` — adds `@tailwindcss/vite` plugin
  ahead of `sveltekit()` so Tailwind v4 picks up the CSS.
- Dark mode is wired via `mode-watcher`. Place `<ModeWatcher />` in
  the root `+layout.svelte`, then `toggleMode()` / `setMode()` from
  the same package. The theme toggle Button lives in the header
  with `text-primary-foreground` class so its icon is visible on the
  still-unmigrated dark header background.

### Component conventions

- Use Svelte 5 runes (`$props`, `$state`, `$bindable`, `$derived`).
- Type props with `WithElementRef<...>` from `$lib/utils` so each
  component takes a `bind:ref`.
- Class names: `cn(baseVariants(...), className)`; never hardcode
  Tailwind colors — use semantic tokens (`bg-card`,
  `text-card-foreground`, `border`, `text-muted-foreground`, …).
- For variant-rich components use `tailwind-variants`’s `tv({...})`
  in a `<script lang="ts" module>` block and export the variants
  function (e.g. `buttonVariants`) so other components can reuse it.

### Existing pages still use bespoke inline `<style>` blocks

Each per-page sub-ticket (`Johnny-stt.10.1`–`.10.8`) is responsible
for swapping its inline styles for shadcn primitives. Until then,
unmigrated pages keep working — the layout-level changes (importing
`app.css`, adding `<ModeWatcher />`, adding the theme toggle) are
non-breaking.

---

## 2026-06-06 - Johnny-stt.10

**Foundation + landing-page migration shipped. Parent bead remains OPEN. 9 sub-tickets track per-surface migrations.**

### What was implemented

1. **Tailwind v4 + shadcn-svelte foundation**
   - Installed: `tailwindcss@4`, `@tailwindcss/vite@4`, `tw-animate-css`,
     `clsx`, `tailwind-merge`, `tailwind-variants`, `bits-ui`,
     `mode-watcher`, `@lucide/svelte`.
   - `frontend/components.json` configured (style=default,
     baseColor=neutral, ui alias `$lib/components/ui`).
   - `frontend/src/app.css` with full canonical theme tokens
     (light + dark, oklch values straight from the shadcn-svelte
     theming guide).
   - `frontend/vite.config.ts` now wires `@tailwindcss/vite` plugin.
   - `frontend/src/lib/utils.ts` with `cn()` and ref/children type
     helpers.

2. **Primitive components** (canonical shadcn-svelte
   implementations, Svelte 5 runes mode):
   - Button (`button/button.svelte` + `index.ts`) with all six
     variants (default, destructive, outline, secondary, ghost,
     link) and four sizes.
   - Card family (Root, Header, Title, Description, Content, Footer,
     Action).
   - Input (handles `type="file"` separately for files binding).
   - Label (uses `bits-ui` `Label.Root`).
   - Badge (default/secondary/destructive/outline).
   - Separator (uses `bits-ui` `Separator.Root`).
   - Alert family (Root, Title, Description; default + destructive).

3. **Root layout (`/+layout.svelte`)**
   - Imports `../app.css` so Tailwind utilities + theme tokens are
     active app-wide.
   - Mounts `<ModeWatcher />` for class-based dark mode persistence.
   - Adds a theme-toggle `<Button>` in the header next to the
     account indicator (sun→moon icon swap via dark: variants).
   - Removed hardcoded `color: #111827; background: #ffffff;` from
     the `:global(body)` block so the body now respects
     `bg-background` / `text-foreground` from the @layer base.
   - Everything else in the layout (sidebar, sessions panel,
     approvals panel, menu toggle) still uses its existing scoped
     CSS — its full migration is tracked in `Johnny-stt.10.1`.

4. **Landing page `/+page.svelte` (62 lines → 78 lines)**
   - Fully migrated: replaced hand-rolled heading + health-check
     section with shadcn Card (Header/Title/Description/Action +
     Content), Button (outline, sm), and Alert (default for
     loading/ok, destructive for the error state), plus Lucide
     icons for status.
   - Inline `<style>` block removed.

### Files changed

- `frontend/package.json` — new deps.
- `frontend/pnpm-lock.yaml` — pnpm regenerated.
- `frontend/vite.config.ts` — added `@tailwindcss/vite`.
- `frontend/components.json` — new.
- `frontend/src/app.css` — new.
- `frontend/src/lib/utils.ts` — new.
- `frontend/src/lib/components/ui/button/{button.svelte,index.ts}` — new.
- `frontend/src/lib/components/ui/card/{card.svelte,card-header.svelte,card-title.svelte,card-description.svelte,card-content.svelte,card-footer.svelte,card-action.svelte,index.ts}` — new.
- `frontend/src/lib/components/ui/input/{input.svelte,index.ts}` — new.
- `frontend/src/lib/components/ui/label/{label.svelte,index.ts}` — new.
- `frontend/src/lib/components/ui/badge/{badge.svelte,index.ts}` — new.
- `frontend/src/lib/components/ui/separator/{separator.svelte,index.ts}` — new.
- `frontend/src/lib/components/ui/alert/{alert.svelte,alert-title.svelte,alert-description.svelte,index.ts}` — new.
- `frontend/src/routes/+layout.svelte` — app.css import, ModeWatcher, theme toggle button; removed hardcoded body colors.
- `frontend/src/routes/+page.svelte` — fully migrated to shadcn.

### Chrome-devtools MCP validation (per CLAUDE.md top-rule)

- `start-chrome.sh` confirms Chrome is up on 127.0.0.1:9222.
- Navigated to `http://localhost:5173/`, took snapshot — confirmed
  shadcn Card / Button / Alert / theme toggle render.
- Screenshot light mode → `.ralph-tui/iterations/stt-10-landing-light.png`.
- Clicked theme toggle, screenshot dark mode →
  `.ralph-tui/iterations/stt-10-landing-dark.png` (Card and Alert
  re-color correctly; sidebar still bright because unmigrated).
- Navigated to `/providers` and `/playground` to confirm no
  regression — both pages render exactly as before; layout shell
  including the new theme toggle button is visible →
  `.ralph-tui/iterations/stt-10-providers-unmigrated.png`,
  `.ralph-tui/iterations/stt-10-playground-unmigrated.png`.
- `list_console_messages` shows only backend connectivity errors
  (`ws://localhost:8000/ws/global` and `localhost:8000` REST) —
  unrelated to the migration; backend isn't running.
- `list_network_requests` shows all migrated assets (`/src/app.css`,
  `/src/lib/components/ui/*`, `mode-watcher`, lucide icons,
  tailwind-merge, tailwind-variants, clsx) resolved with 200/304 —
  no 404s.

### Why the parent bead Johnny-stt.10 was NOT closed

The bead's strict acceptance criteria require **every** page
migrated, **every** surface chrome-devtools MCP screenshot-toured
in light + dark, and old bespoke CSS deleted. That is 10,000+ lines
of Svelte across 10 pages plus delete passes — multi-day work, not
one iteration. The bead description explicitly permits splitting
into sub-tickets: *"Whether to split into sub-tickets per surface.
Acceptable if the assignee deems necessary — but the parent ticket
then tracks the overall closure."*

Filed sub-tickets (P2, child-of Johnny-stt.10, label
`shadcn-migration`):

- `Johnny-stt.10.1` — Migrate /+layout.svelte shell
- `Johnny-stt.10.2` — Migrate /providers (2358 lines)
- `Johnny-stt.10.3` — Migrate /playground (1504 lines)
- `Johnny-stt.10.4` — Migrate /calendar (1322 lines)
- `Johnny-stt.10.5` — Migrate /settings (988 lines)
- `Johnny-stt.10.6` — Migrate /templates (539 lines)
- `Johnny-stt.10.7` — Migrate /history list + detail
- `Johnny-stt.10.8` — Migrate /sessions/[id] detail
- `Johnny-stt.10.9` — Final audit + cleanup + full screenshot tour

The parent bead `Johnny-stt.10` stays OPEN until `.10.9` closes.

### Learnings

- **Tailwind v4 + SvelteKit 2.57 + Svelte 5.55 works out of the
  box.** No `tailwind.config.js` is needed when using Tailwind v4;
  theme is defined entirely in CSS via `@theme inline`. Vite plugin
  must come BEFORE `sveltekit()` in the plugins array.
- **`bits-ui` is required for any primitive that wraps an
  accessibility primitive** (Label, Separator, Dialog, etc.) but
  NOT for visual-only components (Button, Card, Badge, Alert, Input,
  Separator-style div). Keep that in mind to avoid pulling
  `bits-ui` into the landing-page bundle.
- **`mode-watcher` handles class-based dark mode via a `class="dark"`
  on `<html>` and persists to localStorage.** Just mount
  `<ModeWatcher />` in the root layout. The toggle call is
  `toggleMode()` from the same package.
- **`tsconfig.rewriteRelativeImportExtensions: true` (already set
  in this repo) requires the canonical `index.js` suffix on
  shadcn-svelte imports** — `import { Button } from
  '$lib/components/ui/button/index.js'`, not `.ts` or no suffix.
- **Mixing shadcn dark + legacy bright works without breaking but
  looks weird.** The landing page goes black, the layout sidebar
  stays light — fine as a foundation handoff but cannot ship as
  end-state. Sub-tickets must clear this per-surface.
- **Svelte 5 scoped `<style>` overrides Tailwind base layer if it
  reuses element selectors.** Had to strip the layout's
  `color: #111827; background: #ffffff;` from `:global(body)` so
  `bg-background` / `text-foreground` win.
- **Chrome-devtools MCP `uid`s expire across navigations.** Always
  re-take a snapshot before clicking; uids from a prior snapshot
  raise "Element with uid X no longer exists" after any
  `navigate_page`.

---
