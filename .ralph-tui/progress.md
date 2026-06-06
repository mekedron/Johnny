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
