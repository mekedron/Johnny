# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Playground group-start contract (trt.48/64)**: `StartBrowserGroupPayload.agents[].context` is the per-member brief; omitted = inherit the group-level `context`. Server resolves it at `backend/app/api/browser_session_groups.py:380` and stamps the RESOLVED brief into each member session's `playground_overrides.context` — assert isolation there (GET `/sessions/<id>`), not just in the request payload.
- **Controller unit tests run without the Svelte compiler**: `playgroundController.test.ts` installs an identity `$state` shim on `globalThis` before importing the `.svelte.ts` controller — class rune fields become plain properties. Reactivity itself is covered only by chrome-devtools browser validation, so any new `$state` field needs a browser pass too.
- **chrome-devtools `evaluate_script` can wedge ("No page found") while snapshot/click/navigate keep working** — even after `select_page`. Workaround: hit the backend API with `curl` from the shell for JSON assertions and keep UI checks snapshot-based.

---

## 2026-06-12 - Johnny-trt.64
- Playground multi-agent start: per-agent context briefs in the UI (playground form only; meeting page untouched per scope).
  - `frontend/src/lib/playground/playgroundSession.svelte.ts`: new `agentContexts: Record<number, string>` state + `setAgentContext()`; `startGroup()` now sends `members[].context` (trimmed; blank → omitted → server inherits the shared field). Single-agent path byte-identical.
  - `frontend/src/lib/components/playground/SetupForm.svelte`: each checked roster row in group mode (2+ selected) grows an optional per-agent textarea (`data-testid="playground-agent-context-<id>"`); roster + shared-Context hints explain blank = inherit; single-agent form unchanged.
  - `frontend/src/lib/playground/playgroundController.test.ts`: payload test — filled brief trimmed into `members[].context`, blank omitted, unselected agent's stored brief never leaks, group-level context intact.
- Quality: 113/113 vitest, svelte-check 0/0, eslint clean on changed files (one PRE-EXISTING `no-undef` in `settings/+page.svelte`, untouched).
- Browser-validated (chrome-devtools, artifacts in `.validation/Johnny-trt.64/01-07`): group-start payload asserted (`agents[0].context` = IT brief, `agents[1]` omitted, group `context` = marketing brief); sessions 71/72 each persisted ONLY their own resolved brief; live BLUEFALCON/REDPANDA rehearsal — Johnny answered Jenkins/BLUEFALCON, Echo B answered REDPANDA/Q3-newsletter from the inherited shared brief and explicitly didn't know Jenkins; no console errors.
- **Learnings:**
  - The inherit semantics are server-side (`entry.context ?? payload.context` → stored in `playground_overrides.context`), so the strongest isolation assertion is each member session's persisted record, not the UI.
  - The decision-pipeline "View router prompt" deliberately excludes the brief (progressive disclosure) — don't expect the context brief there; it reaches the answer LLM only.
  - In a group, a question addressed to one agent can still be answered by the other FROM ITS OWN brief (router redirect) — that's correct behavior and actually stronger isolation evidence.
---

