# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### chrome-devtools MCP validation pattern (Johnny project top rule)

The project's CLAUDE.md mandates real-browser validation via the
**chrome-devtools MCP** for every UI-visible change. Pattern for any
validation task:

1. `./scripts/start-chrome.sh` (idempotent — runs only if Chrome isn't already attached at `127.0.0.1:9222`).
2. `ToolSearch select:mcp__chrome-devtools__{list_pages,new_page,navigate_page,take_snapshot,take_screenshot,click,fill,evaluate_script,list_console_messages,list_network_requests,wait_for}` (and others as needed) — schemas are deferred and must be loaded before calling.
3. Screenshots / artifacts must go to a path within the workspace root (e.g. `.validation-ckz10-artifacts/`) — `/tmp` is rejected with "Access denied: path is not within any of the workspace roots".
4. NEVER use `claude-in-chrome` MCP. Project rule + global rule in `/Users/nikita/.claude/rules/common/browser-automation.md`.
5. After each chrome-devtools tool call the `selected` page can be reset to the default — use `pageId` consistently on every subsequent call, OR call `select_page` once and trust it. Most failures of `evaluate_script` returning "No page found" come from a stale page selection.

### Beads issue tracking (NO TaskCreate / TodoWrite in this project)

`CLAUDE.md` is explicit: use `bd` for all task tracking. Even when the
harness gently reminds you to use TaskCreate, ignore it for this project.

- New issue: `bd create --title="…" --description="…" --type=bug|task|feature --priority=0..4 --db /Users/nikita/Projects/Johnny/.beads/beads.db`
- Link to epic: `bd update <id> --parent <epic-id> --db …`
- Add notes (long-form, supports markdown + `[[wiki-links]]` to other issues): `bd update <id> --notes "$(cat …)" --db …`
- Close: `bd close <id> --reason "…" --db …`
- The wiki-link target is the issue's `description` slug; `bd` resolves them lazily so a `[[link]]` to a not-yet-existing issue is fine.

### Playground architecture (for future validation runs)

- `/playground` page is configuration + start. After Start the live UI re-uses the same panel; the per-session detail page is at `/sessions/{id}` and offers a "Reopen playground" link back to `/playground?session={id}`.
- Audio plumbing lives in `frontend/src/lib/browserAudio.ts` — it opens a WebSocket to `/ws/sessions/{id}/audio`, captures mic via `getUserMedia` → `AudioWorkletNode` → 16 kHz PCM frames as binary WS messages, and plays incoming binary frames through an `AudioContext` + `GainNode`. **At time of Johnny-ckz.10 validation, the page never actually called `startBrowserAudioSession` — see Johnny-c8t.**
- BROWSER badge on active-sessions sidebar is the visual marker that distinguishes playground sessions from `source: meet` sessions.

---

## 2026-06-06 - Johnny-ckz.10

### What was done

Pure verification (no code changed). Drove `/playground`, `/sessions/{id}`, `/calendar` (Try with bot button), and the sidebar through chrome-devtools MCP to validate the 8 acceptance bullets from Johnny-ckz.6. Captured screenshots to `.validation-ckz10-artifacts/`. Filed 4 follow-up bugs under epic Johnny-ckz for the gaps. Wrote a per-check verdict trace into Johnny-ckz.10's notes.

### Files changed

- No application code changed.
- `.ralph-tui/progress.md` (this file) updated with the reusable patterns above.
- `.validation-ckz10-artifacts/*.png` — 6 screenshots from the chrome-devtools MCP trace.
- 4 new beads filed: Johnny-c8t (P0), Johnny-31g (P1), Johnny-klh (P1), Johnny-wyd (P1).

### Learnings

- The configuration UI half of Johnny-ckz.6 / Johnny-ckz.11 is well-built (mode picker, template picker, persona, advanced disclosure with per-session STT/LLM/TTS overrides, volume slider, mute mic/speaker buttons, BROWSER badge, Reopen playground link). The plumbing half (WebSocket audio path, conversation reply loop) is **completely missing or silently failing** despite the "audio ready" badge in the UI.
- `browserAudio.ts` exists and looks correct in isolation, but the playground page never wires it up — none of the 113 network requests over a 3-minute session is a WebSocket.
- faster-whisper hallucinates aggressively on silence (`Does Olam A.P.I.`, Welsh-shaped strings, `. . . .`). These are getting saved into `bot_sessions.transcripts` with `speaker = NULL` and pollute the conversation feed.
- The barge-in classifier shares the user's "active" LLM choice — which in this stack is a 35B Qwen model that times out 5×/session under `httpx`'s default read timeout. Worker logs are noisy and every timeout is a silently-missed interrupt.
- "Try with bot" on a calendar event whose times are nonsensical (`end < start`) can silently trigger an auto-spawn of a real `meet-worker-session-{id}` container that fails on Meet's "Join now" selector. Out of scope for Johnny-ckz.6 but worth tracking if it recurs.
- Per-check timing: chrome-devtools MCP `click` + `wait_for` round-trip adds noticeable wall-clock overhead (3-10 s in practice). Cannot use wall-clock alone to assert sub-3s budgets — instrument the page directly or use the network response timing.

---
