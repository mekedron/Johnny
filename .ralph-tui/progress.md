# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Playground end-to-end validation method** (proven trt.25/55/58/26): typed asks
  with mic muted; raw in-page WS capture via `evaluate_script` opening
  `ws://localhost:8000/ws/sessions/<id>` into `window.__allFrames` (needs explicit
  `pageId`); gog auth toggled by moving `~/.johnny/sandbox-home/.local/share/gogcli/keyring`
  aside/back (restore = `rm -rf` the empty shell gog recreates FIRST; the first
  `check.sh` read after restore is transiently exit 2 — re-run to confirm 0);
  per-attempt cost cut from ~90s to ~10s by clicking Interrupt after a `speak`
  verdict instead of waiting out TTS playback; verify state via
  `docker compose exec skills-sandbox bash /skills/google-calendar/check.sh`.
- **trt.55 capability-snapshot lifecycle is the failure-path lever**: the session
  catalog freezes availability at assembly; the worker re-derives it at claim.
  Break the link AFTER session start to get ack→failed-task (claim-time
  revalidation); break it BEFORE session start to get the honest no-task decline.
- **llama3.2:3b delegate stochasticity**: the 3B router frequently fills
  `task{kind, ack}` in raw_output while emitting `action: "speak"` (one token from
  delegating), then the answer model fabricates results in-persona. Natural-ask
  delegate rate can run 0/8; an explicit in-conversation instruction naming the
  task kind flips it reliably. Check `agent_decisions.raw_output` before
  suspecting the gate/catalog (gate degrades stamp markers next to
  CAPABILITY_GAP_KEY; their absence = the model's own verdict).

---


## 2026-06-12 - Johnny-trt.26
- Phase-4 capstone validated end-to-end in the playground (chrome-devtools MCP,
  validation-only chore — zero source changes). Run A (session #45, agent_tasks
  48): ask → delegate → router-authored ack spoken → worker claim +16ms (wake
  ping) → REAL gog run in the skills-sandbox → done +1.03s with speech-ready
  result_text (3 real calendar events) → task_queued/task_progress/task_completed
  frames captured on the live session WS → result NOT spoken (Phase-5 boundary
  held). Run B (session #46, agent_tasks 49): snapshot taken linked → keyring
  broken mid-session → ack → claim-time revalidation failed the task +152ms with
  the skill-authored actionable copy → task_completed(status=failed) frame →
  trt.53 correction spoken (agent_spoke kind=correction). The claim-time-break
  leg trt.55 couldn't finish live is now live-validated.
- Ack ≤1.5s bar NOT met on the canonical local trio (felt 2.67s, 89% = the 3B
  triage call, prompt_chars 4484) — same verdict + attribution as the Phase-3
  capstone; Phase-4 mechanics add ~0ms. Levers stay trt.41/42/51.
- Files changed: none (validation artifacts under .validation/Johnny-trt.26/
  only: 00-RUN-NOTES.md + 11 captures; progress.md patterns above).
- **Learnings:**
  - The capstone's "unlinked → ack then failed task" wording predates trt.55;
    post-trt.55 that behavior exists only via the claim-time break (snapshot
    frozen available). Session-start-unlinked = honest decline, no task row
    (validated in trt.55). Both documented in the run notes.
  - Worker's claim-time registry rebuild (kind-not-ready → refresh → settle
    failed without exec, error "skill unavailable at session snapshot") is the
    path a mid-session credential break actually takes — check.sh inside the
    runner never even runs. Same graceful copy either way.
  - 0/8 natural-phrasing delegate verdicts under the default cyberpunk persona
    (raw_output one token from delegate every time) — far worse than trt.21's
    measured 12.5%; quantitative fuel for trt.41/42 per-agent triage model.
  - gog auth list works on the first call after keyring restore but check.sh
    can transiently exit 2 once — always re-run before trusting the state.
---
