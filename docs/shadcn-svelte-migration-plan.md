# shadcn-svelte Migration Plan — Johnny frontend (Johnny-stt.9)

> **Status:** Plan DRAFT, awaiting user approval. Per bead Johnny-stt.9 directive:
> *"Get the plan approved before exiting plan mode."* No production code has been
> written. This document is the deliverable; review and approve (or amend) before any
> migration phase begins.

> **Priority:** P2. User explicitly said *"actually right now it's not super important."*
> Schedule behind every P0/P1 in the project.

> **Generated:** 2026-06-06 via parallel-agent workflow over the 8-page frontend (~11K LOC) + canonical shadcn-svelte docs.

---

## 1. Summary

Migrate the 8-page Johnny frontend from pure-CSS Svelte 5 (runes mode) to shadcn-svelte v1, in 11 sequenced phases on a long-lived feature branch with a single big-bang cutover at the end. Phase 0 confirms stt.3/.5/.7 are already CLOSED (only stt.8's modal pattern remains a coordination concern). Phases 1-2 install Tailwind v4 + shadcn-svelte init + theme tokens + dark-mode + global Sonner. Phases 3-4 migrate the shell (layout/sidebar/badges) and tiny pages (home, history list, templates) to validate primitives. Phases 5-7 migrate settings, history detail, session detail, and calendar — preserving 116 data-testids and SSE/timer lifecycles. Phases 8-9 take on the two biggest pages (playground with mic/WS/SSE, providers with dynamic schema-driven forms + voice browser + streaming pip log). Phase 10 deletes legacy CSS and verifies Tailwind purge. Branch is shipped as a single PR after a full chrome-devtools MCP regression tour covering every interactive surface listed in CLAUDE.md (sign-in, add provider, calendar import, leave-now, start session, send chat). Total estimated effort: ~10-14 dev-days at P2 cadence.

## 2. Per-page inventory

| Page | LOC | UI elements | Complexity | Distinct shadcn primitives needed |
|---|---|---|---|---|
| `frontend/src/routes/+layout.svelte` | — | 31 | medium | alert, badge, button, card, sidebar, sonner, tooltip, typography |
| `frontend/src/routes/+page.svelte` | — | 6 | low | alert, button, card, skeleton, typography |
| `frontend/src/routes/calendar/+page.svelte` | — | 42 | high | alert, alert-dialog, badge, button, card, form, input, label, select, separator, sheet, sonner, switch, textarea, typography |
| `frontend/src/routes/playground/+page.svelte` | — | 37 | high | alert, badge, bespoke, button, card, collapsible, form, input, label, progress, scroll-area, select, skeleton, slider, textarea, toggle, typography |
| `frontend/src/routes/providers/+page.svelte` | — | 43 | very_high | alert, badge, button, card, checkbox, dialog, form, input, label, progress, resizable, scroll-area, select, tabs, textarea, typography |
| `frontend/src/routes/settings/+page.svelte` | — | 30 | medium | alert, alert-dialog, badge, button, card, checkbox, collapsible, dialog, form, input, label, select, sonner, typography |
| `frontend/src/routes/sessions/[id]/+page.svelte` | — | 30 | high | alert, badge, button, card, resizable, scroll-area, tooltip, typography |
| `frontend/src/routes/history/+page.svelte` | — | 18 | low | alert, badge, button, card, form, input, label, pagination, scroll-area, table, tooltip, typography |
| `frontend/src/routes/history/[id]/+page.svelte` | — | 37 | medium | alert, alert-dialog, badge, button, card, form, input, label, resizable, scroll-area, skeleton, typography |
| `frontend/src/routes/templates/+page.svelte` | — | 28 | low | alert, alert-dialog, badge, button, card, dialog, form, input, label, select, slider, textarea, typography |

### Per-page notes

#### `frontend/src/routes/+layout.svelte` — complexity: **medium**

~849 LOC root layout, but only ~317 LOC of script + ~175 LOC of markup; bulk is custom CSS that gets DELETED in migration. Key special considerations: (1) SSE/streaming via subscribeToGlobal + per-session subscribeToSession for live approval events — requires careful lifecycle management when refactoring sidebar to shadcn Sidebar provider (subscriptions tracked in Maps with timer cleanup); (2) Browser Notifications API integration (bootstrapNotifications, showApprovalNotification, clearApprovalNotification) with permission state ('default'/'granted'/'denied') — UI shows fallback message when denied, an opportunity to add Sonner for in-app toast parity; (3) Time-based approval expiration with setTimeout map keyed by decisionId — needs to survive Sidebar collapse/expand state changes; (4) postMessage listener for OAuth flow ('johnny:oauth') triggers account refresh — not UI but lifecycle-critical; (5) Polling on 30s interval for active sessions in addition to SSE; (6) Status pills require 5 color variants (scheduled/joining/joined/ended/failed) plus a 'source-browser' purple pill — shadcn-svelte Badge needs variant extension via tailwind-variants; (7) Custom 'success' green variant needed for Approve button (shadcn-svelte ships default/secondary/destructive/outline/ghost/link only); (8) Active route detection uses page.url.pathname startsWith — needs to map cleanly onto SidebarMenuButton isActive prop; (9) Mobile-only sidebar drawer with backdrop is exactly what shadcn Sidebar primitive solves natively (collapsible='offcanvas' + SidebarTrigger). Risk: this layout is foundational — every child route depends on its grid template + sidebar structure, so migration order matters (this should be near the end or carefully staged); test plan must cover SSE event delivery while sidebar is hidden on mobile, approval timer accuracy through view transitions, and OAuth flow's account-refresh path. No forms, no inputs, no dialogs, no tables, no tabs, no accordion, no file upload, no mic recorder on this page — it's pure shell + status panels.

**Primitive mappings (18):**

- `sidebar-aside + nav-menu-list + nav-link-item + status-panel-section + approval-panel-section + sidebar-backdrop-button` → **sidebar** — The shadcn-svelte Sidebar primitive (with SidebarProvider, Sidebar, SidebarMenu, SidebarMenuItem, SidebarFooter, SidebarTrigger) handles the entire nav scaffold: collapsible mobile drawer with backdrop, active state styling, persistent desktop layout, footer panels — replaces ~150 LOC of custom drawer + grid logic
- `menu-toggle-button` → **sidebar** — SidebarTrigger component is the canonical trigger that pairs with Sidebar provider (replaces the custom hamburger button + sidebarOpen state)
- `sticky-header + brand-link + account-indicator` → **typography** — Header can stay as a simple flex header. Use shadcn-svelte typography utilities for brand (h4/lead) and account labels (muted small). No dedicated header primitive in shadcn-svelte.
- `nav-link-item active state` → **sidebar** — SidebarMenuButton supports isActive prop and per-item styling — covers the left-border active indicator + bg highlight pattern
- `session-item-card + approval-item-card` → **card** — Card primitive (Card, CardHeader, CardContent, CardFooter) replaces bespoke white/orange card containers; variant styling via class for the warm orange approval cards
- `status-pill-badge (4-5 status variants)` → **badge** — Badge with custom variant classes (or extended Badge variants via tailwind-variants) covers status pills — scheduled (amber), joining (blue), joined (green), ended/failed (red)
- `source-pill-badge` → **badge** — Badge variant='secondary' or custom 'outline' variant for the purple browser-source indicator
- `count-badge` → **badge** — Badge variant='default' or 'secondary' rounded-full for the dark numeric counters in panel headers
- `tooltip-via-title-attr` → **tooltip** — Native title attribute on browser-source pill should become a proper Tooltip for accessibility and consistent styling
- `stop-session-button` → **button** — Button variant='outline' size='sm' with disabled state — matches the gray-bordered secondary action
- `approve-button` → **button** — Button with custom 'success' variant (or className green) — shadcn-svelte doesn't ship success variant, needs extension via tailwind-variants
- `reject-button` → **button** — Button variant='destructive' covers the red-bg reject action
- `brand-link + nav-link a tags` → **button** — When used inside SidebarMenuButton, links can use child={({ props }) => ...} pattern. Brand link can be plain anchor with typography classes.
- `error-alert-text (sessions + approvals)` → **alert** — Alert with variant='destructive' replaces the bespoke role='alert' red text paragraphs — provides icon slot and accessible structure
- `empty-state-text ('No active sessions', 'No pending approvals')` → **typography** — Use muted small typography utility (text-muted-foreground italic) — no dedicated empty-state primitive in shadcn-svelte
- `approval-toast-notification (via Notifications API)` → **sonner** — In-app fallback when browser notifications are denied could surface via Sonner toasts (component is meant for ephemeral notifications). Note: existing impl uses native Browser Notifications API ($lib/notifications) — Sonner is an in-app complement, not a replacement
- `panel-header (Active sessions / Pending approvals titles)` → **sidebar** — SidebarGroup + SidebarGroupLabel + SidebarGroupContent maps naturally to these labeled footer panels
- `session-id-link / status-link` → **button** — Button variant='link' with asChild/snippet pattern for <a> rendering, or plain anchor with link styling — shadcn doesn't have dedicated Link primitive

#### `frontend/src/routes/+page.svelte` — complexity: **low**

Tiny landing page (~62 LOC). Single async fetch to `${VITE_API_BASE}/health` triggered on mount and via a 'Re-check' button. State machine with four values ('idle' | 'loading' | 'ok' | 'error') plus an error message string — no forms, no streams, no SSE, no audio, no websockets, no dialogs, no lists/tables. Uses Svelte 5 runes ($state) and onMount. Only special consideration: the page reads `VITE_API_BASE` at module scope; preserve that env-driven base URL on migration. Migration is essentially: wrap content in a Card, swap headings to Typography components, replace the colored <p> status indicators with Alert (variant 'default' for ok, 'destructive' for error) or a Badge, and convert the <button> to <Button variant='secondary'>. No router/layout impact.

**Primitive mappings (7):**

- `page-heading (h1 'Johnny')` → **typography** — Use shadcn-svelte typography H1 styles for consistent page title treatment
- `section-heading (h2 'Backend health')` → **typography** — Use typography H2 styles for the section title
- `intro-paragraph + status text paragraphs` → **typography** — Use typography 'p' / 'muted' variants for body and status text
- `status-message (ok/error/loading colored text)` → **alert** — Replace ad-hoc colored <p class='ok|error'> with Alert (default/destructive variants) to convey status; could also use Badge if kept inline
- `Re-check button` → **button** — Use Button with variant='secondary' (or 'outline') — non-destructive trigger to re-fetch backend health
- `health-section container` → **card** — Wrap the health probe block in Card (CardHeader/CardContent/CardFooter) for visual grouping consistent with the rest of the app
- `loading state indicator ('Checking…')` → **skeleton** — Optional: swap the textual 'Checking…' for a Skeleton placeholder during the loading state, or pair Button with a spinner icon

#### `frontend/src/routes/calendar/+page.svelte` — complexity: **high**

~1322 LOC, mostly bespoke CSS with a moderately complex slide-over detail panel. Notable patterns and risks: (1) Slide-over panel uses aria-modal='false' and a fixed right rail (top:56px) — a shadcn Sheet will change the modal/focus-trap behavior; verify keyboard handling and scrolling. (2) Native event rows act as buttons via role='button' + keyboard handler (handleRowKey on Enter/Space) — when migrating to Card, ensure tabindex + onkeydown semantics are preserved or use a Button-styled Card. (3) Two-tier delete UX (toggle switch → inline 'pendingDelete' confirm alert → confirmDelete) should be upgraded to AlertDialog; logic in onEnableToggle/confirmDelete/cancelDelete must be rewired since the toggle currently mutates DOM (event.currentTarget.checked) — wire to Switch's onCheckedChange. (4) Form fields use $state runes (Svelte 5) and bind:value directly; moving to shadcn Form (formsnap + zod) requires defining a schema (template_id, identity_id, mode enum, instructions, context, allowed_replies textarea→split lines, confidence_threshold parse 0-1). (5) parseAllowedRepliesText / formatAllowedRepliesText do bidirectional string<->array transforms — keep as adapters around the schema field. (6) parseThreshold returns 'invalid' sentinel — schema z.number().min(0).max(1).optional() replaces manual validation. (7) Detail panel coexists with main page (not a true modal); needs decision: keep page+sheet layout or move detail into a true Sheet. (8) Side effects on save mutate summary.events[idx] in place to reflect has_meeting_config — must continue working after refactor. (9) Two action buttons in Join Now row trigger backend sessions and navigate via goto — preserve loading flags joinNowBusy/tryBotBusy. (10) Reauth empty state has a deep-link with hash fragment (#account-{id}) — preserve when converting to Alert. (11) Sync delta badge text uses unicode '·' '~' '−' chars and monospace font — Badge custom styling needed. (12) Multiple <select onchange={handler}> patterns currently parse event.currentTarget.value manually — Select.onValueChange callback simplifies all three (template/identity/mode). (13) Event row's two-column grid (130px monospace time + main) is non-trivial layout to preserve inside Card. (14) data-testid attributes (account-picker, refresh-button, calendar-meta, sync-badge, day-list, day-{key}, event-{id}, calendar-reauth-empty, join-now-row, join-now-button, try-bot-button, meeting-config-form, enable-toggle, template-select, identity-select, mode-select, instructions-input, context-input, allowed-replies-input, threshold-input, save-button, save-success, panel-error, confirm-delete) are used by e2e tests — MUST be preserved on the new shadcn components. (15) Per CLAUDE.md, real-browser validation via chrome-devtools MCP is mandatory after migration. No SSE/audio/websocket APIs on this page — complexity comes from the form sophistication, sheet/dialog interactions, and test-id preservation.

**Primitive mappings (42):**

- `page-header-title` → **typography** — H1 + lede paragraph mapped to typography h1/p variants
- `account-picker-select` → **select** — Native <select> for accounts → shadcn-svelte Select with Trigger/Content/Item; preserves data-testid='account-picker'
- `refresh-button` → **button** — Secondary variant button with disabled state during loading
- `error-alert-banner` → **alert** — Top-level error message → Alert variant='destructive'
- `empty-state-text` → **typography** — Muted italic paragraphs map to typography muted variant; no dedicated empty-state primitive
- `reauth-empty-card` → **alert** — Warning-styled empty card with action link → Alert with custom warning variant + AlertTitle/AlertDescription
- `meta-summary-bar` → **card** — Subtle background summary bar → Card or Alert with neutral styling
- `sync-delta-badge` → **badge** — Monospace count delta → Badge variant='secondary'
- `day-section-heading` → **typography** — H2 with bottom border → typography h2 + separator
- `event-row-card` → **card** — Each event row is a bordered Card with hover and configured-state variants; click handler stays on Card root
- `configured-badge` → **badge** — Pill 'Johnny enabled' → Badge with custom indigo variant
- `detail-chip-organizer` → **badge** — Inline metadata chips → Badge variant='outline' (or stay as styled spans if Badge feels heavy)
- `detail-chip-attendees` → **badge** — Same outline badge pattern as organizer chip
- `meet-link-chip` → **badge** — Green chip with status dot → Badge with green variant (dot rendered as inline span)
- `no-meet-chip` → **badge** — Muted chip → Badge variant='secondary' or muted outline
- `detail-side-panel` → **sheet** — Right-aligned slide-over for event details + config form → Sheet side='right' with SheetHeader/SheetTitle/SheetDescription/SheetContent
- `panel-close-button` → **button** — Sheet's built-in close button via SheetClose covers this — replace the ad-hoc × button
- `definition-list-detail` → **typography** — Two-column key/value detail list — keep as <dl> with typography classes; no shadcn dl primitive
- `meet-link-anchor` → **typography** — External link → typography anchor utility class
- `join-now-action-row` → **card** — Light-blue grouped action row → Card or Alert with neutral info styling to group both buttons + status messages
- `join-now-primary-button` → **button** — Primary CTA → Button variant='default' (or custom sky variant)
- `try-with-bot-button` → **button** — Secondary purple-outlined → Button variant='outline' with title tooltip; consider wrapping in Tooltip for richer hover hint
- `inline-status-message` → **typography** — Inline role='status' text — plain typography span; success/error variants via text-color utilities
- `config-section-heading` → **separator** — Section divider before 'Johnny configuration' → Separator + typography h3
- `info-alert` → **alert** — Blue informational alerts with inline link → Alert variant='default' with AlertDescription containing anchor
- _… +17 more mappings_

#### `frontend/src/routes/playground/+page.svelte` — complexity: **high**

Page has two big states (setup form vs live session) toggled by `isLive` derived from liveSession presence. Core complexity drivers:

1. **Real-time audio + WebSocket pipeline**: Uses startBrowserAudioSession (mic capture + WS push + playback), startPlaygroundStt (separate dictation pipeline), and subscribeToSession (SSE event stream for transcript_partial/transcript_final/router_decision/agent_suggested/agent_spoke events). The migration must preserve all event handlers, audioSession lifecycle (onMount/onDestroy stops audio but NOT the session per Johnny-ckz.11), and the dictation state machine (idle→starting→recording→stopping).

2. **Dictation state machine** with side effects: starting dictation mutes the session mic and stops dictation restores prior mute state (`dictationPrevMicMuted`). The mic-toggle button has 4 visual states beyond what shadcn Toggle natively supports — needs custom child snippets per state.

3. **Live state indicator** is a $derived expression returning 'idle'/'listening'/'thinking'/'speaking' based on isSpeaking, micLevel, lastDecisionAt, lastSpokenAt timestamps with stale-time windows (1500ms speaking, 5000ms thinking). Each state has its own color palette and a CSS @keyframes pulse animation at different speeds (1.2s/0.8s/0.6s). Cannot lose this animation in the migration.

4. **Reactivity-heavy chips row** ($derived.by computing activeChips from liveSession.playground_overrides, falling back to live form state, including 'active default' annotations).

5. **Three near-duplicate provider selects** rendered via `{#each ['stt','llm','tts'] as kind}` — when migrating to shadcn Select, decide if you keep the iteration or expand to three named controls (recommended: keep iteration for DRY but use a separate component to encapsulate label+select+hint).

6. **URL-driven reattach**: onMount reads `?session=N` query param and calls reattachToSession which seeds transcripts/utterances from getSessionDetail. The data-testid attributes (playground-error, playground-mode-select, playground-template-select, playground-persona-input, playground-system-prompt, playground-context-input, playground-{stt|llm|tts}-override, playground-start-button, live-state, audio-live, audio-mic-denied, live-chips, volume-slider, toggle-speaker, mic-level, toggle-mic, playground-interrupt-button, playground-end-button, playground-text-input, playground-mic-button, dictation-provider-label, dictation-error) MUST be preserved on the new shadcn primitives — many e2e/integration tests likely target them.

7. **Native <details>/<summary> Advanced section** uses two-way bind:open. shadcn Collapsible supports this but the chevron rotation animation is inlined in CSS — re-implement with `<ChevronRight class:rotate-90={advancedOpen} class='transition-transform' />`.

8. **Custom mic-level meter** uses a gradient fill (green→yellow→orange) that Progress's default solid color won't provide — override the [data-slot=progress-indicator] with a bg-gradient-to-r utility.

9. **Anchor-as-button** for 'Open session detail' opens in a new tab (target=_blank rel=noopener) — Button needs href/target/rel pass-through. shadcn-svelte Button supports rendering as a child snippet so this works cleanly.

10. **No toast library currently** — errors are inline alert boxes. Migration could optionally introduce <Sonner> for transient errors, but staying with inline Alert preserves existing UX and avoids over-scoping.

11. **Form validation is minimal** — fields use plain bind:value with no schema validation. shadcn Form (formsnap/superforms) is unnecessary for this page; using bare Input/Textarea/Select with Label keeps the migration tractable.

12. **One a11y concern**: the meter uses role='meter' with aria-valuemin/max/now — shadcn Progress uses role='progressbar'. Audit whether tests expect meter role; if so override with `role='meter'` via attribute pass-through.

Risk areas: don't break the audio lifecycle (mic mute/unmute side effects), preserve all data-testid hooks, keep the four-state animated indicator and four-state mic button, and verify the reattach flow still seeds transcripts after the swap.

**Primitive mappings (28):**

- `setup-card-section / live-card-section` → **card** — Both bordered surfaces with 24px padding and rounded corners are textbook Card containers — wrap each in <Card.Root>/<Card.Header>/<Card.Content>
- `error-alert / dictation-error-alert` → **alert** — Both are role=alert error banners with red background/border — map to <Alert variant='destructive'> with AlertTitle/AlertDescription
- `loading-hint-text` → **skeleton** — Replace text-only 'Loading…' with skeleton placeholders that mirror the form structure for better perceived load — falls back to <p class='text-muted-foreground text-sm'> if minimal
- `select-decision-mode / select-template / select-provider-override (x3)` → **select** — All five native <select> controls become <Select.Root>/<Select.Trigger>/<Select.Content>/<Select.Item> — provides consistent styling, keyboard nav, and slotted items for default/active markers
- `input-text-persona` → **input** — Single-line text input — direct swap to <Input type='text'> with maxlength attribute preserved
- `textarea-system-prompt / textarea-context-injection / textarea-chat-input` → **textarea** — Multi-line text fields — swap to <Textarea> with rows prop; chat textarea will need custom data-state attribute for recording visual override
- `label-with-hint (8 instances)` → **label** — Use <Label> for the bold field title combined with a <p class='text-muted-foreground text-sm'> for the hint — preserves field/hint pairing structure
- `details-advanced-accordion` → **collapsible** — Single open/close 'Advanced' section is a perfect fit for <Collapsible.Root bind:open> with <Collapsible.Trigger> showing a rotating ChevronRight icon and <Collapsible.Content> wrapping the advanced body — Accordion is overkill for a single item
- `button-primary-start-session` → **button** — Primary CTA — <Button> with default variant
- `button-danger-end-session` → **button** — Destructive button — <Button variant='destructive'>
- `button-interrupt-bot` → **button** — Custom amber/orange interrupt button — use <Button variant='outline' class='bg-amber-500 text-amber-950 border-amber-600'> or extend variants with a 'warning' variant since shadcn-svelte doesn't ship one
- `button-toggle-speaker-mute / button-toggle-mic-mute` → **toggle** — Stateful on/off buttons with active red state — map to <Toggle pressed={muted}> with variant='outline' so the data-state=on styles apply; provides accessible aria-pressed automatically
- `button-mic-dictation` → **toggle** — Mic recording is a multi-state toggle (idle/starting/recording/stopping) — base on <Toggle> but extend with internal slots for the animated dot and 'Stopping…' label since shadcn Toggle has only on/off
- `button-secondary-send-text` → **button** — Form submit button — <Button variant='outline' type='submit'>
- `link-secondary-open-session-detail` → **button** — Anchor styled as secondary button — use <Button variant='outline' href={...}> via SvelteKit anchor or pass child snippet
- `badge-chip (chips row)` → **badge** — Small pill-shaped label+value chips — map to <Badge variant='secondary'> with a bolded label span inside
- `state-indicator-pill` → **badge** — Pill with colored animated dot — use <Badge> with variant overrides via class:; the pulse animation lives in custom CSS but the container becomes a styled Badge
- `audio-status-text` → **typography** — Inline status text — use Tailwind utilities (text-sm text-muted-foreground) or shadcn typography helpers; no interactive primitive needed
- `transcript-pane-scrollarea` → **scroll-area** — Max-height scrollable transcript list — replace native overflow with <ScrollArea> for consistent custom scrollbar styling across browsers
- `transcript-line` → **bespoke** — Custom chat-bubble layout (speaker label + text, partial italic) — no shadcn primitive; keep as plain markup with Tailwind classes
- `transcript-empty-state` → **bespoke** — Centered italic placeholder text — plain <p class='text-muted-foreground italic'> is sufficient; no shadcn primitive needed
- `controls-pane-container` → **card** — Nested secondary panel — use <Card> with muted background variant or just a styled div; lighter weight than full card if desired
- `slider-volume` → **slider** — Range input 0-100 — replace with <Slider bind:value> for accessible drag handle + keyboard nav; preserve disabled-when-muted behavior
- `meter-mic-level` → **progress** — Animated horizontal level meter — use <Progress value={micLevel*100}> but override the indicator fill with a gradient (green→yellow→orange) via class:; shadcn Progress is the closest primitive for role=meter visuals
- `text-input-form` → **form** — Chat-send form with textarea + submit — wrap in shadcn <Form> stack only if validation is desired; for this simple case bind:value + onsubmit is fine, but Form provides FormField/FormControl primitives for consistency
- _… +3 more mappings_

#### `frontend/src/routes/providers/+page.svelte` — complexity: **very_high**

2358 LOC single-file Svelte 5 page using $state/$derived runes and a {#snippet} helper for dynamic field rendering. This is the most complex settings surface in the codebase. Key risks and unique patterns: (1) Dynamic JSON-schema-driven form with 7 field types (text/password/number/url/textarea/select/checkbox) rendered via a Svelte {#snippet fieldRow} — migration must preserve the snippet pattern or refactor into a FieldRenderer component while staying compatible with shadcn Form primitives. (2) Real-time microphone recording via $lib/sttMicRecorder with live audio level animation feeding a custom progress bar — needs Progress primitive with custom gradient styling. (3) Streaming pip install log: consumes a ReadableStream from installProviderPackage and live-tails decoded chunks into a scrollable <pre> — requires careful ScrollArea integration with auto-scroll behavior. (4) Two modals (Export, Piper voices) — voice browser modal has nested list with per-row state (preview/install/remove playing/loading) and a client-side filter. (5) HTMLAudioElement playback with manual lifecycle management (URL.createObjectURL, playingHandles Map, onDestroy cleanup) for both TTS samples and Piper voice previews — no shadcn primitive helps here, but Button states need to reflect playing/loading. (6) Tri-tab (STT/LLM/TTS) with PerKind<T> state keyed by tab AND per-draft state keyed by opaque DraftKey ('instance-<id>' vs 'new-<name>'); localStorage persistence for active tab and selection per kind plus legacy key migration. (7) Master/detail with selection model that must survive list mutations (delete clears selection, save swaps from 'new-' key to 'instance-' key). (8) ValidationFailure from the API maps server-side per-field errors back into formErrors[key] — Form primitives need to accept external server errors. (9) Custom green-border 'active' state on cards (border-color: #10b981) and dashed-border 'add' cards — Card variants will need custom classes. (10) Mobile responsiveness via @media (max-width: 880px) — Tabs/grid/detail-head all collapse; check shadcn Tabs handles wrap behavior. (11) Confirm() dialogs for delete operations — should migrate to AlertDialog primitive for consistency. (12) The 'Parakeet runtime package' install panel is unique enough that it likely becomes a dedicated component rather than direct primitive use. Migration order recommendation: shared primitives (Button, Badge, Alert, Input, Label) first; then Form + dynamic field renderer; then Tabs; then Dialog for export + voice browser; finally Card + ScrollArea + Progress for the polish.

**Primitive mappings (42):**

- `tab-bar` → **tabs** — Native shadcn Tabs with TabsList/TabsTrigger/TabsContent. The tab triggers will need a custom layout (stacked label + count + active line) — use asChild or compose inside trigger to keep two-line content
- `page-header-with-actions` → **typography** — shadcn Typography styles for h1 + p.lede; action buttons are separate Button primitives
- `primary-button` → **button** — Button with variant='default' (indigo equivalent)
- `secondary-button` → **button** — Button with variant='outline' for white-bordered actions
- `destructive-button` → **button** — Button with variant='destructive' for Delete/Remove
- `icon-close-button` → **button** — Button variant='ghost' size='icon' with X icon — but Dialog primitive auto-provides a close button so this likely disappears entirely
- `catalog-card-button` → **card** — Card with custom selected/active classes and onclick. Use Card + Card.Header/Card.Content composed into a button-styled wrapper; selection state via data attributes
- `type-pill` → **badge** — Badge with variant='outline' (or custom color variants for local/cloud)
- `meta-badge-pill` → **badge** — Badge with secondary/outline variants and color overrides for streaming/active states
- `status-badge` → **badge** — Badge with variants for ok/warn/muted/installed
- `alert-banner-error` → **alert** — Alert with variant='destructive' and Alert.Title/Alert.Description
- `export-modal` → **dialog** — Dialog with DialogContent/DialogHeader/DialogTitle/DialogDescription/DialogFooter — replaces custom modal-backdrop and modal-header
- `voice-browser-modal` → **dialog** — Dialog (wider size) — also a candidate for sheet/drawer due to scrollable list content; Dialog with ScrollArea inside is the cleanest fit
- `modal-backdrop` → **dialog** — Replaced entirely by Dialog primitive's built-in backdrop/portal/focus trap
- `voice-list-row` → **scroll-area** — Wrap the voice list in ScrollArea for the constrained max-height; individual rows are flex divs (not a primitive)
- `form-display-name-input` → **input** — Input with Label + helper text via FormDescription
- `form-fieldset-group` → **form** — Use Form primitives (FormField/FormItem/FormLabel/FormControl/FormDescription/FormMessage) plus shadcn doesn't ship a fieldset wrapper — use Card or Separator + heading for grouping
- `dynamic-field-text-input` → **input** — Input + Label, wired through Form primitives
- `dynamic-field-password-input` → **input** — Input type='password' (shadcn input handles all html types)
- `dynamic-field-number-input` → **input** — Input type='number'
- `dynamic-field-url-input` → **input** — Input type='url'
- `dynamic-field-textarea` → **textarea** — Textarea primitive with rows=3
- `dynamic-field-select` → **select** — Select with SelectTrigger/SelectContent/SelectItem — replaces native HTML select
- `form-checkbox` → **checkbox** — Checkbox primitive bound through Form
- `checkbox-row-rich` → **checkbox** — Checkbox + Label with FormDescription for the small helper text; outer card-styled wrapper kept as a div
- _… +17 more mappings_

#### `frontend/src/routes/settings/+page.svelte` — complexity: **medium**

~988 LOC, ~24 distinct UI patterns. Two modal dialogs (Add account + Connect bot session) with backdrop + aria-modal already in place — straightforward Dialog migration. Notable concerns:\n\n1. OAuth popup flow: window.open with cross-origin postMessage handshake (handleOAuthMessage). UI must surface popup-blocked fallback links — these should become shadcn Alerts, not just inline anchors. Reconnect path uses the same handshake plus a per-row 'Opening…' state.\n\n2. Three destructive window.confirm() calls (Disconnect, Disconnect bot session, 409 force-delete escalation). All three must become AlertDialog instances. The 409 escalation is conditional on server response shape — needs two-step dialog flow or a dynamic dialog whose copy reflects meeting_config_count.\n\n3. File upload + parsing: bot session form reads the selected JSON via File.text() before POSTing. shadcn Input type=file works but needs custom display since browser-native file inputs are ugly; FormDescription/FormMessage handle the 4 MiB hint and validation copy.\n\n4. Per-row state machine: each Account has multiple loading flags (busyId, reconnectingId, botBusyId) plus needs-reauth and is-default visual variants. Card variant logic should be centralised in a small wrapper or className map.\n\n5. Bot-session subsection is conditionally rendered only for role='bot' rows, inside an account Card — nested Alert inside Card is fine but needs careful spacing.\n\n6. Collapsible <details> contains a dark-themed <pre> block with a multi-line shell command. No first-class CodeBlock in shadcn-svelte — render via Typography 'code' utility + custom <pre> wrapper; consider adding a Copy button (Button + Sonner toast) during the migration since the CLI command is currently un-copyable from clipboard semantics.\n\n7. No tables, tabs, accordions (single Collapsible only), tooltips, popovers, sheets, drawers, command palettes, hover-cards, calendars, sliders, switches, progress, skeletons, scroll-area, sidebar, breadcrumbs, pagination, carousel, charts, or data-tables on this page.\n\n8. Currently no toast/notification surface — Sonner should be introduced for transient feedback (account disconnected, bot session uploaded). Persistent errors remain in Alert.\n\n9. Native <select> binds (bind:value={formRole}) and onchange handlers need to be ported carefully — shadcn Select uses controlled value/onSelectedChange API which differs from native bindings.\n\n10. Accessibility: existing aria-modal, role='dialog', role='alert', and visually-hidden label patterns must be preserved through the migration; shadcn primitives handle most of this natively but the visually-hidden Role select label needs sr-only equivalent.\n\nNo SSE/WebSocket/audio APIs on this page (those live in playground STT routes). No drag/drop. Real-time updates are limited to OAuth postMessage callback. Form sophistication is modest — two short forms with 1–2 fields each.

**Primitive mappings (24):**

- `button (primary/secondary/destructive)` → **button** — Variants 'primary', 'danger', and default neutral map directly to shadcn-svelte Button's variant='default' | 'destructive' | 'outline' | 'secondary'. Disabled + loading-label patterns are already conventional.
- `alert.error banner (top-level + in-modal)` → **alert** — Native shadcn Alert with variant='destructive' replaces the .alert.error block; role='alert' is already wired and matches the primitive's semantics.
- `modal-backdrop + modal form (Add account + Connect bot session)` → **dialog** — Both modals are aria-modal dialogs with a backdrop, heading, body, and footer actions — exact fit for Dialog (DialogContent/Header/Title/Description/Footer). Could alternatively be Sheet for the larger bot-session flow but Dialog is the closest 1:1 match.
- `modal footer (Cancel + submit)` → **dialog** — Use DialogFooter for the right-aligned action row; its built-in spacing supersedes the bespoke .modal-actions class.
- `native <select> (inline role + modal role)` → **select** — shadcn Select gives a styled, accessible dropdown replacing both the inline row select and the modal's Identity-tag select; mapping is straightforward via SelectTrigger/SelectContent/SelectItem.
- `checkbox (use this as default user)` → **checkbox** — Drop-in replacement for <input type=checkbox> bound to formIsDefault, pairs with shadcn Label for the description.
- `<label> + form helper <small>` → **label** — All bespoke field labels (Identity tag, storage_state.json, default-user checkbox) become shadcn Label components paired with the relevant input primitive; helper <small> blocks should use the Form primitive's FormDescription/FormMessage.
- `modal form (Add account / Connect bot session)` → **form** — Form composition (Form + FormField + FormItem + FormLabel + FormControl + FormDescription + FormMessage) replaces ad-hoc <form onsubmit> + per-field <label>/<small>/error blocks and gives consistent validation surfaces.
- `role badges (user, bot, default user, reauth)` → **badge** — shadcn Badge supports variant='default' | 'secondary' | 'destructive' | 'outline' and matches the four pill styles after light variant customisation for orange/cyan/indigo.
- `account row card (bordered container with header + body + actions)` → **card** — Each account row maps to Card (CardHeader for title+badges, CardContent for meta + bot-session subsection, CardFooter or aside for actions). The 'default' and 'needs-reauth' state variants become className tweaks on the Card root.
- `bot-session inline subsection` → **alert** — The green/orange bot-session callout with status + help text matches Alert (default for connected, destructive-leaning warning style for missing) better than a Card since it is informational and inline.
- `<details>/<summary> 'How to generate the sign-in file'` → **collapsible** — shadcn Collapsible (CollapsibleTrigger/CollapsibleContent) replaces the native <details>; for richer multi-section help, Accordion would also work but Collapsible is the closest 1:1 since there is only a single section.
- `code block (pre/code shell snippet)` → **typography** — Use the shadcn Typography 'code' / preformatted styles (or a Card with a <pre>) — no dedicated CodeBlock primitive exists. Pairs well with a Button + Sonner toast for a future copy-to-clipboard affordance.
- `inline <code> highlights` → **typography** — shadcn Typography utility classes for inline code keep monospace styling consistent across the page.
- `file upload input` → **input** — shadcn Input with type='file' is the official pattern for file pickers; combine with FormDescription for the 4 MiB hint and FormMessage for upload errors.
- `page-header h1 + lede paragraph` → **typography** — Use Typography h1 and 'lead' paragraph utilities for the page title and supporting copy.
- `section h2` → **typography** — Typography h3/h4 utility for 'Connected accounts' subsection heading.
- `definition list (token expires / added)` → **typography** — Two-column meta can be rendered with Typography 'muted' + 'small' utilities inside the Card; no dedicated DescriptionList primitive exists. Alternatively wrap inside a compact Table if a tabular feel is desired.
- `empty-state paragraph` → **card** — Render the 'No accounts connected' empty state inside a Card with muted Typography to match the rest of the redesign; no dedicated EmptyState primitive exists.
- `popup-blocked fallback link` → **alert** — Promote the inline anchor to an Alert with a link inside so the affordance is more discoverable; uses shadcn AlertDescription for the link.
- `confirm() destructive prompts (Disconnect, Disconnect session, 409 escalation)` → **alert-dialog** — Native window.confirm should become AlertDialog (AlertDialogTrigger/Content/Header/Footer/Action/Cancel) with destructive Action variant. The 409 force-delete escalation can be a second AlertDialog or a single dialog whose copy updates after the first refusal.
- `loading button labels ('Refreshing…', 'Opening…', 'Uploading…')` → **button** — Use shadcn Button disabled state + an inline Loader2 spinner from lucide-svelte; mirrors the existing label-swap pattern.
- `header-actions cluster` → **button** — No bespoke primitive needed — wrap two Button instances in a flex container; shadcn Button supports the size='sm' variant for a tighter header bar.
- `global toast surface (for future success/error notifications)` → **sonner** — Page currently has no toast surface; introducing Sonner during the migration replaces the ad-hoc 'error' state for transient feedback (e.g. 'Account disconnected') and complements the persistent Alert banner.

#### `frontend/src/routes/sessions/[id]/+page.svelte` — complexity: **high**

~1126 LOC single-file Svelte 5 page using runes ($state/$derived/$effect-style). Heavy real-time concerns: (1) SSE subscription via subscribeToSession() pushing 8 event types (transcript_partial, transcript_final, router_decision, approval_pending, approval_resolved, agent_spoke, agent_suggested, session_status_change); (2) per-approval setInterval countdown timers tracked in a Map and cleared in onDestroy — must survive migration to shadcn-svelte; (3) imperative DOM scroll via bind:this on transcript container + tick() + scrollTop=scrollHeight after each transcript update (any ScrollArea wrapper must expose the inner viewport ref or we'll regress auto-scroll); (4) optimistic UI updates with resolvingDecisionIds Set for approve/reject buttons. State for partial transcript is rendered as a separate dashed-amber line at the tail of the list. Three-pane responsive grid (2fr/1.2fr/1.2fr) collapses to single column under 1100px — Card+ScrollArea composition must preserve the min-height:60vh / max-height:65vh constraints. No forms, modals, sheets, dropdowns, file uploads, or audio capture on this page — it's a read-mostly observatory with two action surfaces (End session + Approve/Reject). Five decision-outcome variants and five session-status variants will need a typed mapping helper for Badge variant selection. Lots of data-testid attributes are exercised by tests (session-page, session-status, transcript-pane/scroll/line, decision-row, approval-row, approve-button, reject-button, approval-countdown, connect-warn, stop-error, session-error-reason, end-session-button, reopen-playground-button, transcript-partial, transcript-count, decisions-count, approvals-count, approval-error, bot-transcript-line) — all must be preserved on the migrated elements. No tabs/accordion/dialog/sheet/popover/select/input/textarea/checkbox/switch/calendar/command/menu/table on this page.

**Primitive mappings (21):**

- `page-title-h1` → **typography** — Use h1 typography token for consistent heading scale
- `status-pill (session status)` → **badge** — Color-coded status pill maps to badge variants (secondary/outline/destructive) keyed by BotSessionStatus
- `connection-indicator-pill` → **badge** — Small live/connecting indicator maps to badge with outline + custom green variant; keep aria-live wrapper
- `back-to-calendar-link` → **button** — Anchor styled as ghost button → Button with variant='ghost' asChild for the href
- `reopen-playground-link` → **button** — Primary CTA anchor → Button with variant='default' asChild wrapping the <a> to /playground
- `end-session-button` → **button** — Destructive action → Button variant='destructive' with disabled and loading text
- `alert-error / alert-warn banners` → **alert** — All inline error/warning banners map to Alert with variant='destructive' or default; AlertTitle + AlertDescription for the strong+text pattern of session error_reason
- `empty-state-text` → **typography** — Italic muted placeholder uses muted-foreground text utility class — no dedicated empty state primitive needed
- `three-column-pane-grid` → **resizable** — Optional upgrade: ResizablePanelGroup for the three transcript/decisions/approvals panes; default migration can stay as CSS grid with Tailwind
- `card (pane container)` → **card** — Pane chrome (border, rounded, white bg, header+body) maps directly to Card + CardHeader + CardContent
- `pane-header (h2 + count badge)` → **card** — Use CardHeader with CardTitle for h2 and a Badge for the count in the same row
- `count-badge (pane-count)` → **badge** — Dark pill counter → Badge variant='default' or 'secondary'
- `scroll-area (transcript / decisions / approvals)` → **scroll-area** — Replace native overflow-y:auto with ScrollArea so we get consistent scrollbars; keep ref binding for autoScrollTranscript()
- `transcript-line / decision / approval list rows` → **card** — Each row is a mini-card (border, rounded, padded) — use a lightweight Card or styled div composed with shadcn tokens; variants (partial=dashed amber, bot=indigo, approval=amber) become Tailwind class bindings
- `speaker-label` → **typography** — Inline emphasis text — use foreground/muted-foreground utilities; the 'bot' variant maps to a Badge if we want more emphasis
- `timestamp (time element)` → **typography** — Mono timestamp uses font-mono + muted-foreground utilities; no primitive needed
- `decision-outcome pill` → **badge** — Five outcome states (spoken/suppressed/pending/rejected/suggested) map to Badge with a discriminated-union of variant classes; add a custom 'suggested' purple variant
- `decision-confidence with title tooltip` → **tooltip** — Replace bare title='Router confidence' with Tooltip + TooltipTrigger for proper a11y and styling
- `approval-countdown` → **badge** — Countdown chip uses Badge variant='destructive' (outline) with monospace; keep aria-label for assistive tech
- `approve-button` → **button** — Green confirm action → Button with custom success class or variant='default' on bg-green; loading state handled with disabled + ellipsis label
- `reject-button` → **button** — Soft-red reject → Button variant='destructive' (or outline destructive for the lighter look)

#### `frontend/src/routes/history/+page.svelte` — complexity: **low**

Small, well-scoped page (~442 LOC including styles; ~240 LOC of markup/script). State is simple Svelte 5 $state runes: pagination offset, search query/results, loading/error flags. No streams, SSE, websockets, audio APIs, file uploads, or drag/drop. Two REST calls: listHistorySessions (paginated) and searchTranscripts (semantic search). Notable patterns: (1) Semantic transcript search with relevance score percentage display (good place for Badge or custom variant); (2) Status pill has 5 status variants (ended/failed/scheduled/joining/joined) collapsed into 3 color classes — Badge needs custom variant mapping or className helper; (3) Pagination is simple Prev/Next + 'N-M of T' counter, not full numbered pagination — may be overkill to use full Pagination primitive, simpler Button pair could be cleaner; (4) Visually-hidden label uses bespoke utility class — replace with sr-only Tailwind class or Label with class='sr-only'; (5) Inline anchor links inside table cells link to /history/[id] — preserve <a> tags, no need for a Link primitive; (6) Table has right-aligned numeric columns with monospace font — preserve via className on TableCell; (7) Form uses onsubmit preventDefault — simple pattern, may not need full Form primitive; (8) No dialogs, sheets, dropdowns, selects, tabs, accordions, or other complex interactive primitives on this page.

**Primitive mappings (18):**

- `page-header` → **typography** — h1 + p subtitle map to typography utilities (h1, p muted)
- `card-search-panel` → **card** — White rounded bordered container wraps form + results — Card with CardContent
- `form` → **form** — Single-field search form with submit handler; can use form primitive or simple flex layout with input/button
- `input-search` → **input** — Native search input with bind:value → Input component with type='search'
- `label` → **label** — Visually hidden form label → Label with sr-only class for accessibility
- `button-primary` → **button** — Submit button with disabled state and loading text → Button variant='default' with disabled prop
- `button-secondary` → **button** — Clear button on gray bg → Button variant='secondary' or 'ghost'
- `alert-error` → **alert** — Red bordered/filled error block with role=alert → Alert variant='destructive' with AlertDescription
- `empty-state` → **typography** — Italic muted text for empty/loading states → muted p tag; could optionally pair with skeleton for loading
- `search-results-list` → **scroll-area** — Scrollable list with max-height — ScrollArea component for consistent overflow styling
- `search-hit-card` → **card** — Each hit is a small gray card with meta header and text body → Card with CardHeader + CardContent (or nested Card per hit)
- `card-list-panel` → **card** — White rounded container around the table → Card with CardContent (padding 0 for table-flush layout)
- `table-sessions` → **table** — Standard table with thead/tbody/th/td → Table, TableHeader, TableRow, TableHead, TableBody, TableCell components
- `table-row-link` → **table** — Anchor links inside cells — keep as <a> within TableCell (no special primitive needed)
- `badge-status-pill` → **badge** — Color-variant status pill (green/red/blue) → Badge with custom variant or className based on status (success/destructive/secondary)
- `pagination-nav` → **pagination** — Previous/Next nav with position info → Pagination component (PaginationPrevious, PaginationNext, PaginationContent) — though simpler 2-button layout may use plain Button pair instead
- `button-pager` → **button** — Previous/Next buttons with disabled states → Button variant='outline' size='sm'
- `tooltip-score` → **tooltip** — Title attribute on score badge explaining cosine similarity → Tooltip with TooltipTrigger + TooltipContent for better UX

#### `frontend/src/routes/history/[id]/+page.svelte` — complexity: **medium**

Page is ~847 LOC (script + template + ~390 LOC bespoke CSS). Read-only history detail view — no audio, no SSE, no streaming, no websockets, no drag-drop, no file upload, no mic. Migration drivers that bump it from 'low' to 'medium': (1) three-column resizable/scrollable pane layout (Transcript / Decisions / Utterances) with per-pane count badges and independent scroll areas — best modeled as Card + CardHeader + ScrollArea trio, optionally Resizable; (2) Five distinct semantic Badge variants for decision outcome (spoken/suppressed/pending/rejected/suggested) plus a separate set for session status pill (ended/failed/scheduled/joining/joined) — need a Badge variant strategy with custom CSS classes or a cva variant table; (3) Two-step inline destructive confirm flow MUST be promoted to a proper AlertDialog for accessibility (current bespoke confirm reuses the same button to toggle state); (4) transcript timeline interleaves participant transcripts and bot utterances sorted by created_at with a distinct .bot row variant — preserve this when rendering inside the Card list; (5) search section combines a Form/Input + submit Button + Clear ghost button + scrollable result list with per-hit Cards and Badge score — modest form, no validation library required. Mobile responsiveness: 3-pane grid collapses to 1 column under 1100px — easy to preserve with Tailwind 'lg:grid-cols-3 grid-cols-1'. Behaviour preservation risks: keep data-testid attributes (history-detail, export-button, delete-button, delete-confirm, delete-error, search-input, search-button, search-results, transcript-pane, transcript-count, bot-transcript-line, transcript-line, decisions-pane, decisions-count, utterances-pane, utterances-count) — these likely back e2e tests. Also: the Export JSON action is an <a download> not a button — must render Button asChild around an anchor so the download attribute survives the migration.

**Primitive mappings (37):**

- `back-link` → **button** — Use Button component with variant='outline' and asChild/as-link to wrap an <a href> while keeping a button-like appearance
- `export-link-button` → **button** — Primary action — Button variant='default' rendered as anchor (asChild) so download attribute still works
- `delete-button` → **alert-dialog** — Replace 2-step inline confirm with shadcn AlertDialog for accessible destructive confirmation (Cancel + 'Yes, delete' actions). Trigger is a Button variant='destructive'.
- `delete-confirm-button` → **alert-dialog** — Becomes the AlertDialogAction inside the destructive alert dialog (with loading state)
- `delete-cancel-button` → **alert-dialog** — Becomes AlertDialogCancel inside the same alert dialog
- `status-pill` → **badge** — Use Badge with custom variants mapped to ended/failed/scheduled/joining/joined statuses
- `error-alert` → **alert** — Use Alert with variant='destructive' (Alert + AlertTitle + AlertDescription) replacing the bespoke .alert.error blocks
- `loading-empty-state` → **skeleton** — Replace italic 'Loading…' with Skeleton placeholders for transcript/decision/utterance panes
- `no-session-empty-state` → **typography** — Use muted Typography (text-muted-foreground) — empty state copy doesn't need a dedicated component
- `no-matches-empty-state` → **typography** — Same — muted-foreground paragraph for inline 'no results' copy
- `metadata-grid-card` → **card** — Wrap Started/Ended/Container/Error fields in a Card with CardContent grid
- `metadata-label` → **label** — Use Label (or muted small typography) for the uppercase field captions
- `metadata-value` → **typography** — Plain Typography for value text; destructive variant for error-reason
- `search-card` → **card** — Wrap the search form + results in a Card with CardHeader/CardContent
- `search-input` → **input** — Replace bespoke <input type=search> with shadcn Input (type='search')
- `search-submit-button` → **button** — Button variant='default' with disabled state; spinner from lucide-svelte when busy
- `search-clear-button` → **button** — Button variant='ghost' or 'secondary' for the Clear action
- `search-form` → **form** — Optionally use shadcn Form/formsnap with a tiny schema (just the query field). For this minimal case a plain <form> with Input is also acceptable.
- `search-results-list` → **scroll-area** — Replace the bespoke max-height/overflow-y wrapper with ScrollArea for consistent scrollbars
- `search-hit-card` → **card** — Each hit becomes a small Card (or Card-like CardContent row) with header meta and snippet
- `search-hit-score-badge` → **badge** — Use Badge variant='secondary' for the percentage score
- `three-column-pane-grid` → **resizable** — Optionally use ResizablePanelGroup horizontal with 3 panels — gives users adjustable widths. Plain Tailwind grid is fine too.
- `pane-card` → **card** — Each of Transcript / Decisions / Utterances becomes a Card with CardHeader (title + count) and CardContent (ScrollArea + list)
- `pane-header` → **card** — Use CardHeader containing CardTitle + count Badge in a flex row
- `pane-count-badge` → **badge** — Badge with dark variant for pane counts
- _… +12 more mappings_

#### `frontend/src/routes/templates/+page.svelte` — complexity: **low**

~539 LOC total but only ~225 lines of markup — the rest is scoped CSS that will largely be removed once shadcn-svelte primitives + Tailwind classes take over. Straightforward CRUD page: list + create/edit modal + delete confirm. No SSE, no audio, no websockets, no drag/drop, no real-time updates. State is simple Svelte 5 $state runes. Form has mode-conditional validation (autonomous requires instructions, limited_auto_speak requires allowed_replies) — fits naturally into formsnap + zod superforms refinement. Mode badges have 6 distinct color variants (listen_only/suggest_only/approval_required/limited_auto_speak/free_auto_speak/autonomous) — either define 6 custom Badge variants in components.json or use className overrides; default Badge primitive has only default/secondary/destructive/outline. Native confirm() for delete should be upgraded to AlertDialog (better UX, browser-verifiable per CLAUDE.md). Confirm() has two branches: plain delete vs forced delete when referenced by meeting configs (refs > 0) — AlertDialog should preserve the conditional warning copy. The Saving/Refreshing label swap pattern (button text toggles based on loading boolean) is preserved as-is on shadcn Button. Modal backdrop currently has z-index:50 and inset:0 — Dialog primitive handles overlay automatically. No responsive sheet/drawer needed at <640px — current CSS just stacks card actions; can keep with Tailwind responsive classes. Accessibility already decent (role=alert, role=dialog, aria-modal, aria-labelledby) — shadcn primitives will preserve/improve this. Risk: low — most items map 1:1 to shadcn primitives; only edge is the 6-variant mode badge color scheme.

**Primitive mappings (18):**

- `refresh-button-secondary, edit-button-secondary, cancel-button-secondary` → **button** — All outline/default-variant buttons map directly to shadcn-svelte Button with variant='outline' (or 'secondary'). Includes disabled and loading-text swaps.
- `new-template-button-primary, submit-button-primary` → **button** — Primary action buttons map to Button with variant='default' (primary indigo) — keep current dynamic label for loading states.
- `delete-button-destructive` → **button** — Red Delete button maps to Button with variant='destructive'.
- `error-alert, form-error-alert` → **alert** — Inline error banners (page-level and inside modal) map to Alert with variant='destructive', preserving role='alert' semantics.
- `template-row-card` → **card** — Each <li class='template'> is a bordered card with title + meta + actions — natural fit for Card (CardHeader, CardContent, CardFooter or inline actions).
- `mode-badge (6 color variants), ref-count-badge` → **badge** — Pill badges map to Badge primitive. Mode badges need 6 distinct color variants — either custom variants or className overrides. Ref badge uses variant='secondary'.
- `modal-dialog (form)` → **dialog** — Create/edit modal maps to Dialog (DialogContent + DialogHeader + DialogTitle + DialogFooter), replacing the manual modal-backdrop + role=dialog markup.
- `confirm-dialog (delete)` → **alert-dialog** — Replace native confirm() with AlertDialog for destructive Delete action, preserving the conditional 'referenced by N meeting configs' message branch and cascade-delete warning.
- `form` → **form** — Template create/edit form should use shadcn-svelte Form (formsnap + sveltekit-superforms) for typed validation of name/mode/instructions/context/allowed_replies/threshold with mode-conditional rules.
- `text-input` → **input** — Name field maps to Input primitive.
- `textarea (x3)` → **textarea** — base_instructions, base_context, allowed_replies all map to Textarea. The allowed-replies one keeps monospace styling via className.
- `select-dropdown (BOT_MODES)` → **select** — Native <select> for mode maps to Select (SelectTrigger/SelectContent/SelectItem) iterating BOT_MODES.
- `range-slider` → **slider** — Confidence threshold range input maps to Slider, with live-displayed numeric value in the label.
- `form-field-label` → **label** — Bold field labels map to Label primitive (used inside FormField when paired with Form).
- `conditional-helper-text` → **form** — Helper <small> text maps to FormDescription within shadcn Form fields; standalone usages can use typography muted variant.
- `definition-list (template-details)` → **typography** — No direct shadcn primitive for dl/dt/dd — keep semantic <dl> with shadcn typography classes (muted-foreground, text-sm) for the Allowed replies / Confidence display.
- `lede-paragraph, h1-heading, snippet-paragraph, empty-state-text` → **typography** — All text blocks (heading, lede, snippets, empty state) map to shadcn-svelte Typography utilities (h1, p, muted) — no separate primitive, just className utilities.
- `template-list` → **card** — List container is just a grid wrapper — no shadcn primitive needed; cards inside provide the structure. Could optionally add scroll-area if list grows long.

## 3. Migration phases

### Phase 0 — Coordination & freeze check

**Goal:** Confirm in-flight tickets have either landed or are deliberately serialized behind this migration; freeze the modal/dialog pattern decision; capture a golden baseline of every page in the current UI for visual diffing.

**Estimated effort:** 2-4h

**Steps:**

- Re-verify ticket status: stt.3, stt.5, stt.7 are CLOSED (confirmed); stt.8's bespoke modal pattern is the only open coordination item — decide policy: migrate its surfaces directly to shadcn Dialog rather than preserving the bespoke pattern, and update stt.8 with a comment to that effect before code begins.
- Run ./scripts/start-chrome.sh and take chrome-devtools screenshots of all 8 pages in their current state (frontend/src/routes/+page.svelte, /calendar, /playground, /providers, /settings, /sessions/[id], /history, /history/[id], /templates) plus the layout shell in both desktop (1440px) and mobile (390px) viewports — store as PR-attachable PNGs.
- Create a long-lived branch feat/shadcn-svelte-migration off main; lock no other PRs touch frontend/src/routes/* until cutover (use bd to file a temporary 'do-not-touch frontend' marker if needed).
- Audit the 116 data-testid attributes in calendar (24), playground (22), providers (48), sessions/[id] (22) into a single text file at frontend/.migration/testid-inventory.txt — this is the regression contract.
- Confirm svelte.config.js already enforces compilerOptions.runes = true (verified) — no flip required; document that this satisfies the canonical install doc's Svelte-5 prerequisite.

**Deliverables:**

- frontend/.migration/baseline-screenshots/ (16+ PNGs)
- frontend/.migration/testid-inventory.txt
- Branch feat/shadcn-svelte-migration created
- bd note on stt.8 documenting the 'use shadcn Dialog, not bespoke pattern' decision

### Phase 1 — Tailwind v4 + shadcn-svelte init

**Goal:** Land the foundation: Tailwind v4 plugin in Vite, src/app.css with slate tokens, components.json wired to $lib aliases, utils.ts with cn() helper. Nothing visual changes yet because no component imports the new CSS.

**Estimated effort:** 2-3h

**Steps:**

- cd frontend && pnpm dlx sv add tailwindcss — this installs tailwindcss + @tailwindcss/vite, edits vite.config.ts to add tailwindcss() plugin (before sveltekit()), creates src/app.css with @import "tailwindcss";, and adds import '../app.css'; to src/routes/+layout.svelte.
- pnpm dlx shadcn-svelte@latest init --base-color slate --css src/app.css --lib-alias '$lib' --components-alias '$lib/components' --utils-alias '$lib/utils' --hooks-alias '$lib/hooks' --ui-alias '$lib/components/ui' — overwrites src/app.css with the full @theme inline + slate :root + .dark blocks per canonical theming.md.
- Verify generated files: frontend/components.json (slate, src/app.css, $lib aliases), frontend/src/lib/utils.ts (exports cn + WithoutChild/WithoutChildren/WithoutChildrenOrChild/WithElementRef per migration/svelte-5.md), frontend/src/app.css with @import 'tailwindcss', @import 'tw-animate-css', @theme inline block, and slate :root + .dark blocks.
- Verify vite.config.ts has tailwindcss() before sveltekit() in plugins.
- pnpm dev — confirm app boots, no console errors, every page still renders exactly as before (pure-CSS isolation guarantees Tailwind utilities are loaded but unused).
- Run typecheck + lint to confirm no Svelte runes regressions from app.css import.

**Deliverables:**

- frontend/vite.config.ts updated
- frontend/components.json
- frontend/src/app.css (slate theme tokens + Tailwind v4 + tw-animate-css)
- frontend/src/lib/utils.ts (cn helper + type utils)
- frontend/src/routes/+layout.svelte (single new line: import '../app.css';)
- package.json + pnpm-lock.yaml diff

### Phase 2 — Dark mode + global notifications + base primitive set

**Goal:** Install mode-watcher, set up Sonner globally (currently no toast surface exists app-wide), and scaffold the universal primitive set so per-page migrations can pull components without round-tripping to the CLI.

**Estimated effort:** 4-6h

**Steps:**

- pnpm i mode-watcher && add <ModeWatcher /> to frontend/src/routes/+layout.svelte (per canonical dark-mode/svelte.md). Skip the dark-mode toggle UI for now — the .dark class is wired but no toggle is exposed (per inventory: app has no dark mode today; landing it as deferred opt-in keeps scope tight).
- pnpm dlx shadcn-svelte@latest add button card badge alert input label textarea separator skeleton tooltip dialog alert-dialog sheet select dropdown-menu checkbox switch collapsible scroll-area progress slider sonner tabs table pagination form — single batch install; transitive deps (bits-ui, @lucide/svelte, svelte-sonner, formsnap, sveltekit-superforms, zod, tailwind-variants, tailwind-merge, clsx, paneforge for resizable, vaul-svelte for drawer-style sheet) auto-install.
- pnpm dlx shadcn-svelte@latest add sidebar resizable — these are page-specific (sidebar for +layout.svelte; resizable for session detail/history detail/playground).
- Add <Toaster /> from $lib/components/ui/sonner to +layout.svelte, positioned after {@render children?.()} so toasts overlay the entire app — replaces the inventory-flagged 'no toast library' gap in playground/settings/calendar.
- Extend frontend/src/lib/components/ui/badge/badge.svelte with custom variants required across pages: 5 status-pill colors (scheduled/joining/joined/ended/failed) + source-browser purple + 6 template-mode colors (listen_only/suggest_only/approval_required/limited_auto_speak/free_auto_speak/autonomous) + 5 decision-outcome variants (spoken/suppressed/pending/rejected/suggested) — use tailwind-variants per shadcn-svelte convention. Centralize the mapping in $lib/components/badges.ts so per-page code stays clean.
- Extend Button variants with 'success' (green) per layout inventory (approval Approve button).
- Smoke test: render a <Sonner.toast.success/> from each page's onMount once behind a feature flag to confirm global toast routing works; revert.

**Deliverables:**

- frontend/src/lib/components/ui/* (~26 component folders)
- frontend/src/lib/components/badges.ts (variant maps)
- frontend/src/routes/+layout.svelte (+ ModeWatcher + Toaster)
- package.json deps locked (mode-watcher, bits-ui, svelte-sonner, formsnap, sveltekit-superforms, zod, @lucide/svelte, tailwind-variants, paneforge, vaul-svelte)

### Phase 3 — Shell migration: +layout.svelte sidebar

**Goal:** Replace the bespoke 849-LOC layout with shadcn Sidebar primitive while preserving SSE subscriptions, approval timers, browser notifications, polling, and OAuth postMessage handshake.

**Estimated effort:** 1-1.5d

**Steps:**

- Read frontend/src/routes/+layout.svelte fully and map: nav links → SidebarMenuButton with isActive computed from page.url.pathname startsWith (per inventory note 8); mobile drawer → Sidebar collapsible='offcanvas' + SidebarTrigger (per inventory note 9); 5 status-pill variants + source-browser → use the badges.ts map from Phase 2; approval pane → Card + Alert; success Approve button → Button variant='success' from Phase 2.
- Move all lifecycle code (subscribeToGlobal SSE, per-session subscribeToSession Map, approval setTimeout Map keyed by decisionId, browser Notifications API bootstrap, 30s polling interval, 'johnny:oauth' postMessage listener) into a dedicated module frontend/src/lib/layoutLifecycle.svelte.ts that the new Sidebar-wrapped layout calls from onMount/onDestroy — keeps the .svelte template thin.
- CRITICAL: verify the timer Map survives Sidebar collapse/expand state changes (shadcn Sidebar uses CSS-only collapse, so component instances are NOT destroyed — but write a chrome-devtools test that asserts a pending-approval countdown keeps ticking through a sidebar toggle).
- Preserve mobile-only behaviour: shadcn Sidebar's offcanvas mode replaces the bespoke backdrop and z-index management — verify by resizing to 390px in chrome-devtools.
- Delete the ~530 LOC of bespoke CSS from the <style> block of +layout.svelte once the shell is fully Tailwind-driven.
- Run chrome-devtools tour: navigate to / → take_snapshot → click each sidebar link → assert page.url updates and active pill highlights correctly → resize to 390px → click SidebarTrigger → assert drawer opens with backdrop → click outside → assert it closes.

**Deliverables:**

- frontend/src/routes/+layout.svelte (rewritten with Sidebar.Provider + Sidebar + SidebarMenu)
- frontend/src/lib/layoutLifecycle.svelte.ts (SSE, timers, notifications, polling, OAuth listener)
- Chrome-devtools verification screenshots: desktop sidebar, mobile drawer open, approval pane with timer

### Phase 4 — Tiny pages: home, history list, templates

**Goal:** Migrate the three lowest-complexity pages to validate the primitive set and form patterns before tackling stateful work.

**Estimated effort:** 1d

**Steps:**

- frontend/src/routes/+page.svelte (62 LOC): wrap in Card; swap headings to Typography slots; replace colored <p> status with Alert variant='default' for ok and 'destructive' for error; convert <button> to Button variant='secondary'; preserve VITE_API_BASE env read at module scope (inventory note).
- frontend/src/routes/history/+page.svelte (~442 LOC): Card wrapper; semantic search Input + Button submit (no Form primitive needed — single field); results Table from $lib/components/ui/table with right-aligned monospace TableCell className; status pill via badges.ts map; visually-hidden label → Label class='sr-only'; prev/next as Button pair (inventory recommends NOT using full Pagination primitive); preserve all data-testids; preserve inline <a href='/history/[id]'> (no Link primitive).
- frontend/src/routes/templates/+page.svelte (~539 LOC): Card list + create/edit Dialog + delete AlertDialog (upgrade from window.confirm per inventory item 'Native confirm() for delete should be upgraded to AlertDialog'); 6 mode badge variants from badges.ts; form uses formsnap + zod with refinement: autonomous→requires instructions, limited_auto_speak→requires allowed_replies (per inventory mode-conditional validation); Slider for confidence threshold; preserve role=alert / role=dialog semantics (shadcn handles natively).
- Delete all <style scoped> blocks from these three files after migration — Tailwind classes replace them entirely.
- Chrome-devtools tour per page: navigate, snapshot, exercise primary action (re-check / search / create template / delete template), assert AlertDialog focus trap works, capture screenshot.

**Deliverables:**

- frontend/src/routes/+page.svelte (rewritten ~30 LOC)
- frontend/src/routes/history/+page.svelte (rewritten ~180 LOC, all data-testids preserved)
- frontend/src/routes/templates/+page.svelte (rewritten ~250 LOC + formsnap schema in $lib/forms/templateSchema.ts)
- 3x chrome-devtools verification screenshots

### Phase 5 — Settings page + OAuth flow

**Goal:** Migrate frontend/src/routes/settings/+page.svelte: 2 Dialogs (add account + connect bot session), 3 destructive AlertDialogs (disconnect + 2x bot disconnect with conditional 409 escalation), file upload, OAuth popup handshake — surfaces are well-bounded.

**Estimated effort:** 1d

**Steps:**

- Replace the two bespoke modals (Add account + Connect bot session) with shadcn Dialog — both already have aria-modal + role=dialog (inventory 'straightforward Dialog migration').
- Replace all three window.confirm() calls with AlertDialog — the 409 force-delete escalation requires a two-step dialog whose copy reflects server response shape meeting_config_count (inventory note 2). Implement as a single AlertDialog whose body is $derived from server state.
- File upload for bot session JSON: keep <Input type='file'> with custom Label styling (browser-native file inputs are ugly per inventory); read via File.text() unchanged; surface 4 MiB hint via FormDescription, validation copy via FormMessage.
- Per-row state machine (busyId, reconnectingId, botBusyId): centralize in a small Map<accountId, {busy: boolean; reconnecting: boolean; botBusy: boolean}> store; pass through to Card variant className via cn().
- Native <select bind:value={formRole}> → shadcn Select with controlled value/onValueChange (NOT bind:value — Select uses callback API per inventory note 9); test the visually-hidden Role label remains screen-reader-accessible.
- Add Copy button (Button + Sonner toast) for the CLI command in the Collapsible <pre> block — this is a UX improvement called out in inventory note 6.
- Surface transient feedback via Sonner: 'Account disconnected', 'Bot session uploaded', 'Reconnection email sent'. Keep persistent errors in Alert.
- Preserve the OAuth popup-blocked fallback link as an Alert (inventory note 1).
- Chrome-devtools tour: navigate to /settings → take_snapshot → click 'Add account' → assert Dialog opens with focus trapped → close → click disconnect on a row → assert AlertDialog with destructive copy → confirm → assert account removed and Sonner toast appears → upload a bot session JSON via fill_form → assert success.

**Deliverables:**

- frontend/src/routes/settings/+page.svelte (rewritten)
- frontend/src/lib/forms/accountSchema.ts + botSessionSchema.ts
- Chrome-devtools verification: add account flow, disconnect AlertDialog, 409 escalation dialog, file upload, OAuth popup-blocked Alert

### Phase 6 — History detail + session detail (read-mostly, real-time)

**Goal:** Migrate the two observatory pages. Session detail is harder due to live SSE; history detail is mostly layout.

**Estimated effort:** 1.5-2d

**Steps:**

- frontend/src/routes/history/[id]/+page.svelte (~847 LOC): 3-pane Card+CardHeader+ScrollArea trio (Transcript / Decisions / Utterances); optional Resizable; 5 decision-outcome Badges + 5 status pill Badges via badges.ts; promote inline destructive confirm to AlertDialog (inventory note 3); preserve all data-testids; Export JSON as Button asChild wrapping <a download> so the download attribute survives (inventory note Behaviour preservation risks).
- frontend/src/routes/sessions/[id]/+page.svelte (~1126 LOC): same 3-pane Card+ScrollArea pattern; preserve 8 SSE event types + per-approval setInterval countdown Map cleared in onDestroy; CRITICAL — expose the inner viewport of ScrollArea so the imperative bind:this scroll-to-bottom pattern (tick() + scrollTop=scrollHeight after each transcript update) keeps working. shadcn-svelte ScrollArea forwards a ref via bind:this on the viewport — use `let viewportEl = $state<HTMLDivElement | null>(null);` and pass to ScrollArea via the documented ref prop.
- Optimistic UI for resolvingDecisionIds Set on Approve/Reject: wrap Button variant='success' (Approve) and variant='destructive' (Reject) with disabled={resolvingDecisionIds.has(id)} — preserve existing optimistic state pattern.
- Partial transcript dashed-amber line: render as a separate dashed-border row at the tail; use Tailwind border-dashed + border-amber-500 with className override.
- Preserve the 3-pane responsive collapse at <1100px via 'lg:grid-cols-3 grid-cols-1' (inventory note for both pages).
- Chrome-devtools tour: navigate to /history → click a session row → take_snapshot of history detail → assert 3-pane layout + count badges → click Delete → assert AlertDialog → cancel. Then navigate to an active /sessions/[id] (one with live transcripts) → take_snapshot → wait_for transcript_partial via list_console_messages or evaluate_script → assert ScrollArea auto-scrolls to bottom → assert countdown timer ticks → click End session → assert AlertDialog → cancel.

**Deliverables:**

- frontend/src/routes/history/[id]/+page.svelte (rewritten)
- frontend/src/routes/sessions/[id]/+page.svelte (rewritten)
- Chrome-devtools verification: history detail panes, session detail with live SSE auto-scroll, countdown timer continuity, AlertDialog End-session

### Phase 7 — Calendar page

**Goal:** Migrate frontend/src/routes/calendar/+page.svelte (1322 LOC, complexity high): slide-over Sheet, two-tier delete (Switch + AlertDialog), form with bidirectional transforms, 24 data-testids.

**Estimated effort:** 1.5-2d

**Steps:**

- Decide Sheet vs in-page rail per inventory note 7: current bespoke rail is aria-modal='false' (non-modal). Keep it non-modal via Sheet's open prop with modal={false} — shadcn-svelte Sheet wraps Bits UI Dialog which accepts modal: boolean; this preserves coexistence with the main page (no focus trap, no inert background).
- Event rows with role='button' + handleRowKey: replace with Card asChild around a Button (shadcn Button supports asChild via render delegation) OR Card with role='button' tabindex={0} onkeydown — preserve keyboard semantics (Enter/Space).
- Two-tier delete UX (toggle → pendingDelete → confirmDelete): replace toggle with Switch onCheckedChange (per inventory note 3); replace 'pendingDelete' inline alert with AlertDialog. Rewire the onEnableToggle/confirmDelete/cancelDelete handlers — current code mutates event.currentTarget.checked which won't work with Switch (controlled).
- Form migration to formsnap + zod: schema { template_id, identity_id, mode: enum, instructions, context, allowed_replies: array (with parseAllowedRepliesText/formatAllowedRepliesText adapters per inventory note 5), confidence_threshold: z.number().min(0).max(1).optional() — replaces parseThreshold 'invalid' sentinel per inventory note 6 }.
- Three native <select onchange> patterns → Select.onValueChange callback (template/identity/mode) — inventory note 12 confirms this simplifies code.
- Side effect on save (mutates summary.events[idx].has_meeting_config in place per inventory note 8): preserve in the onUpdate handler of superforms.
- Reauth empty state deep-link with hash fragment (#account-{id}): preserve as <a> inside Alert (inventory note 10).
- Sync delta badge with unicode '·' '~' '−' + monospace: Badge with custom className='font-mono'.
- Preserve event row 2-column grid (130px monospace time + main) inside Card via grid grid-cols-[130px_1fr] gap-3 (inventory note 13).
- All 24 data-testids preserved.
- Chrome-devtools tour: navigate to /calendar → take_snapshot → click an event row → assert Sheet slides in from right (non-modal — page still scrollable) → fill_form for instructions/context/allowed_replies → click Save → assert save-success badge → toggle Enable off → assert AlertDialog → confirm → assert event row reflects has_meeting_config=false → click join-now-button → assert navigation.

**Deliverables:**

- frontend/src/routes/calendar/+page.svelte (rewritten)
- frontend/src/lib/forms/meetingConfigSchema.ts
- Chrome-devtools verification: Sheet non-modal behaviour, two-tier delete with AlertDialog, form save, join-now

### Phase 8 — Playground (mic + WS + SSE + dictation state machine)

**Goal:** Migrate frontend/src/routes/playground/+page.svelte without breaking the live audio pipeline, dictation state machine, or 4-state animated indicator.

**Estimated effort:** 2-3d

**Steps:**

- Two big states (setup vs live, gated by isLive = $derived(liveSession != null)): render as conditional sub-templates wrapped in Card; do NOT collapse to a single template (inventory note 1).
- Preserve startBrowserAudioSession + startPlaygroundStt + subscribeToSession unchanged — these are imports from $lib/* and don't touch the template. Only the *rendering* of their state changes.
- Dictation state machine (idle→starting→recording→stopping) with mic mute side effects (dictationPrevMicMuted save/restore per inventory note 2): keep state logic identical; replace mic Toggle button with shadcn Toggle but provide 4 child snippets via {#snippet content_idle()} / {#snippet content_starting()} etc. — Toggle accepts snippet children. NOTE: shadcn Toggle is bits-ui Toggle which natively supports 2 states; for 4 states use a plain Button with $derived className + icon swap instead.
- 4-state live-state indicator (idle/listening/thinking/speaking): keep the $derived expression + CSS @keyframes pulse. Move the keyframes into src/app.css under a custom @layer utilities block so Tailwind purge keeps them. Apply via cn() with state-specific class names: animate-[pulse-listening_1.2s_infinite] / pulse-thinking_0.8s_infinite / pulse-speaking_0.6s_infinite.
- Chips row ($derived.by computing activeChips from playground_overrides with 'active default' annotations per inventory note 4): render as Badge list inside a flex flex-wrap gap-2 container.
- 3 near-duplicate provider Selects ({#each ['stt','llm','tts']}): keep iteration but extract <ProviderSelect kind={k} value={v} onValueChange={cb} /> wrapper component in $lib/components/playground/ProviderSelect.svelte (inventory note 5 recommendation).
- URL-driven reattach (onMount reads ?session=N, calls reattachToSession): preserve unchanged.
- Custom mic-level meter with green→yellow→orange gradient (inventory note 8): override Progress [data-slot=progress-indicator] with bg-gradient-to-r from-green-500 via-yellow-500 to-orange-500 via className.
- Anchor-as-button 'Open session detail' with target=_blank (inventory note 9): use <Button href='...' target='_blank' rel='noopener'> — shadcn-svelte Button supports asChild/anchor pass-through.
- Inline Alert preserved for errors (no Sonner — keeps existing UX per inventory note 10).
- Bare Input/Textarea/Select with Label — NO formsnap (inventory note 11: validation is minimal).
- A11y: if tests expect role='meter' on mic level, override Progress role via attribute pass-through (inventory note 12).
- All 22 data-testids preserved.
- CRITICAL chrome-devtools tour: navigate to /playground → take_snapshot of setup state → fill_form mode/template/persona/system_prompt/context → click Start → wait_for live-state attribute to appear → assert audio-live testid present → grant mic permission via emulate → click toggle-mic → assert state machine progresses idle→starting→recording → speak (or evaluate_script to trigger fake transcript_partial event) → assert playground-text-input updates with partials → click toggle-mic → assert recording stops + prior mute state restored → click Interrupt → assert agent_spoke event handler fires → click End session → assert teardown.

**Deliverables:**

- frontend/src/routes/playground/+page.svelte (rewritten)
- frontend/src/lib/components/playground/ProviderSelect.svelte
- frontend/src/app.css (added @keyframes pulse-listening/thinking/speaking under @layer utilities)
- Chrome-devtools verification: setup form, live state with mic level, dictation state machine, mic mute side effect preservation

### Phase 9 — Providers page (very_high complexity)

**Goal:** Migrate the 2358-LOC providers page: dynamic JSON-schema-driven form, mic test recorder, streaming pip install log, 2 modals, master/detail with opaque DraftKey selection model, 48 data-testids.

**Estimated effort:** 2.5-3.5d

**Steps:**

- Recommended migration order per inventory: shared primitives first (Button/Badge/Alert/Input/Label — already scaffolded in Phase 2) → Form + dynamic field renderer → Tabs → Dialog for export + voice browser → Card + ScrollArea + Progress for polish.
- Tri-tab (STT/LLM/TTS) → shadcn Tabs; verify wrap behaviour on mobile (inventory note 10, @media max-width: 880px).
- Selection model with DraftKey ('instance-<id>' vs 'new-<name>') + per-kind localStorage persistence: preserve unchanged — pure logic, not template.
- Dynamic schema-driven form with 7 field types (text/password/number/url/textarea/select/checkbox) currently rendered via {#snippet fieldRow}: keep the snippet pattern; map each field type to the corresponding shadcn primitive inside the snippet (Input type=password / Input type=number / Input type=url / Textarea / Select / Checkbox). DO NOT refactor to a child component — the snippet is more idiomatic Svelte 5 and stays close to the field metadata (inventory note 1).
- ValidationFailure → formErrors[key] external server errors (inventory note 8): superforms accepts setError() from server response — wire onResult handler.
- Mic test recorder ($lib/sttMicRecorder + live animated level): Progress with custom gradient (same pattern as Phase 8 mic meter).
- Streaming pip install log: ScrollArea wrapping <pre>; auto-scroll on chunk arrival via the same viewport-ref pattern from Phase 6 session detail.
- Two modals (Export + Piper voices): shadcn Dialog. Voice browser nested list with per-row state (preview/install/remove playing/loading) + client-side filter: keep as a sub-component <VoiceBrowserDialog /> in $lib/components/providers/.
- HTMLAudioElement playback lifecycle (URL.createObjectURL + playingHandles Map + onDestroy cleanup) preserved unchanged — no shadcn primitive helps; Buttons reflect playing/loading via $derived className + icon (per inventory note 5).
- Custom 'active' card border-color: #10b981 + dashed-border 'add' cards: Card with className={cn('border-2', isActive && 'border-emerald-500', isAdd && 'border-dashed')} (inventory note 9).
- All 3 window.confirm() delete calls → AlertDialog (inventory note 11).
- All 48 data-testids preserved.
- Chrome-devtools tour (matches stt.7 acceptance criteria): navigate to /providers → take_snapshot → click STT tab → click Add → fill_form display='Ollama Qwen 35B' + model='qwen-35b' → Save → assert success Sonner + new card appears → click Add again → fill display='Ollama Llama 8B' + model='llama-3-8b' → Save → assert BOTH instances visible (regression test for stt.7) → click Test on first instance → grant mic → record → assert transcript appears + latency badge updates → click Export → assert Dialog → close → switch to TTS tab → click Piper voices Browse → assert voice list dialog → filter → preview a voice → assert audio plays. Then re-run for LLM tab. Then chrome-devtools resize_page 880px → assert Tabs wrap correctly + detail pane stacks below list.

**Deliverables:**

- frontend/src/routes/providers/+page.svelte (rewritten — likely still ~1200 LOC due to inherent complexity)
- frontend/src/lib/components/providers/VoiceBrowserDialog.svelte
- frontend/src/lib/components/providers/ExportDialog.svelte
- frontend/src/lib/forms/providerSchemas.ts (one zod schema per kind)
- Chrome-devtools verification: multi-instance save (stt.7 regression), Test mic recorder, Export dialog, Piper voice browser, streaming pip log auto-scroll, mobile Tabs wrap

### Phase 10 — CSS purge, bundle audit, full regression tour

**Goal:** Confirm legacy CSS is fully removed, Tailwind purge ships only used utilities, no visual regressions, all 116 data-testids resolve.

**Estimated effort:** 1-1.5d

**Steps:**

- grep -r '<style' frontend/src/routes — should return 0 results (or only @layer utility blocks like the playground pulse keyframes).
- pnpm build && check dist size: confirm Tailwind purged output (look for absence of unused color classes); compare frontend/.svelte-kit/output bundle size before/after — record delta in PR description.
- Run pnpm typecheck + pnpm lint — must pass clean.
- Full chrome-devtools MCP regression tour scripted as a single sequence:
  - Sign-in flow: navigate /settings → add account → OAuth popup mock → assert refresh
  - Add provider (multi-instance per stt.7): navigate /providers → STT tab → add 2 Ollama → assert both saved
  - Calendar import: navigate /calendar → assert reauth empty state if needed → switch account → assert events render → open event → save meeting config
  - Leave-now (calendar join-now-row → click join-now-button): navigate /calendar → click join-now-button on a current event → assert session created and goto /sessions/[id]
  - Start session (playground): navigate /playground → fill setup → Start → assert live state
  - Send chat (playground): in live state, fill playground-text-input → submit → assert message round-trip via SSE
  - End session: click playground-end-button → assert teardown + Sonner toast
  - History review: navigate /history → search → click result → navigate /history/[id] → Export JSON
- Verify the 116 data-testids resolve in chrome-devtools via take_snapshot on each page — diff against frontend/.migration/testid-inventory.txt; fail the PR if any are missing.
- Mobile pass: resize_page to 390px and replay the regression tour focusing on Sidebar drawer, Sheet, Dialog, AlertDialog overlay behaviour.
- Capture final 16+ PNGs to frontend/.migration/post-migration-screenshots/ for direct visual diff against Phase 0 baseline.
- Open PR with: Phase 0 baseline PNGs + Phase 10 post-migration PNGs + bundle size delta + testid inventory diff + full chrome-devtools transcript of the regression tour.

**Deliverables:**

- PR feat/shadcn-svelte-migration → main
- frontend/.migration/post-migration-screenshots/
- PR description with: baseline-vs-post screenshots, bundle delta, testid diff = 0, mode-watcher dark-mode demo (even though no toggle UI ships)
- Empty <style> sections in all routes
- 0 visual regressions per chrome-devtools diff

## 4. Cutover strategy

**Recommended:** `big_bang`

Big-bang on a single long-lived feature branch is the right call here for four reasons. (1) No existing Tailwind / no existing shadcn-svelte means there is no 'two design systems coexisting' phase to manage — the only coexistence risk is CSS specificity collisions between scoped <style> blocks and Tailwind utilities, and that risk is highest *during* a staged rollout (mixed pages would render with inconsistent tokens, fonts, and spacing). (2) The 3 highest-coordination tickets (stt.3 / .5 / .7) already closed, leaving only stt.8's modal-pattern decision — which Phase 0 freezes as 'use shadcn Dialog directly'. There is no in-flight stream of UI PRs to rebase against. (3) P2 priority + 10-14 day estimate fits comfortably inside a single dev sprint; staging would multiply review/test overhead by ~4x (each merged phase needs its own PR + chrome-devtools regression). (4) The Layout shell migration (Phase 3) is foundational — every child route inherits its grid/sidebar/Toaster — so it cannot meaningfully ship without at least the most-trafficked pages migrated alongside it. Tradeoff accepted: a big-bang PR is bigger to review and revert, but the alternative (staged with main as the integration target) is *worse* because it leaves the layout shell in a half-migrated state where the Sidebar primitive coexists with un-migrated child page CSS for weeks. Mitigation: the work is *internally staged* across Phases 0–10 with chrome-devtools verification gates between each phase on the same branch; if any phase fails verification, only that phase is reverted, not the whole migration. The PR is opened only after Phase 10 passes.

## 5. Risks & mitigations

### Tailwind v4 + Svelte 5 runes interaction: tailwindcss() Vite plugin must precede sveltekit() in plugins[]; if order is r

**Risk:** Tailwind v4 + Svelte 5 runes interaction: tailwindcss() Vite plugin must precede sveltekit() in plugins[]; if order is reversed, Svelte component <style> blocks lose Tailwind's @apply support. Additionally, runes-mode propagation of bindable props through shadcn-svelte components (let { open = $bindable(false) } = $props()) can subtly mis-wire if any wrapper component still uses legacy export let.

**Mitigation:** Phase 1 explicitly verifies plugin order in vite.config.ts. The migration uses zero wrapper components in early phases — pages import primitives directly. Where wrappers are needed (ProviderSelect, VoiceBrowserDialog, ExportDialog), follow the WithElementRef + $bindable pattern from migration/svelte-5.md verbatim. svelte.config.js already enforces runes: true globally (confirmed via Read), so no flip risk.

### SSE stream / WebSocket / audio API lifecycle disruption — playground has live mic capture + WS push + SSE event stream; 

**Risk:** SSE stream / WebSocket / audio API lifecycle disruption — playground has live mic capture + WS push + SSE event stream; session detail has 8 SSE event types + per-approval setInterval Map; layout has subscribeToGlobal + per-session Map + 30s polling + browser Notifications API + OAuth postMessage. Any of these can silently break if onMount/onDestroy lifecycles are reshaped during the migration.

**Mitigation:** Lifecycle code is extracted to dedicated modules ($lib/layoutLifecycle.svelte.ts in Phase 3) BEFORE the template is rewritten — so the same hooks run regardless of template shape. shadcn-svelte components are CSS-only / stateless wrappers around bits-ui — they do not destroy/recreate child instances on prop changes. Phase 3 explicitly tests timer continuity through Sidebar collapse/expand via chrome-devtools. Phase 8 explicitly tests dictation state machine + mic mute restore. The mic mute side effect (dictationPrevMicMuted) is the most fragile point — preserve it word-for-word and assert via evaluate_script in chrome-devtools that the mic mute state is restored on dictation stop.

### Multi-instance provider regression (stt.7) is CLOSED but its UI lives in the providers page (Phase 9). A careless port c

**Risk:** Multi-instance provider regression (stt.7) is CLOSED but its UI lives in the providers page (Phase 9). A careless port could re-introduce the kind-based collision that stt.7 fixed.

**Mitigation:** Phase 9 chrome-devtools tour explicitly replays stt.7's acceptance criteria: add 2 Ollama instances with distinct displays + models, assert both save and both render. This is a hard gate — Phase 9 does not close until this passes. Additionally, the providers page form uses opaque DraftKey ('instance-<id>' vs 'new-<name>') as its selection model — this is preserved verbatim because it's the structural guarantee that prevents the regression.

### In-flight modal pattern (stt.8) — bespoke vs shadcn Dialog. If stt.8 ships its own modal pattern before this migration l

**Risk:** In-flight modal pattern (stt.8) — bespoke vs shadcn Dialog. If stt.8 ships its own modal pattern before this migration lands, we either rebase against it (wasting effort) or supersede it (wasting that ticket's work).

**Mitigation:** Phase 0 explicitly resolves this: add a bd note on stt.8 declaring 'all modals use shadcn Dialog directly; bespoke pattern is superseded by Johnny-stt.9'. If stt.8 ships a bespoke modal first, this migration replaces it in Phases 4/5/7/9 anyway — the work is bounded. Communicate the decision upfront so stt.8's owner does not invest in a bespoke pattern.

### CSS specificity collisions during coexistence period — scoped Svelte <style> blocks with high specificity (e.g. .approva

**Risk:** CSS specificity collisions during coexistence period — scoped Svelte <style> blocks with high specificity (e.g. .approval-pane .countdown.warn) can override Tailwind utilities applied to wrapping shadcn primitives, producing visual regressions invisible to typecheck/lint.

**Mitigation:** Big-bang strategy (not staged) eliminates most of this risk because no page is left half-migrated. Within each phase, delete the <style> block in the SAME commit as the template rewrite — never leave dead CSS behind. Phase 10 includes an explicit grep -r '<style' check that must return 0 from frontend/src/routes/. Chrome-devtools take_screenshot in Phases 4–9 provides visual-diff feedback against Phase 0 baseline.

### Bundle size delta — shadcn-svelte components + bits-ui + @lucide/svelte + svelte-sonner + formsnap + superforms + zod + 

**Risk:** Bundle size delta — shadcn-svelte components + bits-ui + @lucide/svelte + svelte-sonner + formsnap + superforms + zod + tailwindcss runtime is a non-trivial dependency set. If purge mis-configures, unused Tailwind utilities can balloon the CSS bundle.

**Mitigation:** Tailwind v4 with @tailwindcss/vite ships content-aware purge by default (no tailwind.config.js content array needed in v4 — it scans the project automatically). Phase 10 compares pnpm build output size against the pre-migration baseline (record both in PR description). @lucide/svelte tree-shakes per-icon (we import @lucide/svelte/icons/sun, not the whole package). Expected net delta: +30–80 KB gzipped JS, +5–15 KB gzipped CSS (pure-CSS delete largely offsets the Tailwind add). If delta exceeds 150 KB gzipped, investigate icon imports + dynamic Component lazy-loading.

### Dark mode story — the app has NO dark mode today. mode-watcher writes .dark on <html> and slate tokens have a .dark bloc

**Risk:** Dark mode story — the app has NO dark mode today. mode-watcher writes .dark on <html> and slate tokens have a .dark block, so dark mode 'works' the instant Phase 2 lands — but every bespoke <style> block still uses hardcoded colors (#10b981, etc.) that will look broken in dark mode.

**Mitigation:** Phase 2 installs mode-watcher but does NOT ship a toggle UI — the .dark class is dormant. Dark mode is therefore opt-in via direct class application or future toggle, not a regression vector. Phase 10 PR description explicitly states 'dark mode wired but no toggle UI; all hardcoded colors replaced with token-aware Tailwind utilities, so a future toggle works out of the box'. If user wants the toggle, that becomes a follow-up ticket (Johnny-stt.10 candidate).

### 116 data-testid attributes power chrome-devtools MCP validation; losing any of them breaks the CLAUDE.md-mandated browse

**Risk:** 116 data-testid attributes power chrome-devtools MCP validation; losing any of them breaks the CLAUDE.md-mandated browser-test contract.

**Mitigation:** Phase 0 captures the full inventory to frontend/.migration/testid-inventory.txt. Phase 10 diffs the live DOM against this file via chrome-devtools take_snapshot + evaluate_script — any missing testid fails the PR gate. Each per-page phase (4–9) lists 'all N data-testids preserved' as a deliverable.

### ScrollArea viewport ref pattern (session detail + history detail + providers pip log) — shadcn-svelte ScrollArea wraps t

**Risk:** ScrollArea viewport ref pattern (session detail + history detail + providers pip log) — shadcn-svelte ScrollArea wraps the scroll viewport in an internal element. Imperative scrolling via bind:this won't work if you bind to the wrong layer.

**Mitigation:** Phase 6 explicitly uses the documented ref prop on ScrollArea.Viewport (not the outer ScrollArea root) and tests auto-scroll-to-bottom via chrome-devtools wait_for + evaluate_script asserting scrollTop === scrollHeight after a synthetic transcript_partial event. Same pattern reused in Phase 8 (mic level container) and Phase 9 (pip install log).

### shadcn-svelte Sheet defaults to modal (focus trap + inert background). Calendar's bespoke slide-over is aria-modal='fals

**Risk:** shadcn-svelte Sheet defaults to modal (focus trap + inert background). Calendar's bespoke slide-over is aria-modal='false' — a regression to modal would break the workflow where the user keeps the calendar list visible while editing a config.

**Mitigation:** Phase 7 explicitly uses Sheet's modal={false} prop (forwarded through to bits-ui Dialog modal: false) — verified via chrome-devtools by asserting the main page is still scrollable + clickable while the Sheet is open.

## 6. Validation strategy

CLAUDE.md mandates real-browser validation via chrome-devtools MCP for every UI surface. Every phase from 3 onward includes a concrete chrome-devtools tour described in 'steps' above. Before any chrome-devtools call: run ./scripts/start-chrome.sh from project root (idempotent). For each tour, load tools via ToolSearch: 'select:mcp__chrome-devtools__navigate_page,mcp__chrome-devtools__take_snapshot,mcp__chrome-devtools__click,mcp__chrome-devtools__fill,mcp__chrome-devtools__fill_form,mcp__chrome-devtools__take_screenshot,mcp__chrome-devtools__list_network_requests,mcp__chrome-devtools__list_console_messages,mcp__chrome-devtools__evaluate_script,mcp__chrome-devtools__resize_page,mcp__chrome-devtools__wait_for'. The PR for this migration must include in its body: (a) Phase 0 baseline screenshots (8 pages × 2 viewports = 16 PNGs), (b) Phase 10 post-migration screenshots in the same set, (c) bundle size delta from pnpm build, (d) testid-diff = 0 confirmation, (e) full chrome-devtools transcript of the Phase 10 regression tour exercising sign-in, add-provider (multi-instance per stt.7), calendar import + leave-now, start session, send chat, end session, history review. Per CLAUDE.md: 'I read the code and it looks right' is NOT validation; 'The unit test passes' is NOT validation; only a recorded chrome-devtools MCP run is. Phase 10 cannot close without all four PR-body artefacts above. Mobile validation (390px viewport) is non-optional — Sidebar offcanvas + Sheet non-modal + Dialog overlay all behave differently at small widths. For SSE + audio + WebSocket flows, use mcp__chrome-devtools__evaluate_script to inject synthetic events into window.dispatchEvent / EventSource readyState assertions, since real backend may not always be running. Per the Phase 6 ScrollArea concern: assert auto-scroll via evaluate_script returning JSON { scrollTop, scrollHeight, clientHeight } and assert scrollTop + clientHeight >= scrollHeight - 8.

## 7. Open questions (require user answers before Phase 0 starts)

- Dark mode toggle UI: should this migration ship the canonical dropdown-menu toggle from dark-mode/svelte.md, or leave mode-watcher dormant and ship the toggle as a follow-up ticket? Recommendation: defer (no current dark mode + scope creep risk), but request explicit confirmation.
- Sidebar primitive default state: shadcn Sidebar supports 'expanded' / 'collapsed' / 'offcanvas' — which is the desktop default? Current bespoke layout is always-expanded on desktop. Recommendation: collapsible='offcanvas' with defaultOpen={true} so desktop preserves current UX and mobile gets drawer behaviour — confirm.
- stt.8 modal pattern: the Phase 0 freeze decision is 'all modals use shadcn Dialog'. If stt.8 has already started writing a bespoke modal pattern, do we want to (a) supersede it via this migration, (b) pause stt.8 until this migration lands, or (c) merge stt.8 first and adapt? Recommendation: (a) — supersede; confirm with stt.8 owner.
- Toast surface: introducing Sonner globally is a UX change (transient toasts where previously there were only inline Alerts). Should toasts replace Alert for transient feedback everywhere, only for new feedback (account disconnected, file uploaded), or be skipped entirely? Recommendation: new transient feedback only — keep persistent errors in Alert, confirm.
- Resizable panes for session detail + history detail: shadcn-svelte's Resizable uses paneforge. The current 3-pane layouts are NOT resizable today. Should we ship resizable as a new UX feature or keep fixed grid columns? Recommendation: keep fixed grid (scope creep), confirm.
- Calendar Sheet vs in-page rail: Phase 7 plans non-modal Sheet (modal=false) to preserve current UX. Alternative is keeping the bespoke fixed-right rail and only swapping internals to shadcn primitives. Recommendation: non-modal Sheet (better a11y, scroll-lock on small screens), confirm.
- Providers page mic test recorder: should the live audio level meter migrate to shadcn Progress with custom gradient (Phase 8 + 9 plan), or stay as a custom <div> with width animation? Recommendation: Progress with gradient (consistency), confirm.
- Form library choice: formsnap + superforms + zod is the canonical shadcn-svelte pattern; alternative is bare bind:value + manual validation (lighter). Migration uses formsnap for Templates, Settings, Calendar, Providers (all server-validated) and bare bind:value for Playground (minimal validation per inventory). Confirm this split is acceptable, since it means two form styles coexist.
- Layout SSE module split (Phase 3): extracting lifecycle code to $lib/layoutLifecycle.svelte.ts means the migration introduces a new architectural pattern (lifecycle modules). Is this acceptable? Alternative: keep lifecycle code inline in +layout.svelte. Recommendation: extract — keeps the .svelte template at <200 LOC, easier to review.
- Branch policy: this migration lives on feat/shadcn-svelte-migration for ~10-14 days. Should the team accept a soft freeze on frontend/src/routes/* during that window, or expect to rebase mid-flight? Recommendation: soft freeze (the PR is internally staged into 10 phases, but ships as one PR to main), confirm.
- Test infra: there is currently NO Playwright or Vitest in the frontend (confirmed: no playwright.config, no /e2e directory, no *.spec/test files). All 116 data-testids exist solely for chrome-devtools MCP validation. Should this migration also bootstrap Vitest + a few unit tests for the new $lib/components/badges.ts variant map and $lib/forms/* schemas? Recommendation: defer (scope creep) — the chrome-devtools tour is the contract.
- Bundle size budget: what is the acceptable delta? Recommendation: +150 KB gzipped JS hard ceiling, +20 KB gzipped CSS hard ceiling — confirm thresholds or set new ones.
- Custom 'success' Button variant (Approve button) and the 16 custom Badge variants (5 status + 1 source-browser + 6 template-mode + 5 decision-outcome): add these to button.svelte and badge.svelte directly, or maintain a separate $lib/components/badges.ts map (Phase 2 plan)? Recommendation: $lib/components/badges.ts map — keeps shadcn primitives untouched (easier future shadcn upgrades), confirm.

## 8. Canonical installation quickstart

Verbatim canonical commands and file contents extracted from https://shadcn-svelte.com/docs/installation/sveltekit, components-json, theming, and dark-mode/svelte (consult the appendix below for the full agent extraction).

Key bullets:

- Foundation: `cd frontend && pnpm dlx sv add tailwindcss` — installs Tailwind v4, wires `@tailwindcss/vite` plugin into `vite.config.ts` *before* `sveltekit()`, creates `src/app.css`, and adds `import '../app.css'` to `+layout.svelte`.
- Init: `pnpm dlx shadcn-svelte@latest init` with base color `Slate`, CSS path `src/app.css`, aliases `$lib`, `$lib/components`, `$lib/utils`, `$lib/hooks`, `$lib/components/ui`.
- `components.json`, `src/lib/utils.ts` (with `cn` helper + `WithElementRef` types), `src/app.css` with `@theme inline` block + slate `:root` and `.dark` tokens are all CLI-written.
- Dark mode: `pnpm i mode-watcher`, drop `<ModeWatcher />` into `+layout.svelte`. Toggle UI is deferred (see Open Questions).
- Add components in batch: `pnpm dlx shadcn-svelte@latest add button card badge alert input label textarea separator skeleton tooltip dialog alert-dialog sheet select dropdown-menu checkbox switch collapsible scroll-area progress slider sonner tabs table pagination form sidebar resizable`.

### Appendix A — full extracted install/config doc

```
 I have all the canonical source content. Now I'll synthesize the quickstart from the verbatim docs I fetched.

A. PREREQUISITES

The shadcn-svelte SvelteKit doc recommends creating the project (or adding TailwindCSS) via the SvelteKit CLI:

  pnpm dlx sv create my-app --add tailwindcss

For an existing SvelteKit project that has NO Tailwind yet (Johnny's case at frontend/), the equivalent is to add the Tailwind addon via `sv` to the existing project:

  cd frontend
  pnpm dlx sv add tailwindcss

This is the canonical path — `sv add tailwindcss` installs tailwindcss v4, @tailwindcss/vite, wires up vite.config.ts with the `tailwindcss()` plugin, creates `src/app.css` with `@import "tailwindcss";`, and adds `import "../app.css";` to `src/routes/+layout.svelte`. There is no separate manual `pnpm i tailwindcss @tailwindcss/vite` step in the official guide — `sv add tailwindcss` does it for you.

If for some reason you cannot use `sv add`, the equivalent manual installs (per Tailwind v4 docs that shadcn-svelte assumes) are:

  pnpm add -D tailwindcss @tailwindcss/vite

The shadcn-svelte CLI itself, when run via `shadcn-svelte@latest init`, will additionally install the runtime deps it needs (e.g. `clsx`, `tailwind-merge`, `tailwind-variants`, `bits-ui`, `@lucide/svelte`, `tw-animate-css`) — these are auto-installed by the init/add commands via the detected package manager. You do not pre-install them by hand.

B. INIT

Exact command (run from frontend/):

  pnpm dlx shadcn-svelte@latest init

The init command will prompt you. The verbatim prompts and the correct answers for Johnny's SvelteKit project at /Users/nikita/Projects/Johnny/frontend (TypeScript, $lib aliases, slate base color) are:

  Which base color would you like to use? > Slate
  Where is your global CSS file? (this file will be overwritten) > src/app.css
  Configure the import alias for lib: > $lib
  Configure the import alias for components: > $lib/components
  Configure the import alias for utils: > $lib/utils
  Configure the import alias for hooks: > $lib/hooks
  Configure the import alias for ui: > $lib/components/ui

Note: the verbatim docs example shows `src/routes/layout.css` as the default placeholder for the CSS file prompt, but `src/app.css` is what `sv add tailwindcss` actually creates and what the docs themselves use elsewhere ("src/app.{p,post}css" example in components-json.md). The init CLI source confirms the prompt accepts whatever path you type and validates it exists, so type `src/app.css` to point at the file that `sv add tailwindcss` just created.

Non-interactive equivalent (if you want to script it):

  pnpm dlx shadcn-svelte@latest init \
    --base-color slate \
    --css src/app.css \
    --lib-alias '$lib' \
    --components-alias '$lib/components' \
    --utils-alias '$lib/utils' \
    --hooks-alias '$lib/hooks' \
    --ui-alias '$lib/components/ui'

C. COMPONENTS_JSON

After init, frontend/components.json will be written with this shape (verbatim per docs/content/components-json.md and the Svelte 5 migration diff):

  {
    "$schema": "https://shadcn-svelte.com/schema.json",
    "tailwind": {
      "css": "src/app.css",
      "baseColor": "slate"
    },
    "aliases": {
      "components": "$lib/components",
      "utils": "$lib/utils",
      "ui": "$lib/components/ui",
      "hooks": "$lib/hooks",
      "lib": "$lib"
    },
    "typescript": true,
    "registry": "https://shadcn-svelte.com/registry"
  }

(Init also persists the chosen preset fields — `style`, `iconLibrary`, `menuColor`, `menuAccent` — derived from the design-system preset prompt; you can let them take defaults.)

D. APP_CSS

After init with base color slate, `src/app.css` will be overwritten by the CLI to contain (a) the Tailwind v4 import, (b) the `tw-animate-css` import, (c) the shadcn-svelte data-state custom variants and animation keyframes from `packages/cli/src/tailwind.css`, (d) the `@theme inline` token mapping, (e) the slate `:root` block, and (f) the slate `.dark` block.

The slate-specific :root and .dark blocks (verbatim from docs/content/theming.md, "Slate" section):

  :root {
    --radius: 0.625rem;
    --background: oklch(1 0 0);
    --foreground: oklch(0.129 0.042 264.695);
    --card: oklch(1 0 0);
    --card-foreground: oklch(0.129 0.042 264.695);
    --popover: oklch(1 0 0);
    --popover-foreground: oklch(0.129 0.042 264.695);
    --primary: oklch(0.208 0.042 265.755);
    --primary-foreground: oklch(0.984 0.003 247.858);
    --secondary: oklch(0.968 0.007 247.896);
    --secondary-foreground: oklch(0.208 0.042 265.755);
    --muted: oklch(0.968 0.007 247.896);
    --muted-foreground: oklch(0.554 0.046 257.417);
    --accent: oklch(0.968 0.007 247.896);
    --accent-foreground: oklch(0.208 0.042 265.755);
    --destructive: oklch(0.577 0.245 27.325);
    --border: oklch(0.929 0.013 255.508);
    --input: oklch(0.929 0.013 255.508);
    --ring: oklch(0.704 0.04 256.788);
    --chart-1: oklch(0.646 0.222 41.116);
    --chart-2: oklch(0.6 0.118 184.704);
    --chart-3: oklch(0.398 0.07 227.392);
    --chart-4: oklch(0.828 0.189 84.429);
    --chart-5: oklch(0.769 0.188 70.08);
    --sidebar: oklch(0.984 0.003 247.858);
    --sidebar-foreground: oklch(0.129 0.042 264.695);
    --sidebar-primary: oklch(0.208 0.042 265.755);
    --sidebar-primary-foreground: oklch(0.984 0.003 247.858);
    --sidebar-accent: oklch(0.968 0.007 247.896);
    --sidebar-accent-foreground: oklch(0.208 0.042 265.755);
    --sidebar-border: oklch(0.929 0.013 255.508);
    --sidebar-ring: oklch(0.704 0.04 256.788);
  }

  .dark {
    --background: oklch(0.129 0.042 264.695);
    --foreground: oklch(0.984 0.003 247.858);
    --card: oklch(0.208 0.042 265.755);
    --card-foreground: oklch(0.984 0.003 247.858);
    --popover: oklch(0.208 0.042 265.755);
    --popover-foreground: oklch(0.984 0.003 247.858);
    --primary: oklch(0.929 0.013 255.508);
    --primary-foreground: oklch(0.208 0.042 265.755);
    --secondary: oklch(0.279 0.041 260.031);
    --secondary-foreground: oklch(0.984 0.003 247.858);
    --muted: oklch(0.279 0.041 260.031);
    --muted-foreground: oklch(0.704 0.04 256.788);
    --accent: oklch(0.279 0.041 260.031);
    --accent-foreground: oklch(0.984 0.003 247.858);
    --destructive: oklch(0.704 0.191 22.216);
    --border: oklch(1 0 0 / 10%);
    --input: oklch(1 0 0 / 15%);
    --ring: oklch(0.551 0.027 264.364);
    --chart-1: oklch(0.488 0.243 264.376);
    --chart-2: oklch(0.696 0.17 162.48);
    --chart-3: oklch(0.769 0.188 70.08);
    --chart-4: oklch(0.627 0.265 303.9);
    --chart-5: oklch(0.645 0.246 16.439);
    --sidebar: oklch(0.208 0.042 265.755);
    --sidebar-foreground: oklch(0.984 0.003 247.858);
    --sidebar-primary: oklch(0.488 0.243 264.376);
    --sidebar-primary-foreground: oklch(0.984 0.003 247.858);
    --sidebar-accent: oklch(0.279 0.041 260.031);
    --sidebar-accent-foreground: oklch(0.984 0.003 247.858);
    --sidebar-border: oklch(1 0 0 / 10%);
    --sidebar-ring: oklch(0.551 0.027 264.364);
  }

These blocks live at the bottom of src/app.css. Above them, the CLI prepends (verbatim from packages/cli/src/tailwind.css plus the standard v4 setup that init writes):

  @import "tailwindcss";
  @import "tw-animate-css";

  @custom-variant dark (&:is(.dark *));

  @theme inline {
    --radius-sm: calc(var(--radius) - 4px);
    --radius-md: calc(var(--radius) - 2px);
    --radius-lg: var(--radius);
    --radius-xl: calc(var(--radius) + 4px);
    --color-background: var(--background);
    --color-foreground: var(--foreground);
    --color-card: var(--card);
    --color-card-foreground: var(--card-foreground);
    --color-popover: var(--popover);
    --color-popover-foreground: var(--popover-foreground);
    --color-primary: var(--primary);
    --color-primary-foreground: var(--primary-foreground);
    --color-secondary: var(--secondary);
    --color-secondary-foreground: var(--secondary-foreground);
    --color-muted: var(--muted);
    --color-muted-foreground: var(--muted-foreground);
    --color-accent: var(--accent);
    --color-accent-foreground: var(--accent-foreground);
    --color-destructive: var(--destructive);
    --color-border: var(--border);
    --color-input: var(--input);
    --color-ring: var(--ring);
    --color-chart-1: var(--chart-1);
    --color-chart-2: var(--chart-2);
    --color-chart-3: var(--chart-3);
    --color-chart-4: var(--chart-4);
    --color-chart-5: var(--chart-5);
    --color-sidebar: var(--sidebar);
    --color-sidebar-foreground: var(--sidebar-foreground);
    --color-sidebar-primary: var(--sidebar-primary);
    --color-sidebar-primary-foreground: var(--sidebar-primary-foreground);
    --color-sidebar-accent: var(--sidebar-accent);
    --color-sidebar-accent-foreground: var(--sidebar-accent-foreground);
    --color-sidebar-border: var(--sidebar-border);
    --color-sidebar-ring: var(--sidebar-ring);
  }

The CLI may additionally include the `@custom-variant data-open`/`data-closed`/`data-checked`/`data-unchecked`/`data-disabled`/`data-active`/`data-horizontal`/`data-vertical` blocks and `@keyframes accordion-down`/`accordion-up` (verbatim from packages/cli/src/tailwind.css) wrapped in `@theme inline`. Once `init` runs, the final on-disk content is authoritative — the CLI overwrites `src/app.css`.

E. VITE_CONFIG

`sv add tailwindcss` (or manual install) writes this into frontend/vite.config.ts. After the change the file will look like (the two load-bearing diffs are the import and the plugin entry):

  import tailwindcss from '@tailwindcss/vite';
  import { sveltekit } from '@sveltejs/kit/vite';
  import { defineConfig } from 'vite';

  export default defineConfig({
    plugins: [tailwindcss(), sveltekit()]
  });

The `tailwindcss()` plugin must precede `sveltekit()` in the `plugins` array. shadcn-svelte init does NOT touch vite.config.ts — that is purely the Tailwind v4 SvelteKit setup, which `sv add tailwindcss` performs for you.

F. LAYOUT_IMPORT

Add this exact line inside `<script>` of src/routes/+layout.svelte (verbatim from docs/content/dark-mode/svelte.md):

  import "../app.css";

A minimal +layout.svelte for runes mode looks like:

  <script lang="ts">
    import "../app.css";
    let { children } = $props();
  </script>

  {@render children?.()}

`sv add tailwindcss` will add the `import "../app.css";` line for you if +layout.svelte already exists; otherwise you create it yourself.

G. DARK_MODE

Step 1 — install mode-watcher (verbatim from docs/content/dark-mode/svelte.md):

  pnpm i mode-watcher

Step 2 — add ModeWatcher to src/routes/+layout.svelte (verbatim from docs/content/dark-mode/svelte.md, runes mode):

  <script lang="ts">
    import "../app.css";
    import { ModeWatcher } from "mode-watcher";
    let { children } = $props();
  </script>

  <ModeWatcher />
  {@render children?.()}

Step 3 — install the icons + the dropdown-menu component used by the toggle:

  pnpm dlx shadcn-svelte@latest add dropdown-menu button

`@lucide/svelte` is auto-installed by shadcn-svelte init/add as a dependency, but if you need it explicitly:

  pnpm add @lucide/svelte

Step 4 — create the toggle component. Two canonical variants exist in the docs registry; for Johnny use the dropdown variant (verbatim from docs/src/lib/registry/examples/dark-mode-dropdown-menu.svelte, with imports rewritten from `$lib/registry/ui/...` to the project's actual ui alias `$lib/components/ui/...`):

  <script lang="ts">
    import SunIcon from "@lucide/svelte/icons/sun";
    import MoonIcon from "@lucide/svelte/icons/moon";

    import { resetMode, setMode } from "mode-watcher";
    import * as DropdownMenu from "$lib/components/ui/dropdown-menu/index.js";
    import { buttonVariants } from "$lib/components/ui/button/index.js";
  </script>

  <DropdownMenu.Root>
    <DropdownMenu.Trigger class={buttonVariants({ variant: "outline", size: "icon" })}>
      <SunIcon
        class="h-[1.2rem] w-[1.2rem] scale-100 rotate-0 !transition-all dark:scale-0 dark:-rotate-90"
      />
      <MoonIcon
        class="absolute h-[1.2rem] w-[1.2rem] scale-0 rotate-90 !transition-all dark:scale-100 dark:rotate-0"
      />
      <span class="sr-only">Toggle theme</span>
    </DropdownMenu.Trigger>
    <DropdownMenu.Content align="end">
      <DropdownMenu.Item onclick={() => setMode("light")}>Light</DropdownMenu.Item>
      <DropdownMenu.Item onclick={() => setMode("dark")}>Dark</DropdownMenu.Item>
      <DropdownMenu.Item onclick={() => resetMode()}>System</DropdownMenu.Item>
    </DropdownMenu.Content>
  </DropdownMenu.Root>

Simpler "light switch" variant (verbatim from docs/src/lib/registry/examples/dark-mode-light-switch.svelte):

  <script lang="ts">
    import SunIcon from "@lucide/svelte/icons/sun";
    import MoonIcon from "@lucide/svelte/icons/moon";

    import { toggleMode } from "mode-watcher";
    import { Button } from "$lib/components/ui/button/index.js";
  </script>

  <Button onclick={toggleMode} variant="outline" size="icon">
    <SunIcon
      class="h-[1.2rem] w-[1.2rem] scale-100 rotate-0 !transition-all dark:scale-0 dark:-rotate-90"
    />
    <MoonIcon
      class="absolute h-[1.2rem] w-[1.2rem] scale-0 rotate-90 !transition-all dark:scale-100 dark:rotate-0"
    />
    <span class="sr-only">Toggle theme</span>
  </Button>

mode-watcher writes the `.dark` class onto `<html>` (via ModeWatcher), which activates the `.dark` CSS variable block in src/app.css from section D.

H. ADD_COMPONENT

Exact syntax (verbatim from docs/content/cli.md):

  pnpm dlx shadcn-svelte@latest add [component]

Examples:

  pnpm dlx shadcn-svelte@latest add button
  pnpm dlx shadcn-svelte@latest add card
  pnpm dlx shadcn-svelte@latest add dropdown-menu
  pnpm dlx shadcn-svelte@latest add button card dropdown-menu input label dialog

Flags:

  -c, --cwd <path>   the working directory
  --no-deps          skip adding & installing package dependencies
  --skip-preflight   ignore preflight checks and continue
  -a, --all          install all components
  -y, --yes          skip confirmation prompt
  -o, --overwrite    overwrite existing files
  --proxy <proxy>    fetch components from registry using a proxy

Where files land — given Johnny's `ui` alias of `$lib/components/ui`, each component scaffolds into a folder under `frontend/src/lib/components/ui/<component-name>/` with at minimum an `index.ts` barrel and one or more `.svelte` files. For example, `add button` writes:

  frontend/src/lib/components/ui/button/button.svelte
  frontend/src/lib/components/ui/button/index.ts

And `add dropdown-menu` writes the cluster of part files (Root/Trigger/Content/Item/Sub/Separator/Shortcut/CheckboxItem/RadioGroup/etc.) plus an `index.ts`. Import via:

  import { Button } from "$lib/components/ui/button/index.js";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu/index.js";

(Verbatim import shape from docs/content/installation/sveltekit.md and the dark-mode examples.)

The add command also resolves transitive deps automatically: e.g. running `add dropdown-menu` will pull in `bits-ui` and `@lucide/svelte` if not already installed.

I. SVELTE5_NOTES

Confirmed Svelte-5-specific facts from the canonical docs that matter when shadcn-svelte is forced into runes mode (compilerOptions.runes = true in svelte.config.js):

1. Props are runes-based. All generated components use `let { ... } = $props()` instead of `export let`. The dark-mode examples and the +layout.svelte from docs/content/dark-mode/svelte.md both use `let { children } = $props();`.

2. Slots are replaced by snippets. The root layout uses `{@render children?.()}` instead of `<slot />`. shadcn-svelte components accept `children` (and named snippet props) as snippet props, not as slots — so consumers must use `{#snippet ...}` / `{@render ...}`, never `<svelte:fragment slot="...">`.

3. `$bindable()` is used wherever shadcn-svelte components expose two-way bindable state (e.g. `open`, `value`, `checked`). When wrapping components, propagate via `let { open = $bindable(false), ... } = $props();`.

4. Event handlers use the new property syntax (`onclick={...}`, `onopenchange={...}`), NOT `on:click` — the verbatim dark-mode-dropdown-menu.svelte example uses `onclick={() => setMode("light")}`. This is mandatory under runes mode and matches what shadcn-svelte scaffolds.

5. The utility/types file emitted by init (verbatim from docs/content/migration/svelte-5.md, "Update utils.ts") is the runes-friendly version exporting just `cn` plus four helper types:

   import { type ClassValue, clsx } from "clsx";
   import { twMerge } from "tailwind-merge";

   export function cn(...inputs: ClassValue[]) {
     return twMerge(clsx(inputs));
   }

   export type WithoutChild<T> = T extends { child?: any } ? Omit<T, "child"> : T;
   export type WithoutChildren<T> = T extends { children?: any } ? Omit<T, "children"> : T;
   export type WithoutChildrenOrChild<T> = WithoutChildren<WithoutChild<T>>;
   export type WithElementRef<T, U extends HTMLElement = HTMLElement> = T & { ref?: U | null };

   `WithElementRef` reflects the runes-era pattern where components expose `ref` as bindable instead of `use:action` for ref access — fully compatible with `compilerOptions.runes = true`.

6. Dependency versions confirmed Svelte-5/runes-compatible by the migration guide: `bits-ui` ^1.0.0, `svelte-sonner` ^1.0.0, `@lucide/svelte` ^0.482.0, `paneforge` ^1.0.0-next.5, `vaul-svelte` ^1.0.0-next.7, `mode-watcher` ^1.0.0. The migration guide explicitly deprecates `cmdk-sv` (use `bits-ui` Command), `svelte-headless-table` (use `@tanstack/table-core`), `svelte-radix` and `lucide-svelte` (both replaced by `@lucide/svelte`). Do NOT install any of the deprecated packages — they will break under runes mode.

7. The `legacy.componentApi` / `accessors` options are not used by shadcn-svelte and should be left default. Forcing `compilerOptions.runes = true` in svelte.config.js is supported — every shadcn-svelte component on the v1+ registry is authored in runes mode, so global runes enforcement causes no conflicts with the scaffolded code.

8. Caveat: any third-party Svelte code in Johnny that is still legacy (non-runes) will fail to compile once `runes: true` is global. shadcn-svelte itself is runes-clean, but verify your other deps before flipping the flag.

Sources fetched:
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/docs/content/installation/sveltekit.md
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/docs/content/components-json.md
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/docs/content/theming.md (full Slate block)
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/docs/content/dark-mode/svelte.md
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/docs/content/cli.md
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/docs/content/migration/svelte-5.md
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/docs/src/lib/registry/examples/dark-mode-dropdown-menu.svelte
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/docs/src/lib/registry/examples/dark-mode-light-switch.svelte
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/packages/cli/src/commands/init/index.ts (prompt source)
- https://raw.githubusercontent.com/huntabyte/shadcn-svelte/main/packages/cli/src/tailwind.css (base CSS template)
```