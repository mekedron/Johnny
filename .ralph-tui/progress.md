# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

- **Scripted fake-mic playground runs interact with trt.9 client auto
  barge-in**: launch the shared Chrome with `CHROME_EXTRA_FLAGS="--use-fake-device-for-media-stream
  --use-fake-ui-for-media-stream --use-file-for-fake-audio-capture=<wav>
  --disable-features=AudioServiceSandbox"` (pkill + re-run
  `./scripts/start-chrome.sh`; restore by pkill + re-run with env unset).
  The WAV loops per getUserMedia stream — mute the mic before the loop
  point or loop rows pollute the run. With replies enabled, any utterance
  starting while the bot still speaks fires client barge-in and the turn
  terminalizes `no_reply(barge_in)` (outcome `suppressed`) — judge VAD/turn
  integrity by `transcript_chunks` rows, not by decision outcomes, or use
  gaps longer than the longest reply.
- **Gate/ledger terminal evidence lives in `agent_decisions`
  (`terminal_state`, `no_reply_reason` columns)** — docker logs swallow the
  gate's INFO breadcrumbs (the known module-local-handler gap), so query
  the table for per-turn verdict/terminal forensics.

---

## 2026-06-10 - Johnny-trt.5
- Endpointing knobs. Verification + completion pass: the implementation was
  already on HEAD (committed under the trt.9 commit b6e33f65 by a prior
  cut-off iteration) — `load_vad(min_silence_duration=...)` passthrough,
  `build_agent_session(endpointing=...)` → factored `build_turn_handling`
  (omits the key when None so LiveKit's 0.5/3.0 defaults stand; room path
  passes nothing), browser-scoped `load_browser_vad()` with
  `BROWSER_VAD_MIN_SILENCE_DURATION_S = 0.40` riding `_shared_vad()`, and
  harness `--vad-min-silence-s` for A/B. Worker/Meet path verified
  untouched (worker.py prewarm calls bare `load_vad()`, no endpointing).
- Verified this iteration: 23/23 unit tests in-image
  (test_session_endpointing.py + browser-session VAD pins + harness label
  test); ruff lint+format clean on all 6 touched files; prior iteration's
  A/B artifacts check out (warm vad_end p50 562.4 → 401.3 ms = **161 ms
  earlier**, within the ~150±50 ms acceptance; 24/24 turns replied both
  floors; 20-turn varied-pause script all single-utterance at 0.40 s).
- Completed the missing test-plan item: chrome-devtools playground run
  (session 82, prod stack, Parakeet+Ollama+Piper) with the prepared
  fake_mic_pauses.wav (8 utterances, mid-sentence hesitations 0.20–0.35 s,
  several at the 0.35 s edge): 8 first-pass utterances → exactly 8 user
  transcript rows, every hesitation rode out, zero premature commits
  (loop replay row #9 also single). All 9 turns should_speak=true and
  replied; the 8 first-pass replies were cut by the NEXT utterance via
  trt.9 auto barge-in (no_reply_reason=barge_in — by design); final turn
  spoken to completion after mic mute. Console clean. Screenshot +
  DB-row dump under .validation/Johnny-trt.5/ (05-*.png, 06-*.txt).
- Files changed this iteration: docs/LATENCY.md (measured A/B numbers added
  to the endpointing-knobs paragraph), .ralph-tui/progress.md.
- **Learnings:**
  - Patterns discovered: fake-mic WAV loops per gUM stream + auto barge-in
    cuts long replies on scripted runs (see Codebase Patterns);
    agent_decisions.terminal_state/no_reply_reason are the queryable gate
    forensics (docker logs swallow gate INFO lines).
  - Gotchas: STT may hyphenate words ("stand-ups") — don't key automation
    exit conditions on exact substrings of expected transcripts; the
    engine had folded this bead's code into the trt.9 commit, so always
    check `git log -S` for the symbols before re-implementing a bead.
---
