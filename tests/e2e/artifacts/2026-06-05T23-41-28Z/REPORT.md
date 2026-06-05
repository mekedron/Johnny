# Johnny-pdf — End-to-end validation: readiness audit and blocker report

**Run ID:** 2026-06-05T23-41-28Z
**Bead:** [Johnny-pdf](../../../../.beads/) — master functional validation across all four modes
**Outcome:** Phase 0 PARTIAL · Phases 1-10 BLOCKED · Phase 11 PARTIAL (this artifact)

## Why the full pass did not run

The bead description states explicitly:

> "After Johnny-ckz Part A (join-stuck bug fix) lands — without it, every phase past Phase 3 will fail."
> "Blocked-by in practice: Johnny-ckz Part A (join-stuck bug fix) — every phase from 3 onward depends on the fix landing"

Johnny-ckz is still OPEN. In addition, this agent session encountered five further hard blockers that prevent the lived journey from running unattended:

| # | Blocker | Impact |
|---|---|---|
| 1 | `GET /providers` returns `{"stt":[],"llm":[],"tts":[]}` | No transcription, no decisions, no speech, regardless of join success |
| 2 | Johnny-ckz Part A not landed | `bot_sessions.status` stuck on `joining` per parent epic |
| 3 | Johnny-ckz Part B (`uv run python -m johnny.e2e --mode=<mode>`) not built | No automated way to run the 4 modes back-to-back; `backend/johnny/e2e` does not exist |
| 4 | `nikita.rabykin@gmail.com` not connected | Phase 10 (identity switching) impossible |
| 5 | No human observer in this agent session | Phases 4-7 need spoken/played audio in the live Meet |

## Phase 0 — what passed and what didn't

| Criterion | Result | Notes |
|---|---|---|
| Docker stack healthy | PASS | `docker_ps.txt` — `johnny-{api,frontend,postgres,redis}` all `Up (healthy)`. |
| Bot account `nikita.rabykin@aikamatkat.fi` connected, role=bot, token decryptable | PASS (warn) | `auth_accounts.json` — token_expires_at=2026-06-05T23:45:28Z, expiry within minutes of run start; refresh required before any real join. |
| Observer `nikita.rabykin@gmail.com` authenticated in second browser profile | FAIL | Not connected to the app. |
| Test calendar event present, both accounts invited, Meet link generated | PASS (warn) | `calendar_events.json` — event id=11, meet_link `bpa-ocqr-inn`. Organizer is the bot account, not the user (bead convention violation). Start time `2026-06-06T02:15:00Z` is in the past for this run window. |
| Active STT, LLM, TTS providers | FAIL | All three lists empty in API and UI. |
| Profile template with allowed_replies (>=3) | PASS | Template 4 "Demo template" has 3 allowed replies (Yes. / No. / Could you clarify?). |

## Phases 1-10 — blocked

All ten phases are blocked transitively on one or more of the hard blockers above. Concretely:

- **Phases 1-2** depend on a working /providers screen with at least one active per kind, AND on the join scheduler being able to spawn meet-worker without the join-stuck bug.
- **Phase 3** depends on Johnny-ckz Part A (join-stuck bug fix).
- **Phases 4-7** depend on Part A + active STT/LLM/TTS + a live audio source (human observer or built-in injector).
- **Phase 8** depends on at least one session reaching `joined` first.
- **Phase 9** depends on at least one completed session existing in `bot_sessions` / `transcript_chunks` / `agent_decisions` / `agent_utterances`.
- **Phase 10** depends on the observer account being connected with role=user.

## Phase 11 — partial evidence package (this artifact)

- `report.json` — structured PASS/FAIL roll-up + blocker list, same schema as the Johnny-upg artifacts.
- `phase-0/calendar.png`, `phase-0/providers.png`, `phase-0/templates.png` — UI baseline.
- `phase-0/*.json` — raw API state for /auth/google/accounts, /providers, /templates, /calendar/events, /sessions/active, /calendar/events/11/meeting-config, /health.
- `phase-0/docker_ps.txt` — container health snapshot.

Empty `phase-1/` through `phase-10/` directories are created so that the next (unblocked) run can drop screenshots into the standard layout without reorganizing.

## Next-action checklist

1. Land Johnny-ckz Part A — structured join-stage logging and root-cause fix.
2. Build Johnny-ckz Part B — `uv run python -m johnny.e2e --mode=<mode>` plus an audio injector (second Playwright participant playing fixture WAV is the bead-recommended approach).
3. Seed providers (one STT + one LLM + one TTS active) via the Johnny-upg harness which already drives this end-to-end.
4. Connect `nikita.rabykin@gmail.com` and re-create the Test event from that account so the organizer matches the bead convention.
5. Refresh `nikita.rabykin@aikamatkat.fi` token if expired.
6. Re-run Johnny-pdf — the full pass should then take 30-45 min observer-driven or 10-15 min harness-driven.
