"""Browser e2e for the personality library (Johnny-oly.7, part B).

This module is the home for the **chrome-devtools MCP** browser matrix the bead
calls for. Unlike the CDP harness in ``tests/e2e/providers_ui`` (which drives
Chrome over a raw CDP socket from inside pytest), the personality browser flow
is **agent-driven**: the operator's Claude Code session loads the
``mcp__chrome-devtools__*`` tools and walks the real UI, exactly as Johnny-oly.4
and .6 validated their surfaces. There is no automated pytest harness for the
MCP flow yet (see the follow-up bead noted below), so the single test here is a
deliberate, documented ``skip`` that records the validated steps + the re-run
recipe. It is also gated behind the ``e2e_ui`` marker, so a default
``uv run pytest`` never touches it.

The chain this matrix proves end to end (operator clicks → bot uses the preset):

    create → preview voice (audio plays) → save → clone → edit → set-default
    (badge moves) → assign on a calendar event → delete non-default → refuse
    delete of the default → restore.

----------------------------------------------------------------------------
Validated run — 2026-06-08, chrome-devtools MCP against the live Compose stack
(http://localhost:5173 → api http://localhost:8000). Screenshots saved under
the gitignored ``.validation/personalities/`` (NN-*.png), referenced as
local-path notes for the reviewer (never committed, per the CLAUDE.md rule):

  1. /personalities renders with the bootstrap "Johnny" default; its Delete +
     Set-default are disabled (single-default invariant).        01-*.png
  2. "New personality" → name "E2E Matrix Bot", description, LLM "Google
     Gemini", TTS "Local Piper"; the unified VoicePicker loaded the Piper
     catalog.                                                     02-*.png
     - Preview on ``ar_JO-kareem-low`` → POST /providers/3/play_sample → 200,
       Content-Type ``audio/wav``, 141356 bytes, header ``x-tts-audible: 1``
       (4416 ms of audible audio), then the browser fetched the blob URL with
       a 206 range request — i.e. AUDIO ACTUALLY PLAYS, not just a 200.
     - "Use" pinned the voice; "Create personality" → POST /personalities 201.
  3. The new card shows LLM "Google Gemini · gemini-2.5-flash", TTS "Local
     Piper · ar_JO-kareem-low" (pinned voice round-tripped via
     metadata.tts_options.voice_id), mode "Suggest only".        03-*.png
  4. Clone → POST /personalities/5/clone 201; the editor opened on the copy
     ("E2E Matrix Bot (copy)"); edited the description → Save → PATCH
     /personalities/6 200 (the COPY, id=6 — source id=5 untouched). 04-*.png
  5. Set the copy as default → list reordered (default first), the "Default"
     badge MOVED off Johnny onto the copy, and the delete-protection (disabled
     Delete + tooltip) followed the new default.                  05-*.png
  6. Calendar → opened the "IT meeting" event → the Personality picker listed
     all personalities and selected "E2E Matrix Bot"; the form binding works.
     NOTE: deliberately did NOT click "Enable Johnny" — that would create a
     real meeting config and make Johnny join a colleague's live Meet. The
     PUT-persistence of ``meeting_configs.personality_id`` is already browser-
     validated by Johnny-oly.6 (PUT body personality_id, reload-persisted) and
     re-asserted at the API level by the integration test
     ``test_meeting_personality_used_without_request``.           06-*.png
  7. Delete the default → REFUSED at the UI: the Delete button is disabled with
     the message "Set another personality as default before deleting this one"
     (the backend 409 is covered by
     ``test_delete_default_personality_refused_409_but_non_default_deletes``).
                                                                  07-*.png
  8. Delete the non-default "E2E Matrix Bot" → confirm dialog → DELETE
     /personalities/5 → gone. Reverted the default to Johnny, deleted the copy,
     leaving the operator DB at exactly one row (the bootstrap "Johnny",
     is_default=true) — fully restored.                           08-*.png

In-session character badge (the one cell not re-driven here): a started session
shows "Character: <name>" on the sidebar / live chip / detail panel + history
``bot_name``. Johnny-oly.6 validated this live (started a playground session →
POST personality_id → response personality_name → all three surfaces showed the
name + the read-only detail panel). It is re-asserted server-side by
``tests/integration/test_personality_e2e.py`` (bot_name + the
playground_overrides snapshot). It was intentionally NOT re-run in this pass:
two live browser sessions were present and ``SINGLE_SESSION_POLICY="reject"``
would 409 a new start (and reaping would disturb the operator's sessions).

----------------------------------------------------------------------------
Re-run recipe (manual, agent-driven):

  1. ``./run-dev.sh`` (or ``./run.sh``) to bring the stack up; ``./scripts/
     start-chrome.sh`` for the shared Chrome on 127.0.0.1:9222.
  2. In a Claude Code session, load the chrome-devtools MCP tools and walk
     steps 1-8 above, asserting each network call / DOM state and writing
     screenshots to ``.validation/personalities/NN-*.png``.
  3. Clean up: delete any test personalities and revert the default to
     "Johnny" so the operator DB returns to the bootstrap row.

CI: there is no chrome-devtools MCP CI stage today (the only workflow is the
Pages docs deploy). Automating this browser matrix as a headless CDP harness
(mirroring ``tests/e2e/providers_ui``) is tracked as a follow-up bead; until it
lands, this matrix is a manual gate run before the epic closes.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e_ui


def test_personalities_browser_matrix_is_agent_driven() -> None:
    """Placeholder that documents the agent-driven chrome-devtools MCP matrix.

    Skipped on purpose: the flow is walked by a Claude Code session driving the
    ``mcp__chrome-devtools__*`` tools (see the module docstring for the full
    validated trace + re-run recipe), not by an automated CDP harness. When the
    headless harness follow-up lands, replace this with the real driver.
    """
    pytest.skip(
        "Agent-driven chrome-devtools MCP flow — run manually per the module "
        "docstring (validated 2026-06-08; artifacts in .validation/personalities/)."
    )
