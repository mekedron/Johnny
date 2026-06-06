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

### Lint baseline is now clean

The pre-existing baseline error in `providers/+page.svelte:235` (unused
`configuredRowsFor`) was cleared by the Johnny-fe.2 REIMAGINE — the
function was removed during the rewrite. `pnpm lint` now exits 0.

### Multi-action surfaces: state-driven `primaryAction` picks the single yellow

When a surface has THREE possible primary actions (Test / Save / Set
as default) and any one could legitimately be "the next thing to do"
depending on state, encode the decision in a derived `primaryAction`
state and have each button query it:

```ts
const primaryAction = $derived.by<'save' | 'activate' | 'test' | null>(() => {
  if (!selectedRow) return 'save';
  if (hasPendingChanges) return 'save';
  if (!selectedRow.is_active) return 'activate';
  return 'test';
});
```

Then `<Button variant={primaryAction === 'test' ? 'default' : 'outline'}>`
on each candidate. Result: exactly one yellow CTA per surface state,
regardless of which buttons are visible. Pattern lives in
`providers/+page.svelte`.

### Picker/list sheets with many row-level actions: ALL outline, no yellow

A picker sheet (e.g., Piper voices browser) renders 10+ row-level
actions on screen at once. Marking each action `variant="default"`
(yellow) instantly violates gate 4 by ~10x. Convention: every
row-level action button (`Install`, `Use`, `Play`, `Remove`) is
`outline` or `ghost`. The sheet has zero main-area yellow; the
operator's focus ring is the only yellow that appears. Pattern in
`providers/+page.svelte` voices browser.

### Pending-change-aware primary discipline

When a surface has TWO actions that could both legitimately be primary
(e.g., `Save changes` and `Join now` on a configured-meeting sheet),
adopt a `hasPendingChanges` derived flag and swap variants so that
*exactly one* button is yellow at any moment:

- New config (no `existingConfig`): submit = yellow `Enable Johnny`,
  Join now hidden.
- Saved + no pending changes: Join now = yellow, submit = outline
  `Saved` (disabled).
- Saved + pending changes: submit = yellow `Save changes`, Join now =
  outline (with tooltip "Save your changes first").

Pattern lives in `calendar/+page.svelte` and resolves the gate-4 risk
of "2+ yellows competing for the operator's attention" without removing
either action.

### Slider yellow budget

`<input type="range">` styled with Tailwind `accent-primary` becomes a
yellow control — the filled-track + thumb both pick up `var(--primary)`.
Acceptable on a one-yellow surface (templates form sheet), but on a
live-session surface that ALSO has the `live-pulse` dot + sidebar nav
active, a focus ring on a nearby input pushes the total visible-yellow
count to 4 — gate 4 fails. Fix: replace `accent-primary` with
`[accent-color:var(--color-foreground)]` (arbitrary Tailwind 4 syntax)
so the slider's thumb/track adopt the neutral ink color (white-ish dark,
black-ish light) instead of yellow. Pattern lives in
`playground/+page.svelte`. Visual cost: zero — the operator reads a
slider as a slider regardless of its accent color.

### FastAPI route ordering for prefix collisions

FastAPI matches routes in declaration order. When a literal path
collides with a parametric one — e.g. `POST /providers/preview/test`
vs. `POST /providers/{provider_id}/test` — the **first one declared
wins**. So if `/preview/test` is added after `/{provider_id}/test`,
requests to `/providers/preview/test` resolve to the parametric route
with `provider_id="preview"` and 422 on integer parsing. Always
declare literal `/<verb>/...` paths BEFORE any `/{provider_id}/...`
parametric routes in the same router. Pattern lives in
`backend/app/api/providers.py` — the `/preview/*` and `/catalog/*`
blocks are positioned right after `/{provider_id}/deactivate` and
before `/{provider_id}/test`.

### `<input type="number">` needs `step="any"` for non-integer values

Default HTML5 `type="number"` step is 1, so any float value (default
`temperature: 0.7`, `top_p`, etc.) triggers the browser's native
validation popup "The two nearest valid values are 0 and 1" which
blocks form submission. Set `step="any"` to allow arbitrary floats.
Pattern: in dynamic-field renderers driving `FieldType.NUMBER`, pass
`step={field.type === 'number' ? 'any' : undefined}` to the `<Input>`.
Pattern lives in `providers/+page.svelte`.

### Row-clickable list rows: don't nest action buttons inside the row button

Don't put an action button (e.g. `Deactivate`) inside the row's
clickable `<button>`. The HTML is invalid (button-in-button) and the
outer click never fires reliably on the inner button's bounds. Fix:
the `<li>` becomes a flex container with the row content in one
`<button onclick={openModal}>` and the side-action in a sibling
`<div>` next to it — no nesting. Pattern lives in
`providers/+page.svelte`.


---

## 2026-06-07 — Johnny-fe.2 (REIMAGINE /providers)

Replaced the 2358-line custom-CSS providers page (largest in the app —
STT/LLM/TTS tabs, master-detail layout, two modals, the Piper voices
browser, the Parakeet runtime-install panel) with a 1597-line
shadcn-svelte + design-token rewrite. From-scratch IA redesign — not a
1:1 port.

### Files changed

- `frontend/src/routes/providers/+page.svelte` — full rewrite.
  Deleted the entire `<style>` block (~654 lines of hardcoded hex
  colors: `#4f46e5` indigo primary, `#10b981` green active, `#fef3c7`/
  `#92400e` amber streaming, `#e0e7ff`/`#312e81` indigo configured,
  `#a7f3d0`/`#065f46` green type-local, `#a5f3fc`/`#155e75` cyan
  type-cloud, `#f59e0b`/`#fffbeb` amber Parakeet package panel,
  `#4f46e5→#c026d3` indigo→purple mic level gradient, `#fef2f2`/
  `#991b1b`/`#fecaca` red error, `#6ee7b7`/`#ecfdf5`/`#064e3b` green
  test-ok). Replaced with shadcn `Button` / `Alert` / `Input` and
  Tailwind utility classes mapped to DESIGN.md tokens. Preserved every
  `data-testid` (providers-page, providers-error, tab-{stt,llm,tts},
  panel-{kind}, configured-list-{kind}, available-list-{kind},
  instance-{kind}-{id}, add-{kind}-{provider}, test-panel-{kind},
  parakeet-package-{id}, parakeet-installed-badge, parakeet-install-{id},
  parakeet-install-log-{id}, parakeet-install-error-{id},
  stt-test-{id-or-name}, stt-test-error-{id}, stt-test-result-{id},
  stt-transcript-{id}, generic-test-{kind}-{id-or-name},
  generic-test-result-{id}, sample-error-{id}, play-{id}, voices-{id},
  form-{kind}-{id-or-new}, field-{name}, display-name-{kind}-{id-or-new},
  save-{kind}-{id-or-new}, activate-{kind}-{id}, deactivate-{kind}-{id},
  delete-{kind}-{id}, export-button, export-modal, export-with-secrets,
  export-error, export-download, voices-modal, voice-filter, voice-list,
  voice-{key}, voices-error, preview-{key}, use-{key}, remove-{key},
  install-{key}, install-error, preview-error, remove-error).

### IA changes (the actual REIMAGINE)

1. **Killed the uppercase tracked eyebrows.** Old design used `CONFIGURED
   (N)`, `ADD A NEW X PROVIDER`, `AUTHENTICATION`, `MODEL`, `ADVANCED`,
   `TYPE` pills, `LOCAL`/`CLOUD` pills, and the `INSTALLED` chip — all
   uppercase tracked. New design uses sentence case throughout:
   "Configured", "Available adapters", "Authentication", "Model",
   "Advanced", "Local", "Cloud", "Streaming", "Installed". Gate 6
   passes.
2. **Killed the rainbow palette.** Old design painted "type" pills in
   green-on-cream for local and cyan-on-cream for cloud, "streaming" in
   amber, "configured-count" in indigo, "active" in green, the
   Parakeet-package panel border in amber-left-edge with cream
   background, the mic-level meter in indigo→purple gradient. New
   design uses neutral chips on `surface-2/3` with Lucide icons
   (`HardDriveIcon`/`CloudIcon`/`ZapIcon`/`LibraryIcon`) — color
   carries no extra information, the icon + label does. Active is a
   green-dot pill; mic-level meter is `bg-foreground` neutral; the
   Parakeet package panel is hairline-bordered `surface-1` with
   `border-warning/40` only when not installed.
3. **Master-detail rail cleaned up.** Old left rail had two sections
   visually identical except for an uppercase eyebrow heading. New
   design separates them with a sentence-case heading + count chip
   ("Configured · 1" + count), a hairline separator, then "Available
   adapters · 5". Configured cards are solid-bordered; available cards
   are dashed-bordered with a leading `PlusIcon` — at-a-glance the
   operator knows which is configured vs. addable. Active configured
   card gets a green-dot "Active" pill (no yellow on the card,
   preserving the Signal Yellow discipline).
4. **Detail panel header consolidated.** Old design had a 2-column
   header with title + lede on the left and a `<dl>` of meta on the
   right. New design uses a 2-row header: title row (with `Active`/
   `Configured`/`New · unsaved` pill), summary in muted-foreground,
   then a meta strip below in `text-muted-foreground` showing `Adapter
   {name} · Local/Cloud · Streaming/Batch · N models · Get a key →`.
   Compact, scannable, matches the calendar page's meta-strip pattern.
5. **State-driven `primaryAction` picks the single yellow CTA.** With
   three possible primary actions per state (Test / Save / Set as
   default), naive variant assignments produce 2+ yellows
   simultaneously. New design encodes the decision in a derived
   `primaryAction: 'save' | 'activate' | 'test' | null`:
   - New draft: `save` (Test = outline)
   - Saved + pending changes: `save` (Test = outline, Set as default
     = outline disabled with "Save your changes first" tooltip)
   - Saved + no pending + not active: `activate` (Test = outline,
     Save = outline disabled "Saved")
   - Saved + no pending + active: `test` (Save = outline disabled
     "Saved", no Set as default button)
   The button variants query `primaryAction` so exactly ONE button is
   yellow at any moment. Documented in the new "Multi-action surfaces"
   Codebase Pattern.
6. **Refresh button removed.** Every mutation already re-fetches the
   list; the Refresh button was UI noise.
7. **Export modal → shadcn-style dialog.** Old design used a centered
   modal with a `<aside>`-based backdrop and a `×` icon-button close.
   New design uses a centered AlertDialog-shaped surface with leading
   `DownloadIcon` in a `surface-2` icon-circle, sentence-case heading,
   mono `config/providers.json` reference, a hairline-bordered
   checkbox-row for "Include API keys and other secrets", and Cancel
   (outline) / Download (yellow primary) footer. Esc + backdrop click
   close it.
8. **Piper voices browser → right-side Sheet.** Old design was a
   centered ~720px-wide modal with a 4-column row layout. New design
   slides in from the right at 640px-max with sticky header
   (heading + helper text + Filter input), scrollable body, and
   sticky footer (Close). Each voice row is hairline-bordered, with
   the language + quality metadata in `text-muted-foreground`. ALL
   row-level actions (`Install`, `Use`, `Play`, `Remove`) are outline
   or ghost — no yellow in the picker, since 10+ visible rows would
   blow gate 4 if any single row's primary were yellow. Documented in
   the new "Picker/list sheets" Codebase Pattern.
9. **Native `confirm()` → AlertDialog for both Delete-provider and
   Remove-voice.** Old design used `window.confirm(...)` (jarring,
   unstylable, blocking). New design uses two custom `role="alertdialog"`
   dialogs with `aria-modal`, `aria-labelledby`, `aria-describedby`,
   focus trapping (`tabindex="-1"`), Esc handling via a window
   keydown handler with a precedence chain (delete → remove-voice →
   voices-sheet → export). Both have the red TrashIcon-in-circle
   pattern + sentence-case heading + destructive footer Button.
10. **Parakeet runtime package panel cleaned up.** Old design had a
    yellow-cream `border-left: 4px solid #f59e0b` panel with an
    uppercase badge "Not installed". New design uses a hairline
    `bg-surface-1` step with `border-warning/40` only when not
    installed, a sentence-case heading "NeMo runtime package",
    `PackageIcon` (`text-warning` or `text-success`), and a
    semantic-tokened status pill (green-dot Installed, amber-bordered
    Not installed, neutral N/A). The install button is yellow when
    not installed (the only yellow on this nested panel), outline
    when reinstalling.
11. **Mic-level meter goes neutral.** Old design had a
    `linear-gradient(90deg, #4f46e5, #c026d3)` indigo→purple gradient
    on the fill. New design: solid `bg-foreground` fill on a
    `bg-surface-3` track with rounded-pill ends. Matches the
    playground/templates "neutral level meter" pattern; the operator
    reads level by length, not by color.
12. **STT test result panel uses semantic tokens, not custom hex.**
    Old design had `#ecfdf5/#064e3b/#6ee7b7` for ok, `#fef2f2/#991b1b/
    #fecaca` for fail. New design uses `bg-success/10`,
    `border-success/30`, `text-foreground` for the transcript line
    + Lucide icons (`ClockIcon`, `MicIcon`, `DollarSignIcon`) in
    mono for the metric strip. Failed test → shadcn destructive Alert.
    Visual cost: zero; brand consistency: huge.
13. **Field types render via shadcn `<Input>` everywhere except
    select/textarea/checkbox.** Display name, text, url, password,
    number all use shadcn `Input` so the design tokens flow through
    (border-input, focus-visible:border-ring, dark:bg-input/30).
    Native `<select>` and `<textarea>` get the Input-equivalent
    Tailwind class string for visual consistency. Checkbox uses
    `[accent-color:var(--color-foreground)]` for the neutral-tone
    box matching the playground's slider-yellow-budget pattern.
14. **Footer pattern: Delete left, Save/Activate right.** Old design
    had three action buttons (`Activate`/`Deactivate`, `Delete`) in
    the top of the config-form header AND a separate `Save provider`
    button at the bottom — visually disjointed. New design uses a
    single sticky footer at the bottom of the form: destructive
    `Delete` (ghost variant, `text-destructive`) on the left,
    Deactivate/Set as default + Save on the right. Standard pattern
    matching calendar's disable/save split.

### Verification (chrome-devtools MCP, dark + light)

Drove every state through the real Chrome instance pointing at
`http://localhost:5173/providers`:
- ✓ Dark mode STT tab: configured Whisper (Active green pill) on the
  left, 5 available adapters listed in dashed cards. Detail panel has
  meta strip + yellow Test (5s mic) CTA. Footer Delete (red ghost) +
  Deactivate (outline) + Saved (outline disabled).
- ✓ Light mode STT tab: same hierarchy, same 2 yellows max (sidebar
  Providers nav + Test CTA). Gate 4 holds at 2 steady, 3 on focus.
- ✓ Dark mode LLM tab: OpenAI-compatible row, full schema (Auth, Model,
  Advanced) rendered, Test yellow primary.
- ✓ Dark mode TTS tab: Local Piper Active, 3 available adapters,
  Test + Play sample + Browse voices buttons (Test yellow primary).
- ✓ Clicked + Deepgram → new draft renders: New · unsaved pill,
  Test disabled outline, Save provider yellow (the only yellow on the
  surface). Authentication section auto-rendered with API key field.
  Get a key link in the header meta strip.
- ✓ Modified Language on saved Whisper → Save changes button became
  yellow, Test became outline, Deactivate stayed outline. Exactly ONE
  yellow CTA active per state.
- ✓ Export configuration → centered AlertDialog opens with backdrop
  dim, sentence-case heading, mono `config/providers.json`,
  Include-secrets checkbox, Cancel (outline) + Download (yellow).
  Esc closes.
- ✓ Browse voices (TTS Piper) → right-side Sheet slides in from the
  right at 640px max-width. Sticky header (heading + helper + Filter
  input), scrollable voice list with ~150 voices, sticky footer
  (Close). Every row-level button is outline or ghost — ZERO yellows
  in the sheet body. Search filter works.
- ✓ Delete provider → centered AlertDialog with red TrashIcon-in-
  circle, sentence-case "Delete this provider?", body explaining
  consequence, Cancel (outline) + Delete (destructive red). Esc /
  backdrop close. Cancel returns to detail with no state change.

### Verification gates (DESIGN.md)

| Gate | Status |
| --- | --- |
| 1. Body text on background ≥4.5:1 | ✓ (foundation, ~18:1 dark, ~16:1 light) |
| 2. Placeholder on surface-3 ≥4.5:1 | ✓ (foundation) |
| 3. Primary button label on primary ≥4.5:1 | ✓ (foundation, ~14:1) |
| 4. Yellow ≤ 3 elements per viewport | ✓ (steady: 1 sidebar Providers nav + 1 primary CTA = 2; with focus ring: 3 max; voices sheet open: 0 yellow in the picker body since every row action is outline/ghost) |
| 5. No card-in-card | ✓ (detail panel is a single card; Test section, Configuration form, sections are hairline-separated. Parakeet package panel is a hairline-bordered `surface-1` step inside the form body — semantically a section, not a Card.) |
| 6. No uppercase tracked eyebrow | ✓ (sentence case throughout: "Configured", "Available adapters", "Authentication", "Model", "Advanced", "Active", "Installed", "Local", "Cloud", "Streaming", "Test", "Configuration".) |
| 7. Reduced motion honored | ✓ (foundation; no custom animations introduced) |
| 8. Screenshot unambiguously NOT stock shadcn | ✓ (mono adapter IDs + yellow primary CTA + green-dot Active pill + dark surfaces + Lucide outline icons + no indigo/purple/cyan/green-yellow-orange anywhere) |

### Quality gates

- `pnpm check` (svelte-check) → 0 errors, 0 warnings ✓
- `pnpm lint` → 0 errors ✓ (the pre-existing baseline `configuredRowsFor`
  error was cleared — that function was removed in the rewrite, since
  it was unused. Updated the Codebase Patterns note at the top.)

### Screenshots in `.validation/Johnny-fe.2/`

- `01-before-dark-stt.png`, `02-before-dark-stt-tab.png`,
  `03-before-dark-llm.png` — pre-REIMAGINE reference (uppercase
  eyebrows, indigo/green/cyan/amber rainbow, yellow active state
  competing with the Set-as-default primary CTA).
- `12-after-dark-stt.png`, `15-after-dark-saved-active.png`,
  `20-after-dark-stt-final.png` — saved+active STT in dark.
- `13-new-draft-deepgram.png`, `14-after-fix-new-deepgram.png` —
  new-draft state. `13` shows the BEFORE-fix bug (2 yellows: Test +
  Save provider). `14` shows the after-fix state (only Save provider
  yellow).
- `16-export-dialog.png` — Export AlertDialog in dark.
- `17-voices-sheet.png` (before fix — too many yellow Installs),
  `18-voices-sheet-fixed.png` (after — all row actions outline).
- `19-delete-dialog.png` — Delete AlertDialog with red Trash + foreground
  emphasis on the provider name.
- `21-pending-changes.png` — pending-changes state: Test outline,
  Save changes yellow, focus ring on Language input.
- `22-after-light-stt.png` — light mode with pending changes.
- `23-after-light-stt-clean.png` — dark mode clean saved+active.
- `24-after-dark-llm.png`, `25-after-dark-tts.png` — LLM + TTS dark.

### Learnings

- **`primaryAction` derived state is the right abstraction for
  multi-action surfaces.** With three possible primary actions per
  state (Test / Save / Set as default), threading variant assignments
  manually through each button creates 2-yellow bugs. The derived
  `primaryAction: 'save' | 'activate' | 'test' | null` makes the
  decision once per render and every button queries it. The state
  table is small (4 cases) and easy to verify by inspection.
- **Picker sheets need explicit yellow-discipline thinking.** A list
  of 10+ rows where each has a row-level primary action would render
  10+ yellows simultaneously. The rule: in any sheet/picker with
  many visible row actions, ALL row-level buttons must be `outline`
  or `ghost`. The yellow only appears via the operator's focus ring
  on the row they're interacting with.
- **`Date.now()` fallback for unique display names.** The
  `suggestDisplayName` helper falls back to `${base} (${Date.now()})`
  in the pathological case where 1000 instances of the same provider
  share the same display name. Wouldn't fire in practice but the
  cost is one Date.now() call and it preserves the "guaranteed
  unique" property of the suggestion.
- **mode-watcher persistence vs. ModeWatcher component.** The
  `mode-watcher-mode` localStorage key is read by the ModeWatcher
  Svelte component on mount; setting it in an `initScript` (before
  the page boots) only works if the script runs BEFORE the
  ModeWatcher reads localStorage. Faster path: click the
  `Toggle theme` button (focuses on it, then enter), or just trust
  the page renders correctly in whichever mode the browser is in.
  Both modes share the same Tailwind class structure so a
  validation-pass in one mode covers the structural gates.
- **`evaluate_script` "No page found" is a fixture of chrome-devtools
  MCP.** The Codebase Patterns note is accurate — wait_for +
  take_snapshot doesn't always restore it. Falling back to screenshot
  visual inspection is more reliable for theme / contrast checks
  than fighting the CDP session.
- **`docker compose exec -T frontend pnpm check` is faster than
  `docker exec` directly** — the `-T` flag disables interactive TTY
  allocation which would otherwise hang on `pnpm`'s progress
  indicators inside an automated session.

---

## 2026-06-07 — Johnny-fe.3 (REIMAGINE /playground)

Replaced the 1504-line custom-CSS playground page with a 994-line
shadcn-svelte + design-token rewrite. From-scratch IA redesign — not a
1:1 port.

### Files changed

- `frontend/src/routes/playground/+page.svelte` — full rewrite.
  Deleted the entire `<style>` block (~520 lines of hardcoded hex
  colors: `#2563eb` indigo primary, `#dc2626` red danger, `#f59e0b`/
  `#fbbf24` orange-amber interrupt, `#d1d5db`/`#fee2e2`/`#fca5a5` toggle
  states, `#2563eb` line-user blue, `#7c3aed` line-bot purple, a green-
  yellow-orange mic-meter gradient, blue/amber/green state indicators
  with three different pulse durations). Replaced with shadcn `Button`
  / `Alert` / `Input` and Tailwind utility classes mapped to DESIGN.md
  tokens (`bg-card`, `bg-surface-1/2/3`, `text-foreground`,
  `text-muted-foreground`, `border-border`, `border-border-strong`,
  `border-separator`, `text-success`, `text-warning`, `text-destructive`,
  `bg-primary`). Preserved every `data-testid` (`playground-error`,
  `playground-mode-select`, `playground-template-select`,
  `playground-persona-input`, `playground-system-prompt`,
  `playground-context-input`, `playground-stt-override`,
  `playground-llm-override`, `playground-tts-override`,
  `playground-start-button`, `playground-advanced-toggle`,
  `live-state`, `audio-live`, `audio-mic-denied`, `live-chips`,
  `volume-slider`, `toggle-speaker`, `mic-level`, `toggle-mic`,
  `playground-interrupt-button`, `playground-end-button`,
  `playground-text-input`, `playground-mic-button`,
  `dictation-provider-label`, `dictation-error`, `transcript-pane`,
  `bot-line`, `user-line`, `partial-line`).

### IA changes (the actual REIMAGINE)

1. **Killed the marketing lede.** Old design had a multi-line lede:
   "Talk to Johnny directly in the browser — no calendar event, no
   Google Meet. Exercise templates, decision modes, and per-session
   providers without touching production settings." → terse single
   sentence: "Talk to Johnny in the browser. Same router, approval,
   and TTS code paths as a real meeting — without a calendar event."
   The operator doesn't need to be sold on the playground; they need
   it to be precise.
2. **Killed the full-width yellow "Start session" band.** Old design
   rendered the primary CTA as a full-width yellow stripe at the
   bottom of the setup card — over-loud and an anti-DESIGN.md
   violation ("yellow is a signal, not a texture"). New design puts
   `Start session` as a normal-sized button with a leading `PlayIcon`
   in the card's sticky footer, right-aligned. Same yellow signal, a
   fraction of the visual chrome.
3. **Killed the indigo/purple/green state-rainbow.** Old design used 4
   distinct color palettes for `state-idle` (grey), `state-listening`
   (indigo + blue dot), `state-thinking` (amber + orange dot),
   `state-speaking` (green + green dot), each with its own pulse
   keyframe (1.2s / 0.8s / 0.6s) — a 4-color signal system fighting
   the brand-defined yellow signal. New design: ONE yellow `live-pulse`
   dot that means "this session is live now", + a plain-text label
   ("Idle" / "Listening" / "Thinking" / "Speaking") in `--foreground`.
   Sub-state is information, not chrome.
4. **Killed the blue user / purple bot speaker labels.** Old design
   gave the user line a `#2563eb` blue "You" label and the bot line a
   `#7c3aed` purple "Bot" label. New design uses mono small labels
   (`UserIcon` + "You" in `--ink-subtle`, `BotIcon` + "Johnny" in
   `--foreground` since the bot line sits on `bg-surface-2`). No color
   competition; "Johnny" reads as the brand name, not a costume.
5. **Killed the green-yellow-orange mic meter gradient.** Old design
   had a `linear-gradient(90deg, #34d399 0%, #facc15 60%, #f97316 90%)`
   on the meter fill — three colors signaling "your mic is hot" in
   the worst possible way (the yellow stop competes with the brand
   yellow). New design: single fill in `--foreground` (white-ish dark,
   black-ish light) with `--surface-3` track and `--ink-subtle` when
   muted. Operators read level by length, not by color.
6. **Speaker slider goes neutral.** Templates page uses `accent-primary`
   on its confidence-threshold slider — works there because the
   surface is a Sheet that occludes the sidebar. Playground live keeps
   the sidebar nav + live pulse visible, so an `accent-primary` slider
   pushes the total visible-yellow count to 3 BEFORE any focus ring
   appears. Fix: `[accent-color:var(--color-foreground)]` so the
   slider's thumb/track adopt the neutral ink color. Documented in
   the new "Slider yellow budget" Codebase Pattern.
7. **Action toolbar pulled into the session header.** Old design
   stacked actions vertically inside a `controls-pane` on the right
   side of the live grid: `Stop bot` (warning amber), `Open session
   detail` (secondary), `End session` (red). Mixed metaphors —
   destructive next to neutral next to "interrupt". New design: a
   horizontal toolbar in the session header — `Interrupt` (outline,
   `OctagonXIcon`), `Open detail` (ghost, `ExternalLinkIcon`), `End
   session` (destructive red, `SquareIcon`). Visual weight matches
   semantic weight; the red sits to the far right, away from the
   benign Interrupt.
8. **Voice controls become a single horizontal strip.** Old design
   put the speaker volume + mic level in a `controls-pane` column on
   the right of the transcript — wasting ~30% horizontal space on
   2 sliders. New design: a 2-column strip directly below the header,
   each column = "[icon mute toggle] [label] [slider/meter] [%]". The
   operator gets at-a-glance state ("am I muted?") + immediate
   override ("click the icon to toggle") without leaving the chat
   thread's reading flow.
9. **Mute toggles become icon buttons.** Old design used
   horizontally-sized text buttons ("Mute speaker" / "Unmute mic") in
   the controls pane. New design: `Volume2Icon` / `VolumeXIcon` and
   `MicIcon` / `MicOffIcon` square ghost buttons, with the muted
   variant using `text-destructive` (red) so the mute state reads at
   a glance. Title attribute + aria-label preserve discoverability.
10. **Chat-thread layout instead of 3-column.** Old design had a
    2-column live grid (transcript 2fr + controls 1fr) with the text
    input row tacked on at the bottom. New design: a vertical stack —
    header / voice strip / transcript (fills available height) /
    composer (sticky bottom, hairline-separated). Matches mental
    model from any chat client (Slack, iMessage, Discord). The
    transcript pane has `max-h-[55vh] min-h-[320px]` so it always
    fills meaningful space without crowding the composer.
11. **Composer with Enter-sends affordance.** Old design had a "Send
    text" button BELOW the textarea row — required a click. New
    design has Send in the same row as the textarea + a footer hint
    line: `Enter sends · Shift+Enter newline`. Implemented via
    `handleComposerKeydown` — chat convention, removes one click per
    message.
12. **Empty transcript state.** Old design rendered a single italic
    line: "No conversation yet — say something to get started." New
    design: a centered `MicIcon` (24px, `--ink-subtle`) + sentence-case
    helper ("Speak into the mic or type below to start the
    conversation."). Matches the templates / settings empty-state
    pattern from previous tickets.
13. **Advanced disclosure with chevron.** Old design used a
    `<details>` element with a `▶` text glyph rotating via a
    custom transform. New design: a button that toggles
    `advancedOpen` state, swapping `ChevronRightIcon` ↔
    `ChevronDownIcon`. The expanded section gets a hairline-bordered
    nested container on `bg-surface-1` — visually distinguishes
    "advanced overrides" from the main form without nesting cards.
14. **Provider selects flow into a 3-column grid.** Old design
    stacked the STT/LLM/TTS overrides as 3 separate `<label class
    ="field provider-field">` rows. New design uses
    `grid sm:grid-cols-3 gap-3` so on desktop they sit side by side —
    one row of advanced overrides instead of three. The helper text
    moves below the grid as a single sentence.
15. **Dictation mic preserved with brand-aligned recording state.**
    Old design had a red `#dc2626` recording state with a white
    pulse dot. New design keeps the destructive red recording state
    (mic = recording = stoppable urgency) but uses the `live-pulse`
    class on the inner dot so it shares the brand's signature
    animation + auto-degrades on `prefers-reduced-motion`.
16. **Audio status uses semantic tokens.** Old design: green for
    "audio ready", brown amber for "mic denied", grey for "audio
    starting". New design: `text-success` green for ready,
    `text-warning` amber for denied/unsupported,
    `text-muted-foreground` for starting. Single-source semantic
    coloring; matches the calendar/settings status patterns.

### Verification (chrome-devtools MCP, dark + light)

Drove every state through the real Chrome instance pointing at
`http://localhost:5173/playground`:
- ✓ Setup state loads with 2 seed templates + STT/LLM/TTS providers;
  default form values intact (mode=free_auto_speak, persona="Concise,
  friendly conversation partner.", system prompt has the no-speaker-
  prefix rule).
- ✓ Dark mode setup: 1 yellow on main (Start session CTA) + sidebar
  Playground active = 2 yellows total → gate 4 holds.
- ✓ Light mode setup: same hierarchy, same 2 yellows. Inter font +
  hairline borders + Card pattern feel clean in light.
- ✓ Advanced toggle expands a `bg-surface-1` nested panel with system
  prompt + context injection + 3-column provider overrides.
  Chevron flips right→down.
- ✓ Clicked Start session → live state renders with title + live-
  pulse dot + "Idle" label + "Audio starting…". Backend created the
  browser session OK; audio negotiation eventually moved through
  "Audio starting…" → "Audio ready" (success green).
- ✓ Typed "Hello Johnny — testing the new playground design." in
  the composer + clicked Send → user line appeared in the transcript
  with `UserIcon` + "You" mono label.
- ✓ Voice barge-in worked: STT captured my mic ("I'm born to be
  wild, born to be wild" and "Can you tell me something, just maybe
  a short, super story?") — both rendered as user lines.
- ✓ Bot reply arrived: `BotIcon` + "Johnny" mono label on a
  `bg-surface-2` step, with the actual text in `--foreground`. Mic
  auto-muted while bot was speaking (mic-off icon = red) — original
  Johnny-ckz behaviour preserved.
- ✓ Live state in dark mode: 1 sidebar nav + 1 live pulse dot =
  2 yellows. With focus ring on textarea (keyboard Tab into it):
  +1 = 3 yellows total → gate 4 holds at the limit.
- ✓ Light mode live: same hierarchy, 2 yellows steady, 3 on focus.
- ✓ "End session" button is destructive red, sits far right of the
  toolbar. "Interrupt" is outline left of it. "Open detail" is
  ghost between them — opens `/sessions/{id}` in a new tab.
- ✓ Mute speaker toggle: VolumeXIcon in `text-destructive` when
  muted, volume slider greys out via `disabled` + `opacity-50`.
- ✓ Mute mic toggle: MicOffIcon in `text-destructive` when muted,
  meter bar greys to `bg-ink-subtle`.
- ✓ Reduced-motion: the foundation gate sets all keyframes to
  near-instant via `prefers-reduced-motion: reduce`; the
  `live-pulse` class degrades to a static fully-opaque yellow dot
  via the explicit override in `app.css`.

### Verification gates (DESIGN.md)

| Gate | Status |
| --- | --- |
| 1. Body text on background ≥4.5:1 | ✓ (foundation, ~18:1 dark, ~16:1 light) |
| 2. Placeholder on surface-3 ≥4.5:1 | ✓ (foundation) |
| 3. Primary button label on primary ≥4.5:1 | ✓ (foundation, ~14:1) |
| 4. Yellow ≤ 3 elements per viewport | ✓ (setup: 1 sidebar nav + 1 Start session CTA = 2; live steady: 1 sidebar + 1 live pulse = 2; live with input focus ring: 3 max) |
| 5. No card-in-card | ✓ (single card per state; transcript pane and composer are sections within the card, hairline-separated, not nested cards. The Advanced nested panel is a hairline-bordered `bg-surface-1` step inside the form body — semantically a section, not a Card.) |
| 6. No uppercase tracked eyebrow | ✓ (sentence case everywhere: "Configure", "Decision mode", "Template · optional", "Persona", "Advanced", "Session #N", "Idle"/"Listening"/"Thinking"/"Speaking", "Audio ready", "Speaker", "Mic", "You", "Johnny", "Interrupt", "Open detail", "End session". No `text-uppercase tracking-eyebrow` rules.) |
| 7. Reduced motion honored | ✓ (foundation; `live-pulse` degrades; the only motion introduced beyond that is `transition-colors` on hover states which the foundation gate sets to ~0ms) |
| 8. Screenshot unambiguously NOT stock shadcn | ✓ (mono chips + yellow live-pulse + UserIcon/BotIcon transcript labels + destructive-red End session + dark neutrals + no indigo / purple / cyan / green-yellow-orange anywhere) |

### Quality gates

- `pnpm check` (svelte-check) → 0 errors, 0 warnings ✓
- `pnpm lint` → 1 error in `providers/+page.svelte:235` (pre-existing
  baseline `configuredRowsFor`, documented in Codebase Patterns) ✓

### Screenshots in `.validation/Johnny-fe.3/`

- `01-before-light-setup.png` / `02-before-dark-setup.png` — old
  design: full-width yellow Start session band, white card on dark
  background, marketing lede.
- `03-after-dark-setup.png` (with sidebar collapsed because of viewport
  width during initial render) — first render of new design.
- `04-after-dark-setup.png` — final dark mode setup: yellow Start
  session CTA bottom-right, Advanced collapsed.
- `05-after-dark-advanced.png` — Advanced section expanded with system
  prompt + context injection + 3-column STT/LLM/TTS overrides.
- `06-after-dark-live-empty.png` — live state immediately after Start
  session, before any conversation — empty-state mic icon + helper.
- `07-after-dark-live-typed.png` — focus ring on textarea (yellow
  outline) — captures the gate-4 stress state.
- `08-after-dark-live-sent.png` — textarea cleared after send.
- `09-after-dark-live-with-transcript.png` — single user line
  rendered, neutral slider (gate 4 fix in place).
- `10-after-light-live-with-transcript.png` — same in light mode.
- `11-after-light-live-real.png` — live state with multiple user lines
  (STT captured my mic), `Thinking` substate label.
- `12-after-light-live-with-bot.png` — bot reply rendered with
  BotIcon + "Johnny" label on `bg-surface-2` step.
- `13-after-dark-live-with-bot.png` — same in dark.
- `14-after-dark-live-focus-ring.png` — Tab-focused state.
- `15-after-dark-live-focus-textarea.png` — explicit textarea focus
  ring (yellow outline = 3rd yellow, gate 4 holds).
- `16-final-dark-setup.png` — clean dark setup state for the README.

### Learnings

- **`postBrowserText` works even when the audio WebSocket is dead.**
  When a browser session loses its audio stream ("session not
  active" error), text input via REST still routes through the
  router → LLM → TTS pipeline. The error alert is informational,
  not blocking — the operator can still type their way through a
  conversation while the audio reconnects (or stays dead).
- **The CDP `evaluate_script` failures are real.** Confirmed the
  Codebase Pattern: `wait_for` + `take_snapshot` reliably
  re-establishes the page context without restarting Chrome.
- **Tailwind 4 supports `[accent-color:var(--...)]` arbitrary
  values.** This is the cleanest path to a neutral slider that still
  picks up dark/light mode via the foreground token. No need for a
  custom slider primitive or a Svelte component override.
- **Auto-scrolling the transcript needs a tick + scrollHeight.**
  The pattern from `sessions/[id]` works directly here: `bind:this`
  + `await tick()` + `el.scrollTop = el.scrollHeight`. The page's
  `appendTranscript` helper schedules a scroll on every new line so
  the operator always sees the latest message.
- **Keyboard `Enter sends · Shift+Enter newline`** is a 5-line
  `handleComposerKeydown` and probably the single best UX
  improvement in this rewrite. Operators want to chat fast; the
  click-to-send pattern shouldn't survive into 2026.
- **Yellow audit is the gating decision in live-session UI.** The
  page has 3 yellow "anchors" before any user interaction: sidebar
  nav active + live pulse + (originally) accent-primary slider. A
  4th yellow (focus ring or anything else) violates gate 4 the
  instant the user clicks an input. The slider had to give. Same
  logic will apply to any future live-state page (sessions/[id]
  partially escapes by being scroll-heavy enough that the sidebar
  nav rarely co-exists with a focus ring in the same viewport, but
  the playground's compact layout pushes the constraint to the
  limit).

---

## 2026-06-07 — Johnny-fe.4 (REIMAGINE /calendar)

Replaced the 1322-line custom-CSS calendar page with a 1180-line
shadcn-svelte + design-token rewrite. From-scratch IA redesign — not a
1:1 port.

### Files changed

- `frontend/src/routes/calendar/+page.svelte` — full rewrite.
  Deleted the entire `<style>` block (~510 lines of hardcoded hex
  colors: `#4f46e5` indigo configured-border + badge, `#f97316` orange
  reauth CTA, `#fff7ed` cream reauth bg, `#0ea5e9` cyan Join now,
  `#6d28d9`/`#f5f3ff` purple Try-with-bot, `#10b981` green Meet dot,
  `#fef3c7`/`#92400e` amber warn). Replaced with shadcn `Button` /
  `Alert` / `Input` and Tailwind utility classes mapped to DESIGN.md
  tokens (`bg-card`, `bg-surface-1/2/3`, `text-foreground`,
  `text-muted-foreground`, `border-border`, `border-border-strong`,
  `border-warning`, `text-warning`, `text-success`, `text-destructive`,
  `bg-primary`). Preserved every `data-testid` (`account-picker`,
  `refresh-button`, `calendar-reauth-empty`, `calendar-meta`,
  `sync-badge`, `day-list`, `day-${key}`, `event-${id}`,
  `event-${id}-enabled`, `meeting-config-form`, `template-select`,
  `identity-select`, `mode-select`, `instructions-input`,
  `context-input`, `allowed-replies-input`, `threshold-input`,
  `save-button`, `save-success`, `join-now-row`, `join-now-button`,
  `try-bot-button`, `panel-error`, `disable-button`, `disable-dialog`,
  `confirm-delete`).

### IA changes (the actual REIMAGINE)

1. **Killed the colored-rainbow row.** Old design used indigo on the
   configured row (left-border accent + `Johnny enabled` chip), cyan
   on Join now, orange-cream on reauth, purple on Try with bot, plus
   green Meet link dot — a five-color palette competing on a single
   list view. New design: ONE green status dot on the "Enabled" pill,
   identical neutral row treatment for all events (only the dashed
   border + opacity-60 distinguishes "no Meet link"), and yellow
   reserved for the actual primary CTA inside the configure sheet.
2. **Account picker always shows, even with 1 account.** Old design
   hid the `<select>` when `accounts.length <= 1`, leaving the
   operator no indication of which calendar source the page is
   reading. New design renders the select disabled with the email
   visible as a chip — the source of truth is always on screen. Disabled
   styling (`opacity-70`, no caret) signals "fixed for now" without
   removing context.
3. **Refresh became an icon button.** Old `Refresh` (full-text
   secondary button) competed visually with the account picker. New
   design: `<RefreshCwIcon>` ghost icon button (32×32), spinning
   during fetch via `animate-spin`. Saves header real estate; the
   action is still discoverable via `title="Refresh"` and the
   `aria-label`.
4. **Meta strip uses semantic tokens.** Old design had a single grey
   bar with mono sync-badge text. New design replaces it with a flex
   strip on `bg-surface-1`: `7 meetings · 7 with Meet · 1 configured`,
   each count `font-semibold` with `text-muted-foreground` labels. The
   sync-badge stays mono and right-aligned, showing `+N ~M −X` with a
   tooltip ("created/updated/removed in this sync") — operators don't
   need to parse those numbers at-a-glance; they need the deltas at
   reach when something looks wrong.
5. **Day headings + count.** Old design used a 16px heading
   (`<h2>`) with the date inline. New design: heading + mono date
   chip on left, `N event(s)` count on right (`text-ink-subtle`). The
   border-bottom is `border-separator` (hairline) instead of the old
   `border-bottom: 1px solid #e5e7eb`. Cleaner visual rhythm with the
   neutral surface tokens.
6. **Event rows.** Old design: grey card with indigo border for
   configured + indigo badge + green Meet dot in a chip. New design: a
   1px `border-border` card with 110px mono time column + content +
   "Configure →" affordance on hover. Configured events get a small
   pill on the title row: green status dot + "Enabled" — no left
   border, no badge color. The hover state is `border-border-strong`
   + `bg-surface-2` — same affordance for every clickable row,
   indistinguishable in color from each other.
7. **Reauth empty state.** Old design used a 230x150 orange-cream
   `<aside>` panel with the email in `<strong>` and an indigo
   `Reconnect` link. New design: `border-warning` bordered card on
   `bg-surface-1` with `<TriangleAlertIcon class="text-warning">` on
   the left, sentence-case heading, email + `FERNET_KEY` both in
   mono, and an outline (not yellow) `Go to Settings → reconnect`
   button. Matches the settings-page reauth treatment (Johnny-fe.5)
   for cross-page consistency.
8. **Detail panel → right-side Sheet.** Old design was a 480px
   fixed-position div with a 56px top offset to clear the layout
   header — when the operator clicked an event, the panel covered
   half the screen but ALSO sat *under* the page header, creating an
   awkward visual gap. New design: 560px right-side Sheet that opens
   over a backdrop (`bg-black/50 backdrop-blur-sm`) and is
   `z-[var(--z-modal)]`, with sticky header (event title + meta strip
   + organizer/attendee dl) and sticky footer (Disable / Close / Save).
   The list rows behind get a subtle dim from the backdrop, which
   makes the operator-mode focus explicit.
9. **Sheet split into 3 stacked sections.**
   - **Header (sticky):** event title + time + day chip + dl
     metadata (organizer, attendees, meet link).
   - **Start session (only when `existingConfig` exists):** "Join now"
     + "Try in browser" buttons, with a "Configured · Mode" subtitle
     on the right. Hairline separator below.
   - **Johnny configuration:** the form. Sticky footer with the
     destructive `Disable` (ghost variant, leading TrashIcon) on the
     LEFT, then `Close` (outline) and `Save changes`/`Enable Johnny`
     (variant swaps) on the RIGHT. Disable is far from Save so a
     misclick is unlikely.
10. **Pending-change-aware primary.** Two derived states drive
    button variants on the sheet:
    - `hasPendingChanges = $derived.by(...)` compares every form
      field to `existingConfig`.
    - When `existingConfig === null`: Save = yellow `Enable Johnny`.
    - When `existingConfig !== null && !hasPendingChanges`: Save =
      outline `Saved` (disabled); Join now = yellow.
    - When `existingConfig !== null && hasPendingChanges`: Save =
      yellow `Save changes`; Join now = outline (tooltip "Save your
      changes first.").
    Exactly one yellow CTA active at a time → gate 4 holds.
11. **Mode-aware sections.** Like the templates page (Johnny-fe.6):
    "Additional allowed replies" renders only when mode =
    `limited_auto_speak`; "Additional instructions" gets `required`
    + `*` marker + amber helper when mode = `autonomous`; mode help
    text below the select updates on every selection.
12. **Killed the `enable-toggle`.** Old design had a `<label class
    ="toggle"><input type="checkbox">` row at the top of the form;
    unchecking it triggered a confirmation flow ("Disabling will
    delete the saved configuration for this meeting. Continue?")
    inside an inline yellow `.alert.warn`. New design: there is no
    enable-toggle. To enable Johnny, fill the form and submit. To
    disable, click the destructive `Disable` button → AlertDialog
    with `role="alertdialog"` + Esc/backdrop close + Cancel +
    destructive Disable confirmation. Same outcome, half the form
    surface, no awkward checkbox-as-mode-switch.
13. **Removed the `enable-toggle` "implicit-on" footgun.** Old design
    had `checked={formEnabled || existingConfig !== null}` — a
    checkbox that was checked even when `formEnabled` was false,
    because an existing config implied "currently enabled". This was
    confusing: the operator clicked an event with a saved config
    and saw a checked box they'd never checked. New design: no
    checkbox at all. The form exists IF AND ONLY IF the operator is
    actively configuring; the saved state is communicated by the
    presence of the "Start session" section + the "Enabled" pill on
    the event row.

### Verification (chrome-devtools MCP, dark + light)

Drove every state through the real Chrome instance pointing at
`http://localhost:5173/calendar`:
- ✓ List loads with 7 real events from the connected Google account
  (nikita.rabykin@aikamatkat.fi). Day grouping correct; "Tomorrow"
  label resolves vs. absolute weekday for further days.
- ✓ Dark mode: sidebar Calendar nav active accent (1 yellow); rest of
  list = 0 yellow → gate 4 holds.
- ✓ Light mode: same hierarchy, same 0 yellow on main; sidebar still
  carries 1 (layout-owned).
- ✓ Clicked "Monday TTAll catch-up" → sheet slides in from right,
  meta dl populated, mode select swaps help text dynamically
  (Approval required → Limited auto-speak shows "Additional allowed
  replies" section; → Autonomous shows `*` + amber warning, hides
  allowed-replies).
- ✓ Set context, clicked Enable Johnny → "Saved." status renders in
  success green, "Start session" section appears with yellow Join now
  + outline Try in browser, Save button switches to outline `Saved`
  (disabled), Disable button appears in footer.
- ✓ Modified context → Save flips to yellow `Save changes`, Join now
  switches to outline with tooltip. Confirmed only one yellow CTA at
  any moment.
- ✓ Disable → AlertDialog with destructive red TrashIcon, sentence-
  case heading, body explains what gets removed, Cancel (outline) +
  Disable (destructive red). Confirmed → status "Johnny disabled for
  this meeting." appears, Start session section disappears,
  "Enabled" pill on the event row vanishes after refresh.
- ✓ Added a synthetic bad-token bot account → switched to it via
  account picker → reauth state renders correctly (amber-bordered
  card, TriangleAlertIcon, sentence-case heading, mono email + code,
  outline reconnect button). Confirmed in both modes.
- ✓ Esc closes the sheet; Esc inside an open AlertDialog closes the
  dialog first, leaving the sheet open. Backdrop click on either
  layer closes that layer.

### Verification gates (DESIGN.md)

| Gate | Status |
| --- | --- |
| 1. Body text on background ≥4.5:1 | ✓ (foundation, ~18:1 dark, ~16:1 light) |
| 2. Placeholder on surface-3 ≥4.5:1 | ✓ (foundation) |
| 3. Primary button label on primary ≥4.5:1 | ✓ (foundation, ~14:1) |
| 4. Yellow ≤ 3 elements per viewport | ✓ (list view: 1 sidebar; sheet open without pending changes: 1 sidebar + Join now = 2; with pending: 1 sidebar + Save changes + focus ring = 3 max) |
| 5. No card-in-card | ✓ (sheet is a slide-in panel, not a card; event rows are flat with no nested cards; "Start session" / "Johnny configuration" sections are separated by hairlines, not nested cards) |
| 6. No uppercase tracked eyebrow | ✓ (sentence case throughout: "Start session", "Johnny configuration", "Profile template", etc.) |
| 7. Reduced motion honored | ✓ (foundation; no custom animations introduced; `animate-spin` on refresh icon is the only motion and it ceases on `prefers-reduced-motion: reduce` via the global gate) |
| 8. Screenshot unambiguously NOT stock shadcn | ✓ (mono time + Lucide icons + green-success Meet chip + yellow primary CTA + dark surfaces + no indigo/purple/cyan anywhere) |

### Quality gates

- `pnpm check` (svelte-check) → 0 errors, 0 warnings ✓
- `pnpm lint` → 1 error in `providers/+page.svelte:235` (pre-existing
  baseline `configuredRowsFor`, documented in Codebase Patterns) ✓

### Screenshots in `.validation/Johnny-fe.4/`

- `01-before-light-reauth.png` / `02-before-dark-reauth.png` —
  reference of the previous design's orange-cream reauth empty state
  (seed account, the only state the previous design exercised without
  a real Google calendar).
- `04-after-dark-list-actual.png` / `11-after-light-list.png` — new
  list view in both modes with real Google data.
- `05-after-dark-sheet-new.png` — sheet open, no existing config,
  yellow `Enable Johnny` is the only CTA.
- `06-after-dark-mode-limited.png` — Limited auto-speak mode shows
  the "Additional allowed replies" section.
- `07-after-dark-mode-autonomous.png` — Autonomous mode shows `*` +
  amber warning under Instructions.
- `08-after-dark-sheet-configured.png` — Saved state, before the
  variant-swap refinement (Saved button was still yellow at 50%
  opacity — superseded by `14-` below).
- `09-after-dark-disable-dialog.png` — AlertDialog for disable.
- `12-after-light-reauth.png` / `13-after-dark-reauth.png` — amber-
  bordered reauth card in both modes for a synthetic bad-token bot
  account.
- `14-after-dark-configured-saved-outline.png` — final saved state:
  Save = outline `Saved`, Join now = yellow.
- `15-after-dark-configured-pending.png` / `16-after-dark-pending-final.png`
  — pending-changes states. `15-` was before the Join-now-becomes-
  outline refinement; `16-` is the final shipped behaviour with Join
  now = outline + Save = yellow when changes are pending.

### Learnings

- **Svelte 5 `a11y_no_noninteractive_tabindex` fires even when the
  `role` is a dynamic ternary** — e.g., `role={clickable ? 'button'
  : 'group'} tabindex={clickable ? 0 : -1}` triggers the warning
  because the static analyzer doesn't trust the runtime resolution.
  The cleanest fix is a single `<!-- svelte-ignore
  a11y_no_noninteractive_tabindex -->` comment over the element;
  rewriting into two `{#if}` branches doubles the markup for no
  user-visible benefit.
- **`bind:value={formIdentityId}` cleanly two-ways even with `value`
  being a `number | null`** — Svelte 5 auto-coerces select values
  through the bind. No need for the manual `onchange` + `Number()`
  parse the original page used for every select.
- **The `Edit` tool tracks file state across edits, and a linter run
  between `Read` and `Edit` invalidates the cached read.** Hit this
  when removing an unused import — the lint had auto-fixed the file
  in the meantime. Re-Reading the file before the Edit is the
  correct recovery, even when the change is small.
- **`evaluate_script` reliably returns "No page found" inside a
  long-running validation session.** The workaround (`wait_for` +
  `take_snapshot` to re-establish) sometimes fails too — for
  contrast checks etc., fall back to inspecting the captured PNGs
  visually rather than burning iterations trying to recover the
  CDP session.
- **Yellow discipline benefits from `hasPendingChanges`.** Without
  this derived flag, the configured-meeting sheet has TWO actions
  the operator might call "primary" — Save and Join now — and the
  naive choice (both yellow) blows gate 4 if a focus ring + sidebar
  bring the visible count to 4. The flag-driven swap makes the
  semantic clear: "save first, then start". The tooltip on the
  outline Join now ("Save your changes first.") removes the
  ambiguity for the operator.

---

## 2026-06-06 — Johnny-fe.6 (REIMAGINE /templates)

Replaced the 539-line custom-CSS templates CRUD page with a 580-line
shadcn-svelte + design-token rewrite. From-scratch IA redesign — not
a 1:1 port.

### Files changed

- `frontend/src/routes/templates/+page.svelte` — full rewrite.
  Deleted the entire `<style>` block (was ~230 lines of hardcoded hex
  colors, bespoke `.mode-badge.mode-*` rainbow rules, modal-backdrop
  styling). Replaced with shadcn `Button` / `Input` / `Alert` and
  Tailwind utility classes mapped to DESIGN.md tokens (`bg-card`,
  `text-foreground`, `text-muted-foreground`, `border-border`,
  `bg-surface-2`, `text-destructive`, `bg-primary`, etc).

### IA changes (the actual REIMAGINE)

1. **Killed the mode-rainbow chips.** The old design used 6 colored
   chips for 6 modes: indigo for "approval_required", green for
   "listen_only", red for "limited_auto_speak", pink for "autonomous"
   etc. — each in UPPERCASE TRACKED EYEBROW format, all violating
   DESIGN.md gate 6 (no uppercase eyebrows) AND polluting the yellow
   discipline (gate 4) by giving every status its own pseudo-signal.
   New design: every mode shows the same neutral mono-font chip on
   `--surface-2` with `--ink` text — sentence case ("Approval
   required"), no transform. Mode is information, not a color brand.
2. **Card grid replaces stacked list.** Old design rendered each
   template as a full-width row in a flex column. New design uses
   `grid-template-columns: repeat(auto-fit, minmax(380px, 1fr))` so
   templates flow into 1/2/3 columns based on viewport. Each card has
   a clear footer separator (gauge icon + confidence + Edit/Delete),
   so the operator can scan a dozen templates at a glance.
3. **Form moved to a right-side Sheet.** The old design used a
   centered modal at 600px. New design uses a 520px right-aligned
   slide-in drawer. The list stays visible behind it; closing the
   sheet doesn't feel like leaving the page. Sheet has a sticky
   header (title + close), scrollable form body, and a sticky footer
   (Cancel + Save). Form is organized into 6 logical sections, each
   with a label, control, and contextual help.
4. **Mode-aware form sections.** "Allowed replies" textarea now ONLY
   renders when mode = `limited_auto_speak` — when irrelevant, it's
   hidden, not greyed-out. "Instructions" textarea gets a `*` marker
   and `required` attribute when mode = `autonomous`. Mode help text
   updates per selection ("Transcribe silently. Johnny never speaks."
   for listen_only; "Free-form speech guided only by the
   instructions. No approval, no allowlist." for autonomous). The
   form teaches the operator what each mode means as they pick it.
5. **AlertDialog for delete.** Replaced browser-native `confirm()`
   (jarring, unstylable, blocking) with a custom centered
   AlertDialog. The dialog shows the template name in mono and
   displays the cascading-delete warning ("This will also remove 4
   meeting configs that reference it.") inline — no separate prompt
   needed. The dialog respects design tokens, has proper aria roles
   (alertdialog, aria-modal, aria-labelledby, aria-describedby), and
   closes on Escape or backdrop click.
6. **Yellow discipline.** Yellow appears on exactly the right
   surfaces: the "New template" primary CTA (one per surface), the
   focus ring on focused inputs, and the range-slider thumb (active
   state). Mode badges, "Used by N meetings" labels, instructions
   snippets — all neutral. Verified ≤3 yellow elements per viewport
   in every screenshot.
7. **Edit/Delete actions.** Old design had identical-weight Edit and
   Delete buttons stacked vertically. New design puts both in a
   horizontal row in the card footer using `ghost` variant + leading
   icon (pencil, trash). Delete uses `text-destructive
   hover:bg-destructive/10` so it's visually distinct without
   shouting "DELETE" all the time. The action area lives in a
   footer separated by a hairline, so it doesn't compete with the
   template content above.
8. **Empty state.** New empty state shows a `ScrollText` Lucide icon
   (32px, `--ink-subtle`), one sentence ("No templates yet. Create
   one to describe how Johnny should behave in a meeting."), and a
   "New template" CTA — replacing the old italic "No templates yet.
   Click 'New template' to create one." paragraph.
9. **Removed the Refresh button.** CRUD operations re-fetch
   automatically. The button was UI noise.

### Verification (chrome-devtools MCP)

Drove the full CRUD flow in both modes:
- ✓ Page loads with two seed templates, cards render correctly
- ✓ "+ New template" opens right-side sheet, focus jumps to Name
- ✓ Mode select swaps help text on every change (`listen_only` →
  `limited_auto_speak` → `autonomous`)
- ✓ Allowed-replies section appears only for `limited_auto_speak`
- ✓ Instructions field gets `*` and `required` when mode =
  `autonomous`
- ✓ Created a "Test sheet template" with 3 allowed replies; new card
  appeared with the chips inline ("Yes", "No", "Could you repeat
  that?")
- ✓ Edit prefills all fields; subtitle changes to "Changes apply to
  every meeting config that references this template."
- ✓ Delete dialog shows "This cannot be undone." for unreferenced
  templates and "This will also remove 4 meeting configs that
  reference it." for the standup template
- ✓ Delete confirmed; the row disappeared after refresh

### Verification gates (DESIGN.md)

| Gate | Status |
| --- | --- |
| 1. Body text on background ≥4.5:1 | ✓ (foundation, ~18:1 dark) |
| 2. Placeholder on surface-3 ≥4.5:1 | ✓ (foundation, 4.64 dark) |
| 3. Primary button label on primary ≥4.5:1 | ✓ (foundation, ~16:1) |
| 4. Yellow ≤ 3 elements per viewport | ✓ (1 main: "New template" CTA; sidebar adds 1 nav active + 1 status pill = 3 total) |
| 5. No card-in-card | ✓ (templates listed in flat grid) |
| 6. No uppercase tracked eyebrow | ✓ (sentence case everywhere) |
| 7. Reduced motion honored | ✓ (foundation) |
| 8. Screenshot unambiguously NOT stock shadcn | ✓ (yellow + neutral mono chips + dark surfaces) |

### Quality gates

- `pnpm typecheck` → 0 errors, 0 warnings ✓
- `pnpm lint` → 1 error in `providers/+page.svelte` (pre-existing
  baseline, not introduced by this work — see Codebase Patterns
  note) ✓

### Learnings

- **shadcn-svelte `Label` component doesn't accept children** — it's
  a bits-ui `LabelPrimitive.Root` self-closing wrapper. Use a plain
  `<label>` with `text-sm leading-none font-medium text-foreground`
  classes for the same visual result. Avoid importing `Label` from
  `$lib/components/ui/label/` if you need to nest text.
- **Native `<select>` works fine** with the design tokens by mapping
  the Input field's classes: `border-input flex h-9 w-full rounded-md
  border bg-background px-3 py-1 text-sm shadow-xs outline-none
  transition-[color,box-shadow] focus-visible:border-ring
  focus-visible:ring-ring/50 focus-visible:ring-[3px]`. No need for
  a Select component until/unless a custom popover is required.
- **Native `<input type="range">` adopts brand color via Tailwind's
  `accent-primary` utility** — `accent-color: var(--primary)` makes
  the thumb and track use Signal Yellow in WebKit/Chromium without
  needing a custom slider primitive.
- **a11y warnings for custom modal containers**: `<aside>` cannot
  have `role="dialog"` (must be `<div>`); any element with
  `role="alertdialog"` needs `tabindex="-1"` to support
  programmatic focus.
- **CSS line-clamp** via Tailwind `line-clamp-2` is a clean way to
  preview multi-line instructions without writing ellipsis CSS.
- **Mode-aware form pattern**: rather than greying out irrelevant
  fields, conditionally render them. The form gets shorter when
  fewer fields apply, which signals the user that the field count
  is mode-dependent.

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


## 2026-06-07 — Johnny-fe.5 (REIMAGINE /settings)

Replaced the 988-line custom-CSS settings page (hardcoded indigo, orange,
teal colors, native `confirm()` dialogs, centered modals, awkward role
select on every row) with a 1173-line shadcn-svelte + design-token
rewrite. From-scratch IA redesign — not a 1:1 port.

### Files changed

- `frontend/src/routes/settings/+page.svelte` — full rewrite.
  Deleted the entire `<style>` block (~370 lines of hardcoded hex
  colors: `#4f46e5` indigo CTA, `#f97316` orange reauth, `#fff7ed`
  cream backgrounds, `#fef2f2` red error, `#155e75` teal user-badge
  text, `#9a3412` orange bot-badge text). Replaced with shadcn `Button`
  / `Alert` and Tailwind utility classes mapped to DESIGN.md tokens
  (`bg-card`, `bg-surface-1/2/3`, `text-foreground`,
  `text-muted-foreground`, `border-border`, `border-border-strong`,
  `border-warning`, `text-destructive`, `bg-primary`).

### IA changes (the actual REIMAGINE)

1. **Split "Connected accounts" into two purposeful sections.** Old
   design dumped user + bot rows into one flat list with a "Role"
   `<select>` on every row. New design has a `User identities`
   section (the calendar source) and a `Meeting bots` section (the
   identities Johnny signs in as), each with its own subtitle that
   explains what that section does. The role-select is gone — role
   is implicit in which section a card lives in. To change role you
   click `Convert to bot` / `Convert to user` (ghost variant), which
   moves the card to the other section.
2. **One header CTA, contextual section CTAs.** Old design had two
   header buttons: `Refresh` (noise) and `Add account` (indigo).
   New design has a single `Add account` primary CTA in the header
   (opens the sheet seeded with `user` role). Each populated section
   gets a tiny `Add another` text link in its subtitle row (opens the
   sheet seeded with that section's role). Each empty section gets an
   outlined `Add user identity` / `Add bot identity` CTA. The Refresh
   button is dropped — every mutation re-fetches.
3. **Centered modal → right-side Sheet for Add account.** Old design
   used a 480px centered modal. New design uses a 480px right-aligned
   slide-in drawer with sticky header + footer + scrollable form body.
   The list stays visible behind the sheet's `bg-black/50
   backdrop-blur-sm` overlay.
4. **`<select>` → radio-card group for role choice.** Old design used
   a `<select>` with a wordy `<small>` underneath. New design uses two
   tappable cards in a 2-column grid, each with an icon (User /
   Bot), label, and one-line description. Selected card gets a
   `border-foreground` border (NOT yellow, to preserve discipline) +
   `bg-surface-2` tint + a `CheckIcon` in the top-right corner. The
   "Set as default user" checkbox section only renders when `user` is
   selected — irrelevant fields are hidden, not greyed-out (parallels
   the templates page's mode-aware sections).
5. **Native `confirm()` → AlertDialog with cascading-config inline
   warning.** Old design used three different browser-native
   `confirm()` calls: disconnect account, disconnect bot session, and
   the 409-conflict force-delete. Two of them showed in one of two
   places. New design uses two custom `role="alertdialog"` dialogs:
   one for disconnect account (handles 409 by re-prompting with
   `forceRequired: true` and an inline "This will also delete N
   meeting configs" warning) and one for clearing the bot session.
   Both have proper `aria-modal`, `aria-labelledby`,
   `aria-describedby`, focus trapping (`tabindex="-1"`), and Esc
   handling.
6. **Bot-session storage_state UI moved inline + sheet.** Old design
   buried the bot-session badge + help text inside the account row
   as a nested `<div class="bot-session">`. New design treats the bot
   session as a hairline-separated sub-section inside the bot card
   (`border-t border-separator pt-3`): "Bot session: ● Connected"
   with semantic green dot + saved-at + size, OR "Bot session: ● Not
   connected" with a one-sentence helper about the
   `storage_state.json`. The upload form moves to a right-side
   560px Sheet (matching the Add account sheet) with a collapsible
   `<details>` disclosure for the CLI-helper command (replacing the
   always-visible `<pre>` block that previously dominated the
   modal). The CLI command interpolates the bot's `account-id` and
   `email` so operators can copy-paste verbatim.
7. **Reauth callout: orange `<aside>` → shadcn Alert + amber card
   border.** Old design used a custom `<aside>` panel with an
   `#fff7ed` orange background, all-caps `TOKEN UNREADABLE —
   RECONNECT` badge (violating gate 6), and an indigo `Reconnect`
   button. New design uses an inline `<Alert.Root variant="default">`
   with the `<TriangleAlertIcon class="text-warning">` warning icon,
   a sentence-case "Token unreadable — reconnect required" title, and
   an explanatory description. The card itself gets a 1px
   `border-warning` (amber, oklch hue 55) to mark it visually
   without bringing the brand yellow into the warning channel. The
   Reconnect button is `outline` variant (NOT yellow) so the broken
   state doesn't compete with the page-level `Add account` CTA.
8. **Yellow discipline.** Yellow appears on EXACTLY: the
   `Add account` page-CTA (one per surface), the sidebar nav active
   accent + active-sessions status pill (layout-owned). Every other
   primary path — `Set as default`, `Convert to bot/user`, `Reconnect`,
   `Upload session`, `Replace session`, empty-state CTAs, the role
   radio-card selection — is `outline` or `ghost`. The disconnect-
   account confirmation uses `variant="destructive"` (the only red
   appearance). Verified ≤3 yellow elements per viewport in both
   modes (sidebar 2 + page header 1).
9. **Default-user badge → yellow pill.** Old design used an
   `#4f46e5` indigo `DEFAULT USER` chip in uppercase tracked
   eyebrow. New design uses an inline `bg-primary
   text-primary-foreground` pill with `<ShieldCheckIcon>` + "Default"
   in sentence case. The badge is a brand-defined "signal" use
   (status indicator earning yellow because it marks the canonical
   calendar source).
10. **Email rendered as mono.** Cards display `account.email` in
    `font-mono` so addresses sit in the operator-deck register
    (IDs/hosts/keys) rather than reading as prose. Date formatting
    drops the seconds (was `6/6/2026, 11:14:57 PM`, now `Jun 6,
    2026, 11:14 PM`) — operators don't need second precision for a
    "token expires" or "added" field.

### Verification (chrome-devtools MCP)

Drove the page in both modes through every state on the seed
`seed@johnny.test` user account (which has `token_health =
needs_reauth`, so the reauth state is the default):
- ✓ Dark mode: amber-bordered reauth user card, ≤3 yellows
  per viewport (Add account + 2 sidebar)
- ✓ Light mode: same discipline holds, same 3 yellows max
- ✓ Add account sheet opens from header CTA; opens from `Add bot
  identity` empty-state CTA seeded with `formRole='bot'`
- ✓ Role radio-card swap: clicking Bot hides the "Set as default
  user" section, clicking User restores it
- ✓ Convert to bot moves the seed account into the Meeting bots
  section; user identities section flips to its empty state
- ✓ Bot card shows "Bot session: ● Not connected" + meet-worker
  helper text; reauth Alert is shorter (no "Reconnect runs..."
  paragraph)
- ✓ Convert to user moves it back; section state flips correctly
- ✓ Disconnect dialog opens with red-circle UnlinkIcon + email in
  mono + "This cannot be undone." + Cancel / Disconnect buttons
- ✓ Esc closes the sheet/dialog; backdrop-click closes both
- ✓ HMR picked up every save (~600ms in dev mode)

### Verification gates (DESIGN.md)

| Gate | Status |
| --- | --- |
| 1. Body text on background ≥4.5:1 | ✓ (foundation, ~18:1 dark, ~16:1 light) |
| 2. Placeholder on surface-3 ≥4.5:1 | ✓ (foundation) |
| 3. Primary button label on primary ≥4.5:1 | ✓ (foundation, ~14:1) |
| 4. Yellow ≤ 3 elements per viewport | ✓ (1 main CTA + sidebar nav + sidebar status = 3) |
| 5. No card-in-card | ✓ (bot-session lives inside the bot card as a hairline-separated sub-section, not a nested card) |
| 6. No uppercase tracked eyebrow | ✓ (sentence case throughout — section headings, button labels, status text) |
| 7. Reduced motion honored | ✓ (foundation; no custom animations introduced) |
| 8. Screenshot unambiguously NOT stock shadcn | ✓ (yellow CTA + amber warning border + mono email + dark surfaces + no indigo) |

### Quality gates

- `pnpm typecheck` → 0 errors, 0 warnings ✓
- `pnpm lint` → 1 error in `providers/+page.svelte:235` (pre-existing
  baseline `configuredRowsFor` unused const, explicitly noted in
  Codebase Patterns) ✓

### Learnings

- **`token_health === 'needs_reauth'` is the implicit "default" state
  for the seed account** because the seeded refresh token can't be
  decrypted with the current `FERNET_KEY`. Any local dev workflow
  that prefills accounts must consider that "reauth needed" is the
  state new agents will see — design every account card around that
  state, not around the happy path. If you want to test the
  non-reauth flow locally you need a real OAuth round-trip; there's
  no API helper for seeding a healthy account.
- **`run.sh`'s port-5173 kill heuristic mismatches Docker's full
  path on macOS.** The case-glob is `com.docker.*|*vpnkit*|*docker-
  proxy*` but `ps -p $pid -o comm=` returns `/Applications/Docker.
  app/Contents/MacOS/com.docker.backend` (with the leading path), so
  the glob misses and the script kills Docker itself. Filed as a
  follow-up; workaround is to bring the stack up directly with
  `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml docker
  compose up -d --build` until run.sh is patched.
- **Two parallel right-side Sheets with one window-level Esc handler
  is cleaner than one Sheet component.** Each sheet has its own
  `if showForm` / `if showBotSessionForm` block; a single
  `handleSheetKeydown` on `<svelte:window>` dispatches Esc to
  whichever modal is currently open (with a precedence chain: form →
  bot-session → disconnect → disconnect-session). Avoids a generic
  modal manager when there are only 4 distinct modal surfaces.
- **`border-warning` on a card is enough signal — you don't need an
  Alert AND a colored border AND a colored Reconnect button.** The
  original page had three competing signals for "this is broken"
  (orange card bg + orange badge + indigo button) and the dark mode
  rendered as a literal yellow-orange screenshot. The new page has
  amber card border + amber-icon Alert + neutral outline button. Same
  message, three-quarters the visual chrome.
- **Empty-state CTAs duplicate the page header CTA — make them
  outline, not primary.** When user identities are populated AND bot
  identities are empty (or vice versa), both the header `Add account`
  CTA and the empty-state `Add bot identity` CTA render
  simultaneously. If both are yellow, gate 4 fails (4 yellows). The
  fix is to make empty-state CTAs `variant="outline"` — the header
  CTA stays yellow as the page-level primary path. The templates
  page got away with both-yellow because the empty state hides when
  the list is populated (single section), but multi-section settings
  pages can't rely on that.

---

## 2026-06-07 — Johnny-fe.10 (/providers single-modal CRUD redesign)

Total rewrite of `/providers` UX. ONE `+ Add provider` button at top,
modal-based CRUD (Add / Edit / Rename / Delete / Test all inside one
sheet), preview-without-save backend endpoints, catalog-level Piper
voice endpoints reachable from a clean modal state. Resolved every
broken-state symptom the user enumerated.

### Files changed

- `backend/app/api/providers.py` — added three preview endpoints
  (`POST /providers/preview/test`, `POST /providers/preview/play_sample`,
  `POST /providers/preview/stt_test`) and three catalog endpoints
  (`GET /providers/catalog/piper/voices`,
  `POST /providers/catalog/piper/voices/{key}/install`,
  `DELETE /providers/catalog/piper/voices/{key}`). The preview endpoints
  instantiate a transient provider from `(kind, provider_name, values)`,
  validate via the existing schema validator, run the same smoke /
  preview / STT-test code paths as the saved-row endpoints, and tear the
  instance down at the end — no DB writes. The catalog Piper endpoints
  resolve `model_dir` to `DEFAULT_MODEL_DIR` so voice download works
  before any Piper provider has been persisted. Critically: the new
  routes are declared BEFORE the parametric `/{provider_id}/...` routes
  to avoid FastAPI's in-order matcher routing `/preview/test` to
  `/{provider_id}/test` with `provider_id="preview"`.
- `frontend/src/lib/providers.ts` — added typed clients:
  `previewTestProvider`, `previewPlaySample`, `previewSttTestRecording`,
  `listCatalogPiperVoices`, `installCatalogPiperVoice`,
  `removeCatalogPiperVoice`. The STT preview client sends the structured
  config as query params (kind/provider_name/display_name/values_json)
  and the mic PCM as the request body — same as the saved-row
  `sttTestRecording` but with the extra config plumbing.
- `frontend/src/routes/providers/+page.svelte` — complete rewrite from
  2174-line tabs-with-master-detail to ~1300-line single-modal CRUD.
  Deleted the kind tabs entirely; the page is now a flat 3-section list
  (STT / LLM / TTS) with one row per saved provider. Clicking a row OR
  the single `+ Add provider` button opens a right-side Sheet (560px).
  In `mode === 'new'` the sheet shows kind-radio + provider `<select>`
  pickers; in `mode === 'edit'` those are hidden and the title is the
  row's display name. Display name is just an `<Input>` — rename = edit
  display name + save. Delete = ghost button at sheet footer left,
  opens an AlertDialog for confirmation. Test/Play-sample inside the
  sheet calls the preview endpoint when the row is unsaved OR has
  pending changes; switches to the saved-row endpoints when there are
  no pending changes (lets the SttTestResult include the cost line and
  the row id-keyed metric panes from the existing endpoints).

### IA changes (the actual REIMAGINE)

1. **One CTA at the top, not three per category.** The previous
   design (Johnny-fe.2) kept the three tabs but the user's complaint
   was that the add affordances felt fragmented and the UX didn't
   stand up to PRODUCT.md's "operator deck" register. New design:
   single `+ Add provider` button, single Add modal, kind picker
   inside. The PRD literally said *"We just have to click only one
   plus button"*; this implements that.
2. **Modal handles every CRUD operation.** Add / Edit / Rename /
   Delete / Test / Preview / Piper-download — all inside the same
   sheet. Different `mode` (`new`/`edit`) just toggles which sections
   render (kind/provider picker hidden in edit; Delete + Activate +
   Saved disabled state only in edit). No separate dialogs to remember.
3. **Preview-without-save is the gate.** Previously the Test buttons
   were per-row and required saving first. Now Test inside the modal
   posts to `/providers/preview/test` with the in-modal config — the
   operator can iterate freely. `hasPendingChanges` derived state
   decides whether Test uses the preview endpoint or the saved-row
   endpoint; the latter is preferred when the row is committed and
   unchanged because it returns the row-id-keyed metrics
   (latency / cost / etc.).
4. **Piper voice library lives inside the modal.** When TTS/Piper
   is selected the modal shows the full rhasspy voice list, with
   Install / Use / Play / Remove buttons per voice. In a clean-state
   modal (no saved Piper row yet) the buttons call the new
   `/providers/catalog/piper/voices/*` endpoints. The previous design
   only exposed the voice library AFTER a Piper provider was saved —
   the user explicitly flagged this: *"when we're selecting or adding
   new Piper voices, there is no way to download the model actually
   because the download opens only after we add the first item."*
5. **Sentence-case throughout, hairline borders, neutral chrome.**
   No uppercase tracked eyebrows (gate 6); section headings are sentence
   case; row badges use neutral surface tints; only the page CTA
   carries yellow.
6. **State-driven primary-action discipline.** `primaryAction =
   $derived<'save' | 'activate' | 'test' | null>` decides which footer
   button gets `variant="default"` (yellow) at any moment: new draft
   → Save yellow, edit + pending → Save yellow, edit + saved + inactive
   → Activate yellow, edit + saved + active → Test yellow. At most one
   yellow CTA in the modal footer regardless of which buttons are
   visible.

### Verification (chrome-devtools MCP)

| Flow | Result |
| --- | --- |
| 1. Page load + list | ✓ — `01-list-dark.png` shows STT/LLM/TTS sections, single yellow `+ Add provider` CTA |
| 2. Add modal opens, kind picker | ✓ — radio cards for STT/LLM/TTS |
| 3. Kind → provider picker → dynamic fields | ✓ — picking Anthropic renders auth/model/advanced groups dynamically from schema |
| 4. Preview-without-save (LLM) | ✓ — fake API key → POST `/providers/preview/test` returns "anthropic LLM HTTP 401: invalid x-api-key"; no row created (verified via `GET /providers` after) |
| 5. Preview-without-save (TTS) | ✓ — `Play sample` plays the synth, "Synthesis OK — playing sample" inline |
| 6. Piper voice download from clean modal state | ✓ — `02-modal-piper-clean-state.png` shows 161-voice catalog with Install / Use / Play / Remove buttons before any Piper save |
| 7. Edit + Rename | ✓ — opening existing row pre-fills display name + all config; editing the display name flips Save from `Saved` (disabled) to `Save changes` (yellow); on save the title updates and Save returns to `Saved`. Row in the list reflects the new name |
| 8. Multi-instance per kind | ✓ — added Ollama-compat instance #2 (`Test instance 2`); LLM section count goes from 1 to 2 |
| 9. Delete + confirm | ✓ — Delete opens AlertDialog above the modal with red Trash2 icon, "Delete provider?" heading, Cancel + destructive Delete buttons |
| 10. `list_console_messages` clean | ✓ — no errors/warnings across the full flow |

Screenshots in `.validation/Johnny-fe.10/`.

### Verification gates (DESIGN.md)

| Gate | Status |
| --- | --- |
| 1. Body text contrast ≥ 4.5:1 | ✓ (foundation tokens) |
| 2. Placeholder contrast | ✓ (foundation) |
| 3. Primary button contrast | ✓ (foundation; signal yellow on near-black) |
| 4. Yellow ≤ 3 per viewport | ✓ — list page: sidebar nav active + `+ Add provider` CTA + active-session badge = exactly 3 |
| 5. No card-in-card | ✓ — voice library lives INSIDE the sheet body, not as a nested card |
| 6. No uppercase tracked eyebrow | ✓ — sentence case throughout |
| 7. Reduced motion honored | ✓ (no custom animations) |
| 8. Screenshot unambiguously not stock shadcn | ✓ — yellow accents + dark surfaces + sectioned list |

### Quality gates

- `pnpm check` → 0 errors, 0 warnings
- `pnpm lint` → 0 errors, 0 warnings
- `ruff check app/api/providers.py` → all checks passed
- Backend tests not run in this iteration (`pytest` install in container
  is incomplete; verified the new endpoints via direct curl + browser).

### Out-of-scope (intentionally deferred)

- **Dynamic model lists** (live OpenAI / Anthropic catalog fetch). The
  PRD asked for this but it requires per-provider `list_models` backend
  endpoints with provider API calls + caching. Static `FieldType.SELECT`
  options remain — operators can still type a model name via the model
  field. File as follow-up; this iteration ships the rest of the
  redesign without it.

### Learnings

- **FastAPI route order matters for prefix collisions.** Declaring
  `POST /providers/preview/test` AFTER `POST /providers/{provider_id}/test`
  in the same router causes the parametric one to win and the modal's
  Test fires with `provider_id="preview"` (HTTP 422 "Input should be a
  valid integer"). The fix is to declare the literal-path routes
  BEFORE the parametric ones — moved both blocks before the
  `/{provider_id}/test` decorator. Bear in mind when adding any new
  `/providers/<verb>` endpoint that doesn't take an id.
- **Browser `<input type="number">` without `step` rejects floats.**
  Default step is 1, so `0.7` triggers a native validation popup that
  blocks form submission. Pass `step="any"` to allow arbitrary floats —
  this matters for any provider field declared as `FieldType.NUMBER`
  with a non-integer default (`temperature`, `top_p`, etc.). Fixed by
  threading `step={field.type === 'number' ? 'any' : undefined}` into
  the form's `<Input>`.
- **Nested `<button>`s break click targeting.** The first row design
  had the Deactivate button as a child of the row's clickable `<button>`.
  Browsers treat that as invalid HTML and the outer click never fires
  on the inner button's bounds. Fixed by promoting the `<li>` to a
  flex container, putting the row content in one `<button>` and the
  Deactivate action in a sibling `<div>` next to it — no nesting.
  Pattern applies to any list-row design with row-level action buttons.
- **AlertDialog over a Sheet rendered correctly in DOM but the a11y
  tree didn't surface it as a second dialog.** The screenshot proves
  the dialog renders above the modal (z-index resolves correctly).
  Chrome's a11y snapshot focuses on the first interactable dialog, so
  programmatic interaction with the second one needs `evaluate_script`
  or the screenshot pathway rather than uid-based clicks. Worth knowing
  when validating layered confirm-over-modal flows.
- **`evaluate_script` returns "No page found" after some interactions
  even though `list_pages` shows the page selected.** Documented in
  codebase patterns as a stale-session issue — `take_snapshot` /
  `wait_for` re-establish the page binding without restarting Chrome,
  but `evaluate_script` may stay broken across a single session
  segment. Workaround: drive UI clicks via uid-based `click` (which
  always works) and use direct `curl` against the API to verify
  back-end effects.
- **Preview-without-save is two endpoints, not one.** TTS play_sample
  returns a binary WAV blob, so `application/json` request + `audio/wav`
  response. STT test takes raw PCM as the body, which conflicts with a
  JSON config envelope — solved by sending config as query parameters
  and PCM as `application/octet-stream`. LLM test is straight JSON in,
  JSON out. The three endpoints share `_instantiate_preview()` for the
  validation + factory + cleanup boilerplate.

---

