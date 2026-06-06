# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### Design tokens (DESIGN.md → `frontend/src/app.css`)

The whole frontend reads tokens from CSS custom properties on `:root`
(light) and `.dark`. **Shadcn-svelte component names are preserved**:
`--background`, `--foreground`, `--primary`, `--card`, `--border`,
`--ring`, `--sidebar`, etc. **Values** point at the DESIGN.md OKLCH
scheme (Signal Yellow `oklch(0.927 0.176 103)` on near-black
`oklch(0.13 0 0)`). Per-surface code should USE the named tokens — never
hardcode Tailwind color classes (`bg-slate-900`, `text-white`,
`bg-indigo-500`, `bg-purple-400`). When you see a hardcoded color in a
+page.svelte or +layout.svelte, that's a per-page reimagining task
(`Johnny-fe.1`–`.8`), not a tokens task.

Additional tokens DESIGN.md adds on top of the shadcn baseline:
`--surface-1/2/3`, `--ink`, `--ink-muted`, `--ink-subtle`,
`--ink-on-yellow`, `--border-strong`, `--separator`, `--primary-hover`,
`--primary-pressed`, `--success`, `--warning`, `--info`, `--glow-primary`,
motion vars (`--dur-fast/--dur-base/--ease-out-quart`...), z-index vars.
The dark/light tokens both ship from the first commit (mode-watcher
toggles `.dark` on `<html>`).

### Signal Yellow discipline

`--primary` is yellow ONLY for: the ONE primary CTA per surface, the
focus ring, the live-now indicator, the sidebar `aria-current="page"`
accent, the brand mark on dark surfaces, the in-flight playground token.
Test: cover the yellow with grey of the same value — if the page still
reads, yellow is doing real work. If not, the structure is broken; fix
structure first.

### Live-pulse signature animation

Use `class="live-pulse"` for the active-session dot / recording badge.
The keyframe is defined in `app.css` and automatically degrades to a
static, fully-opaque state via the `prefers-reduced-motion` media query.

### Browser validation rule

Every UI change MUST be driven through `chrome-devtools` MCP (NOT
`claude-in-chrome`) before close. The MCP attaches to a long-lived
Chrome on `http://127.0.0.1:9222` (started via `./scripts/start-chrome.sh`,
which is idempotent). If `evaluate_script` returns "No page found", that
is a stale-session bug — `wait_for` + `take_snapshot` re-establish it
without restarting Chrome.

### Lint baseline has a pre-existing error

`pnpm lint` currently fails on `providers/+page.svelte:235` — an unused
`configuredRowsFor` const. Not introduced by current work; do not chase
it as a regression. Fix it in a `providers` page ticket
(`Johnny-fe.2` / `Johnny-stt.7` follow-up).

---

## 2026-06-06 — Johnny-fe.9 (foundation pass)

This iteration shipped the **design-token foundation** that every
per-page ticket (`Johnny-fe.1`–`.8`) depends on, plus the
chrome-devtools audit of the current state.

### Files changed

- `frontend/src/app.css` — replaced the stock shadcn neutral baseline
  with the DESIGN.md OKLCH token set in both `:root` (light) and `.dark`
  (primary identity). Added all new tokens DESIGN.md introduces beyond
  shadcn (surface-1/2/3, ink scale, border-strong/separator, status
  colors picked NOT to compete with yellow, glow-primary, motion +
  z-index vars). Added focus ring (2px Signal Yellow,
  `:focus-visible` only), prefers-reduced-motion fallback, and the
  signature `.live-pulse` keyframe.
- `frontend/src/app.html` — added Inter + JetBrains Mono via Google
  Fonts (`preconnect` + `display=swap`). Self-hosting via Vite font
  pipeline is the production-readiness path; not part of the foundation
  drop.

### Audit findings (chrome-devtools)

Verification gates from DESIGN.md status:

| Gate | Token | Dark | Light | Pass? |
| ---- | ----- | ---- | ----- | ----- |
| 1 | `--ink` on `--background` | 17.91 | 18.86 | ✅ |
| 2 | `--ink-subtle` on `--surface-3` | 4.64 | 7.49 | ✅ (lifted from L=0.55→0.62 dark, L=0.60→0.40 light to clear AA) |
| 3 | `--primary-foreground` on `--primary` | 16.09 | 16.09 | ✅ |
| 7 | Reduced motion honored | — | — | ✅ (CSS gate + `.live-pulse` degrade) |
| 8 | Screenshot unambiguously NOT stock shadcn | — | — | ✅ (dark + yellow scheme) |
| 4 | Yellow on ≤ 3 elements per viewport | — | — | ⚠ BLOCKED on `.1`–`.8` — providers page currently uses yellow on > 3 chips |
| 5 | No card-in-card | — | — | ⚠ BLOCKED on `.1`–`.8` — providers page has white cards inside its content card |
| 6 | No uppercase tracked eyebrow | — | — | ⚠ BLOCKED on `.1`–`.8` — providers shows `ADD A NEW STT (SPEECH-TO-TEXT) PROVIDER`, `AUTHENTICATION`, `MODEL`, `ADVANCED` |

Screenshots captured:

- `.ralph-tui/iterations/fe9_home_default.png` — light, header still uses hardcoded `bg-slate-900`
- `.ralph-tui/iterations/fe9_home_dark.png` — dark, main content adopts the new tokens, shell does not
- `.ralph-tui/iterations/fe9_providers_dark.png` — providers page in dark mode, all `.2` audit failures visible

### Verdict

Final-audit gates 1, 2, 3, 7, 8 pass at the foundation level. Gates
4, 5, 6 cannot pass until the per-page reimagining tickets
(`Johnny-fe.1` through `Johnny-fe.8`) ship — they own the hardcoded
header/sidebar colors, the duplicated cards, the uppercase eyebrows,
and the undisciplined yellow on the providers chips. `Johnny-fe.9`
remains OPEN; its work is the post-`.1`–`.8` cleanup + final tour.

### Learnings

- **Token replacement migrates components for free.** The DESIGN.md
  note "shadcn-svelte components reference the existing tokens by name,
  so a value-only replacement migrates the component set without
  component edits" is correct in practice — `Button`, `Card`, `Alert`,
  `Badge`, `Input`, `Label`, `Separator` all changed appearance via the
  app.css edit alone.
- **Hardcoded Tailwind colors in +layout.svelte are the per-page work**,
  not foundation. The light-mode-only header (`bg-slate-900`) and
  light sidebar in `.dark` mode prove this — the foundation tokens flow
  through wherever the markup uses semantic classes (`bg-background`,
  `text-foreground`, `bg-primary`), and break wherever a hex/Tailwind
  literal is used.
- **`--ink-subtle` at L=0.55 (dark) and L=0.60 (light) fails AA on
  `--surface-3`** per DESIGN.md's own conjecture in §Input ("Placeholder
  must still hit 4.5:1; verify after implementation"). Lifted to 0.62
  (dark) and 0.40 (light) to clear 4.5:1 with margin.
- **Google Fonts `display=swap` is enough for the foundation drop.**
  Self-hosting via Vite is a production hardening pass, not blocking
  for the design system. Note for future tickets: per CLAUDE.md font
  rule "do not use Google Fonts CDN in production" — convert before
  the README screenshot ships.
- **chrome-devtools MCP `evaluate_script` was returning "No page
  found"** intermittently. `wait_for` + `take_snapshot` re-establish
  the session without restarting Chrome.

---

## 2026-06-06 — Johnny-fe.8 (REIMAGINE /sessions/[id])

Replaced the 1126-line live-session detail page with a 985-line
shadcn-svelte + design-token rewrite. From-scratch IA redesign, not a
1:1 port.

### Files changed

- `frontend/src/routes/sessions/[id]/+page.svelte` — full rewrite.
  Deleted the entire `<style>` block (was ~400 lines of hardcoded hex
  colors, bespoke `.status-pill-*` rules, custom `.outcome-*` chips,
  duplicate timer formatting). Replaced with shadcn `Button` /
  `Card` / `Alert` and Tailwind utility classes mapped to the
  DESIGN.md tokens (`bg-card`, `text-foreground`, `text-muted-foreground`,
  `border-border`, `bg-surface-2`, `text-warning`, `bg-primary`, etc).
  Preserved every existing `data-testid` (`session-page`,
  `session-status`, `transcript-pane`, `transcript-scroll`,
  `bot-transcript-line`, `transcript-line`, `transcript-partial`,
  `transcript-count`, `decisions-pane`, `decisions-count`,
  `decision-row`, `approvals-pane`, `approval-row`,
  `approval-countdown`, `approve-button`, `reject-button`,
  `approval-error`, `end-session-button`, `reopen-playground-button`,
  `connect-warn`, `stop-error`, `session-error-reason`) so any future
  e2e tests stay green.

### IA changes (the actual REIMAGINE)

1. **Hoisted Pending Approval as the surface's primary action.** The
   old three-column equal-weight layout buried the only thing the
   operator *acts on in real time* (approvals) in the third column.
   New design: when `pendingApprovals.length > 0`, a callout card with
   a 2px Signal Yellow left-edge accent appears ABOVE the
   transcript/decisions grid. The first Approve button is the surface's
   only yellow primary; Reject is outline. Keyboard shortcuts `A` and
   `R` route to the topmost pending approval (skipped when focus is
   inside an input/textarea/contentEditable).
2. **Two-column instead of three.** Transcript (2fr) + Decisions (1fr).
   Pending approvals are no longer a third column — they're either a
   hoisted callout (when actionable) or absent. This frees ~25% of
   horizontal space for the transcript, which is the densest content.
3. **Session metadata row.** Added a meta strip below the title:
   `source · duration · connection state`. The duration ticks every
   second from `started_at` for live sessions; freezes at `ended_at`
   for terminal sessions. Connection state only renders when
   disconnected and the session is non-terminal — when everything is
   working we don't waste a viewport slot on "ok".
4. **Yellow discipline.** The old page used yellow on `.status-pill-scheduled`
   (`#fef3c7` bg), `.outcome-pending` (`#fef3c7`), `.transcript-line.partial`
   (`#fffbeb`), and the approval card border (`#fdba74`). The new design
   reserves Signal Yellow for: the live-pulse dot, the approval callout
   accent, the topmost Approve CTA, and focus rings. Pending decisions
   in the Decisions feed now use the amber `--warning` token at hue 55
   (distinct from primary's hue 103), not yellow. Verified ≤3 yellow
   elements per viewport in every captured screenshot.
5. **Destructive primary.** End session is `Button variant="destructive"`
   per DESIGN.md component spec — red, not yellow. The CTA *is* the
   primary action of an active session, but yellow is for *positive*
   primary CTAs only (Save changes, Start session, Approve reply).
6. **Bot turns lose their indigo costume.** The old `.transcript-line.bot`
   was `#eef2ff` bg with `#4338ca` "Johnny" text. New: a subtle
   `bg-muted` step with a `BotIcon` + mono "Johnny" label. No purple,
   no yellow — just a deliberate surface step.
7. **Partial transcript marker.** The dashed-border treatment survives
   but the bg/text now use tokens (`bg-surface-2`, dashed
   `border-border`, italic `text-muted-foreground`). The "…partial"
   chip uses `text-warning` so it reads as a transient state.
8. **Outcome chips.** Custom inline `<span>` chips (not shadcn Badge,
   because Badge's `default`/`secondary`/`destructive`/`outline`
   variants don't map to the five outcome states). Each chip is
   `border + 10% bg tint + foreground` of its semantic token:
   `success` (spoken), `muted` (suppressed), `warning` (pending),
   `destructive` (rejected), `info` (suggested). All readable AA
   against `--surface-2`.

### Verification (chrome-devtools MCP)

| Gate | Result |
| ---- | ------ |
| 1. Body text contrast (`--ink` on `--background`) | ✅ (inherits from app.css foundation) |
| 2. Placeholder contrast | ✅ (no placeholders on this page) |
| 3. Primary button label contrast | ✅ (Approve uses `--primary-foreground` on `--primary`) |
| 4. Yellow on ≤ 3 elements per viewport | ✅ (live dot + accent stripe + Approve = max 3) |
| 5. No card-in-card | ✅ (Card.Root wraps lists; rows are `<li>` not `<Card>`) |
| 6. No uppercase tracked eyebrow | ✅ ("LIVE" and outcome chips are status indicators, not eyebrows) |
| 7. Reduced-motion honored | ✅ (`live-pulse` degrades via app.css gate) |
| 8. Screenshot unambiguously NOT stock shadcn | ✅ (dark mode + signal yellow accents) |

Screenshots:

- `.ralph-tui/iterations/fe8_before_populated_dark.png` — old design, full populated state
- `.ralph-tui/iterations/fe8_after_with_approval_dark_v2.png` — new design, dark mode, with pending approval callout
- `.ralph-tui/iterations/fe8_after_no_approval_dark.png` — new design, dark mode, no pending approval (transcript + decisions only)
- `.ralph-tui/iterations/fe8_after_light.png` — new design, light mode
- `.ralph-tui/iterations/fe8_after_failed_light.png` — new design, terminal/failed state (`Failure stage` Alert, no End session button, no LIVE indicator)

Quality gates: `pnpm check` 0 errors / 0 warnings. `pnpm lint` fails on
the pre-existing `providers/+page.svelte:235` `configuredRowsFor`
baseline (documented in patterns at the top of this file).

### Learnings

- **Frontend container does NOT bind-mount source.** `docker-compose.yml`
  for `frontend` has no `volumes:` — the Dockerfile bakes
  `COPY . .` so host file edits aren't visible inside the container.
  After any `+page.svelte` / `app.css` edit, `docker compose build
  frontend && docker compose up -d frontend` is required to see the
  change. Vite HMR within the container still works for subsequent
  edits but the IMAGE is the source of truth on first launch. This
  cost ~5 minutes and was not obvious; flagging here so the next
  ticket doesn't get stuck staring at a "why isn't my reload showing"
  page.
- **shadcn-svelte `Badge` doesn't cover 5-state outcome chips.** Its
  variant API (`default`/`secondary`/`destructive`/`outline`) maps to
  a 4-color palette that doesn't include the `success`/`warning`/`info`
  semantic tokens DESIGN.md introduces. For multi-state semantic
  chips, an inline `<span>` with `border + 10% bg tint + foreground`
  of the semantic token works better than fighting `tv()` overrides.
- **Hoisting the primary action surface above the fold is worth more
  than any visual treatment.** The old page had the approval pane in
  column 3 of a 3-column grid; finding "is there something I need to
  approve?" required scanning right. The new callout above the grid
  collapses that to zero scan. Same shadcn primitives, materially
  better task time.
- **`Card.Content` does NOT scroll cleanly with `bind:ref` + a
  height cap.** The first draft tried `<Card.Content bind:ref={el}
  class="overflow-y-auto max-h-[65vh]">` — Svelte 5 `bind:ref` on a
  component prop works (the component exposes `$bindable()`) but the
  Card.Content's grid-row inside Card.Root fights the height cap. The
  simpler fix: use a bare `<div bind:this={el} class="overflow-y-auto
  px-4 py-3">` as a sibling of Card.Header inside the Card.Root, and
  cap the Card.Root itself (`max-h-[70vh]` + `gap-0 py-0`). The Card
  primitives remain, but for scroll panes we drive the scroll
  container directly. Worth knowing for any other "scroll inside card"
  surface (history page is next).

---

