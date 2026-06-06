# Design

> Forward-looking design system seeded from the PRODUCT.md brief. The current `frontend/src/app.css` ships the stock shadcn-svelte neutral baseline, which is what this document is meant to replace. Re-run `/impeccable document` once these tokens are implemented to capture the real, in-code state and supersede the seed values below.

## Visual Mood

Operator-deck restraint with one committed identity strike. The chassis is dark, dense, hairline-bordered, and quiet — Linear-tight rows on Vercel-pristine neutrals. Across that chassis runs a single saturated signal: a bright Cyberpunk-yellow that appears only where the operator needs to *look*. Most of the surface is doing the disciplined job of not competing with the data it carries. The few places that earn the yellow do so because they represent live state, primary action, focus, or the now of an in-flight session.

No glitch, no scanlines, no chromatic aberration, no all-caps SYS labels. The Cyberpunk reference is for the *color commitment*, not the costume. The job of the visual system is to make a confident operator feel both calmer and faster, never to perform "tool-ness" at them.

## Color

### Strategy

**Committed.** One saturated brand color (Signal Yellow) carries 5–10% of any given surface — it is the signal, not the texture. The remaining 90–95% is disciplined dark neutrals with near-zero chroma. The discipline of withholding the yellow is what gives it meaning when it appears. Dark is the primary identity; light is engineered to match, not the front cover.

### Brand anchor

| Token | OKLCH | Notes |
| --- | --- | --- |
| Signal Yellow | `oklch(0.927 0.176 103)` | Equivalent to `rgb(249, 233, 78)`. Cyberpunk anchor. The only saturated hue in the default palette. |

### Dark tokens (primary identity)

```css
:root,
.dark {
  /* Surfaces — neutral, no warm/cool tint. Yellow does the heating. */
  --background:        oklch(0.13  0 0);   /* body */
  --surface-1:         oklch(0.16  0 0);   /* card, panel */
  --surface-2:         oklch(0.20  0 0);   /* popover, hovered row, sidebar */
  --surface-3:         oklch(0.23  0 0);   /* input field, raised affordance */

  /* Borders — hairlines via low-alpha white, Vercel-style */
  --border:            oklch(1 0 0 / 0.08);
  --border-strong:     oklch(1 0 0 / 0.14);
  --separator:         oklch(1 0 0 / 0.06);

  /* Ink — body text against --background must hit AA (≥4.5:1) */
  --ink:               oklch(0.96 0 0);   /* primary text */
  --ink-muted:         oklch(0.70 0 0);   /* secondary text, labels, meta */
  --ink-subtle:        oklch(0.55 0 0);   /* tertiary; non-body only (fails AA at body sizes) */
  --ink-on-yellow:     oklch(0.13 0 0);   /* near-black for text on Signal Yellow surfaces */

  /* Brand — the discipline lives here */
  --primary:           oklch(0.927 0.176 103);   /* Signal Yellow */
  --primary-hover:     oklch(0.950 0.180 103);
  --primary-pressed:   oklch(0.880 0.170 103);
  --primary-foreground: var(--ink-on-yellow);

  /* Status — chosen NOT to compete with the yellow */
  --success:           oklch(0.78 0.13 165);   /* muted teal */
  --warning:           oklch(0.78 0.16  55);   /* warm amber; offset from primary hue 103 */
  --destructive:       oklch(0.66 0.22  25);   /* desaturated red */
  --info:              oklch(0.78 0.13 220);   /* cool cyan, used sparingly */

  /* Focus ring — always the brand yellow */
  --ring:              var(--primary);
}
```

### Light tokens (functional, not primary)

```css
.light {
  --background:        oklch(0.99 0 0);
  --surface-1:         oklch(0.97 0 0);
  --surface-2:         oklch(0.95 0 0);
  --surface-3:         oklch(0.93 0 0);

  --border:            oklch(0 0 0 / 0.10);
  --border-strong:     oklch(0 0 0 / 0.16);
  --separator:         oklch(0 0 0 / 0.07);

  --ink:               oklch(0.16 0 0);
  --ink-muted:         oklch(0.45 0 0);
  --ink-subtle:        oklch(0.60 0 0);
  --ink-on-yellow:     oklch(0.13 0 0);

  /* Yellow on white is contrast-bad as a button label color, but works as a
     button BACKGROUND because the ink-on-yellow value supplies the contrast.
     Yellow as decoration (focus ring, active indicator) is fine in light too. */
  --primary:           oklch(0.927 0.176 103);
  --primary-hover:     oklch(0.900 0.180 103);
  --primary-pressed:   oklch(0.860 0.170 103);
  --primary-foreground: var(--ink-on-yellow);

  --success:           oklch(0.62 0.13 165);
  --warning:           oklch(0.66 0.18  55);
  --destructive:       oklch(0.55 0.22  25);
  --info:              oklch(0.62 0.14 220);

  --ring:              var(--primary);
}
```

### Yellow discipline (the most important rule)

Signal Yellow appears on, and ONLY on:

- The **primary CTA** of any given surface — at most one per screen state. (`Save changes`, `Start session`, `Approve reply`.) Secondary actions are ghost or outline; ghost on default surface.
- The **focused state** of any interactive element — yellow outline ring at 2px with 2px offset.
- The **live-now indicator** — the active-session dot, the recording badge, the "Johnny is speaking" pulse. Animated only with a `prefers-reduced-motion` fallback.
- The **selected / active state** in navigation — sidebar nav `aria-current="page"` gets a yellow left-edge accent (1.5px) plus elevated text color, nothing more.
- The **brand mark / logo** when it appears against a dark surface.
- **Diagnostic highlight** in the playground: when an output is being measured/streamed, the cursor or in-flight token gets a yellow underline.

Yellow does **not** appear on:

- Body text. Ever. (Use `--ink`.)
- Hyperlinks in long-form text. (Use `--ink` with `text-decoration-color: var(--border-strong)`; hover swaps the decoration to `var(--primary)` only.)
- Card borders, panel borders, separators. (Decorative use of the brand color reads as "themed", which is the opposite of disciplined.)
- Decorative icons. (Icon color matches `currentColor` and inherits ink.)
- Status indicators that aren't the brand-defined "live" state. Success is teal, warning is amber-not-yellow, error is red.

The test: if you covered the yellow with a gray of the same value, would the surface still be readable and the hierarchy still legible? If yes, the yellow is doing real work. If the screen falls apart, the yellow was carrying a job that the structure should have carried — fix the structure, then reintroduce the yellow as accent.

## Typography

### Families

```css
--font-sans: 'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
--font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Mono', Menlo, monospace;
```

One sans family (Inter) for both body and headings — weight + tracking + scale carry the hierarchy. One mono family (JetBrains Mono) for codes, IDs, IP/host fields, transcript timestamps, and provider model names. No third family. (Geist Sans / Geist Mono are acceptable swaps if the project later adds them; the brief reads identically.)

Self-host both via the Vite font pipeline (subset to latin-ext for now); do not use Google Fonts CDN in production.

### Scale (modular ~1.25)

```css
--font-size-xs:   0.75rem;    /* 12px — chip labels, sparingly used eyebrow */
--font-size-sm:   0.8125rem;  /* 13px — meta, secondary descriptions */
--font-size-base: 0.875rem;   /* 14px — BODY DEFAULT (console density, not 16px) */
--font-size-md:   1rem;       /* 16px — section subtitles, list-leading rows */
--font-size-lg:   1.125rem;   /* 18px — primary screen titles */
--font-size-xl:   1.375rem;   /* 22px — page titles */
--font-size-2xl:  1.75rem;    /* 28px — section heroes (rare on dashboards) */
--font-size-3xl:  2.25rem;    /* 36px — landing-only, not used in app shell */
```

Body at 14px is intentional and matches Linear/Vercel-dashboard density. Drop to 13px only inside dense tables where horizontal information matters more than reading comfort.

### Weights

```css
--font-weight-regular:  400;   /* body */
--font-weight-medium:   500;   /* emphasized body, button labels, table headers */
--font-weight-semibold: 600;   /* headings, nav active state */
--font-weight-bold:     700;   /* loudest hierarchy moments only — page title in a hero, big stat */
```

Do not introduce 300 (light) — at small sizes it disintegrates against dark surfaces. Do not introduce 800/900 — overshoots the calm-console register.

### Tracking and leading

```css
--tracking-display: -0.025em;  /* xl+ headings */
--tracking-body:     0;
--tracking-mono:     0;
--tracking-eyebrow:  0.06em;   /* the rare all-caps short label (≤4 words) */

--leading-display: 1.1;
--leading-heading: 1.25;
--leading-body:    1.55;
--leading-mono:    1.45;
```

`text-wrap: balance` on h1–h3 (titles, modal headings). `text-wrap: pretty` on long-form prose (transcript blocks, settings descriptions).

Cap body line-length at 70ch via a `max-width` on the prose container, not via the body font itself.

## Spacing

8-point base with a 4 step. Tailwind defaults map cleanly; the token names below are the contracted set we actually use.

```css
--space-0:  0;
--space-px: 1px;
--space-0_5: 0.125rem;  /* 2  — hairline gaps inside compact chips */
--space-1:   0.25rem;   /* 4  — icon-to-label gap, chip padding */
--space-2:   0.5rem;    /* 8  — tight stack inside an input row */
--space-3:   0.75rem;   /* 12 — default button padding-y */
--space-4:   1rem;      /* 16 — card padding, row-to-row gap */
--space-5:   1.25rem;   /* 20 */
--space-6:   1.5rem;    /* 24 — section gap inside a card */
--space-8:   2rem;      /* 32 — section gap between cards */
--space-10:  2.5rem;    /* 40 */
--space-12:  3rem;      /* 48 — page padding-y, screen header gap */
--space-16:  4rem;      /* 64 — rare, between large content blocks */
```

Vary spacing for rhythm; never use the same step three rows in a row without intent. The home dashboard should breathe slightly more than the settings tables.

## Radius

Smaller than stock shadcn (was 10px); console-precise.

```css
--radius-xs:   2px;    /* badges, chips */
--radius-sm:   4px;    /* inputs, small buttons */
--radius-md:   6px;    /* buttons, cards (the workhorse) */
--radius-lg:   8px;    /* large cards, popovers */
--radius-xl:  10px;    /* modals, sheet edges */
--radius-pill: 9999px; /* status pills, active-session indicator */
```

`--radius-md` (6px) is the default; the eye reads it as "deliberate, not cushioned".

## Motion

Linear/Vercel-precise: fast, exponential ease-outs, no bounce, no elastic. Motion is functional, not decorative.

```css
--ease-out-quart: cubic-bezier(0.25, 1, 0.5, 1);
--ease-out-expo:  cubic-bezier(0.16, 1, 0.3, 1);
--ease-in-out:    cubic-bezier(0.45, 0, 0.55, 1);

--dur-instant:    80ms;   /* press-down feedback */
--dur-fast:      150ms;   /* hover, focus, color shift */
--dur-base:      220ms;   /* most state transitions, popover enter */
--dur-slow:      320ms;   /* sheet/drawer enter, large-surface change */
--dur-deliberate: 500ms;  /* page-level transitions; rare */
```

Use `--dur-base var(--ease-out-quart)` as the default for `transition`. Reach for `--ease-out-expo` when a surface enters from off-canvas.

### Reduced motion

Every animated property gates on `@media (prefers-reduced-motion: reduce)` and either crossfades or jumps. The active-session pulse degrades to a static, fully-opaque yellow dot. Page transitions become instant.

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

### Signature motions

- **Active-session pulse**: opacity 0.6 → 1.0 → 0.6 over 1800ms, `ease-in-out`, infinite. Fallback: static at 1.0.
- **Focus ring fade-in**: 80ms `ease-out`. Fade-out 0ms (snap; focus loss must be instant).
- **Toast enter**: translateY(8px) + opacity 0 → 1, 220ms `ease-out-quart`. Exit: opacity 1 → 0, 150ms.
- **Sidebar open** (mobile drawer): translateX(-100%) → 0, 280ms `ease-out-expo`.

No transitions on layout properties (`width`, `height`, `top`, `left`) unless absolutely necessary; prefer `transform`/`opacity`.

## Elevation

Depth comes primarily from surface-lightness steps, not from shadow. Reserve shadows for content that floats above the document plane.

```css
--shadow-flat:    none;
--shadow-popover: 0 8px 24px rgb(0 0 0 / 0.40);
--shadow-modal:   0 16px 40px rgb(0 0 0 / 0.55);

--glow-primary:   0 0 12px oklch(0.927 0.176 103 / 0.45);   /* only on signature moments */
```

`--glow-primary` is the *single* place the brief allows a subtle "neon" lean, applied to the active-session indicator and nowhere else. It is also the first thing to remove if a future critique flags overdesign — it is decoration, and decoration must justify itself.

## Focus

The focus ring is the most-seen application of Signal Yellow; it must be perfect.

```css
:where(button, [role='button'], a, input, select, textarea, [tabindex]):focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
  border-radius: inherit;
}
```

Never remove the ring; if a component's shape would clip the outline, give it `outline-offset` room rather than swapping for a `box-shadow` ring. Use `:focus-visible` exclusively — mouse-click focus on a button should NOT show the ring; keyboard focus must.

## Z-Index

Semantic, named, never arbitrary.

```css
--z-base:           0;
--z-raised:         10;
--z-dropdown:    1000;
--z-sticky:      1100;
--z-modal-backdrop: 1200;
--z-modal:       1300;
--z-toast:       1400;
--z-tooltip:     1500;
```

Never write `z-index: 999` or `z-index: 9999`. If a value isn't on the scale, the scale grows; arbitrary values rot.

## Components

The current `frontend/src/lib/components/ui/` set covers `alert`, `badge`, `button`, `card`, `input`, `label`, `separator`. Below are the variants the design system commits to. Add components from the shadcn-svelte registry as needed (`bd`/CLI install per `components.json`), but pass each through this brief before accepting defaults.

### Button

Variants:

| Variant | Surface | Border | Foreground | Use |
| --- | --- | --- | --- | --- |
| `primary` | `--primary` | none | `--primary-foreground` | The single primary action per surface. |
| `secondary` | `--surface-2` | `--border` | `--ink` | Common actions; outline-feel. |
| `outline` | transparent | `--border-strong` | `--ink` | Reversible actions, dialogs' secondary. |
| `ghost` | transparent | none | `--ink-muted` | Toolbar buttons, inline. Hover bg = `--surface-2`. |
| `destructive` | `--destructive` | none | `oklch(0.98 0 0)` | Confirm-deletes only. |

Sizes:

| Size | Height | Padding-x | Font size |
| --- | --- | --- | --- |
| `sm` | 28px | 10px | `--font-size-sm` |
| `default` | 32px | 14px | `--font-size-base` |
| `lg` | 40px | 18px | `--font-size-md` |
| `icon` | 32×32 | 0 | inherits |

States:
- `:hover` swaps the bg by one step (e.g., primary → `--primary-hover`).
- `:active` swaps to `*-pressed`; transition duration `--dur-instant`.
- `:disabled` reduces opacity to 0.5 and removes pointer events; do not change hue.
- `[data-loading]` shows a leading 14px spinner using `--primary` on dark bg, `--ink-on-yellow` on yellow bg.

Anti-patterns: no gradient buttons, no oversized shadows, no rounded-pill default. The pill button is reserved for status chips and the live-session indicator only.

### Card

```css
.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  /* No default shadow. */
}
```

Cards are the lazy answer; use them only when the content is truly a discrete unit (a session, a provider, a saved template). Nested cards are forbidden. Three card rows in a column without something different between them is a smell — break the rhythm with a section header, a separator, or different padding.

Card header padding `--space-4`; content padding `--space-4` `--space-6`; action area aligns to the right of the header.

### Alert

Variants: `info`, `success`, `warning`, `destructive`.

- Surface: a 10% tinted overlay of the variant color (e.g., `oklch(0.78 0.13 220 / 0.10)` for info on dark).
- Border: 1px solid the same variant color at 30% alpha.
- Icon: lucide, color = variant color full strength.
- Title: `--font-weight-semibold`, `--ink`.
- Description: `--ink-muted`.

No side-stripe borders ever; the border is full on all four sides or absent.

### Badge

Variants:
- `default`: `--surface-2` bg, `--ink-muted` text.
- `signal`: `--primary` bg, `--ink-on-yellow` text. Reserved for "live", "active now". One per visible region max.
- `success` / `warning` / `destructive`: same tint-overlay rule as alerts but as a chip.

Size: `--font-size-xs`, padding `--space-1` `--space-2`, radius `--radius-pill` for status, `--radius-xs` for taxonomy.

### Input

```css
.input {
  background: var(--surface-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: var(--font-size-base);
  padding: var(--space-2) var(--space-3);
  transition: border-color var(--dur-fast) var(--ease-out-quart);
}
.input::placeholder { color: var(--ink-subtle); }
.input:focus-visible { border-color: var(--ring); /* plus the outline rule */ }
.input[aria-invalid='true'] { border-color: var(--destructive); }
```

Placeholder must still hit 4.5:1 against `--surface-3`; `--ink-subtle` at L 0.55 vs surface L 0.23 is ~4.8:1 on dark. Verify after implementation.

### Label

```css
.label {
  color: var(--ink);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  letter-spacing: var(--tracking-body);
}
```

Required-field marker is `*` in `--destructive`, with `aria-required="true"` on the input.

### Separator

`background: var(--separator)`. Height/width 1px. No double borders.

## Layout

### App shell

- Persistent left sidebar, 240px wide. Background `--surface-2`. Hairline right border `--border`.
- No top bar by default; the page title lives in the content area, not the chrome.
- Mobile (`< 768px`): sidebar collapses to a drawer triggered by a top-left icon button. Drawer opens 280px from the left, backdrop `oklch(0 0 0 / 0.5)`.

### Content max-widths

- Dashboard / list views: `max-width: 1200px`.
- Forms / settings panels: `max-width: 720px`.
- Long-form transcript display: `max-width: 760px` (≈70ch at base size).

### Page padding

- Desktop: `var(--space-12)` top, `var(--space-8)` sides.
- Mobile: `var(--space-6)` top, `var(--space-4)` sides.

### Grid / flex defaults

- 1D row/column: flex. 2D (cards across a viewport): grid.
- Responsive card grid without breakpoints: `grid-template-columns: repeat(auto-fit, minmax(280px, 1fr))` and `gap: var(--space-4)`.

## Iconography

- Library: `@lucide/svelte` (already installed).
- Stroke width: 1.5px (Lucide default 2px is too heavy for the calm-console register; override at the icon-wrapper level).
- Sizes: 16px (inline with body), 18px (in buttons), 20px (in headings or alert callouts).
- Color: `currentColor`. Inherits ink, never tinted to yellow except when the icon is *part of* a signature element (the active-session dot icon, the primary CTA leading icon).
- No filled icons; outline only. Cyberpunk-yellow on outline icons reads as deliberate; filled icons in brand color read as decorative.

## Imagery

Johnny is a tool; the only "imagery" is a favicon, a logo mark, and possibly screenshots for the README/landing. Reserve illustration entirely; if a state needs a visual (empty inbox, no sessions today), use a single Lucide icon at 32px in `--ink-subtle` and a one-sentence label. No cartoons. No stock photos.

## Implementation notes

- The current `frontend/src/app.css` defines the shadcn-svelte token surface. The cleanest migration is to replace the value of each existing token with the values above (matching shadcn's `--primary`, `--background`, `--border` etc.), then add the new tokens that don't exist yet (`--surface-1/2/3`, `--ink*`, `--glow-primary`, motion vars, z-index vars).
- shadcn-svelte components reference the existing tokens by name, so a value-only replacement migrates the component set without component edits.
- Tailwind 4's `@theme inline` block in `app.css` needs an additive section for the new tokens; the existing entries can keep their names.
- The new typography requires self-hosting Inter and JetBrains Mono. Add a `static/fonts/` directory and a `@font-face` block in `app.css`; do not pull from Google Fonts CDN.
- Test against the existing dark/light toggle (`mode-watcher`) — both modes must ship correct values from the first commit, even though dark is the brand identity.

## Verification gates (must pass before this DESIGN.md is considered "implemented")

1. Body text (`--ink` on `--background`) hits ≥4.5:1 in both modes. (Dark: ≈18:1, easy.)
2. Placeholder text (`--ink-subtle` on `--surface-3`) hits ≥4.5:1.
3. Primary button label (`--primary-foreground` on `--primary`) hits ≥4.5:1. (Black on yellow: ≈14:1.)
4. The yellow appears on ≤ 3 elements per visible viewport at any moment on the app shell.
5. No card sits inside another card.
6. No section uses an uppercase tracked eyebrow.
7. Reduced-motion is honored; the active-session pulse degrades to a static state.
8. The screenshot in the README (taken in dark mode) is unambiguously not stock shadcn.
