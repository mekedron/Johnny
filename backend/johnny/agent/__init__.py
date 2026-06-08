"""Johnny — LiveKit Agents orchestration package (epic Johnny-7g5).

This package hosts the ``AgentSession``-based voice orchestration that is
replacing the hand-rolled ``johnny/voice_pipeline/pipeline.py`` engine:

* :mod:`johnny.agent.session` — the ``AgentSession`` harness
  (:func:`~johnny.agent.session.build_agent_session`) and
  :class:`~johnny.agent.session.JohnnyAgent`, the ``livekit.agents.Agent``
  subclass that carries Johnny's assembled instructions/personality and
  rehydrates prior transcript history into the LiveKit ``chat_ctx`` on respawn
  (Johnny-re2), with the router "should-speak" gate landing in a later phase.
* :mod:`johnny.agent.adapters` — the provider adapter layer that wraps
  Johnny's ``STTProvider`` / ``LLMProvider`` / ``TTSProvider`` ABCs as
  LiveKit ``stt.STT`` / ``llm.LLM`` / ``tts.TTS`` plugins (Phase 1).
* :mod:`johnny.agent.gate` — the bounded router-gate harness (timeout +
  barge-in cancellation; spike Johnny-9k2) plus the session-scoped
  :class:`~johnny.agent.gate.TurnLedger` that guarantees INV-1 — exactly one
  terminal per LiveKit turn id across the gate and the reply done-callback
  (spike Johnny-o3z). Stdlib-only and ``livekit``-free.
* :mod:`johnny.agent.router_gate` — the router "should-speak" decision that
  runs inside ``Agent.on_user_turn_completed`` (Johnny-xpa): builds the router
  prompt, calls Johnny's router ``LLMProvider`` through the gate harness, and
  raises ``StopResponse`` on no-speak / low-confidence / rate-limited. Requires
  the ``agent`` extra; imported only by :mod:`johnny.agent.session`.
* :mod:`johnny.agent.barge_in` — the slow out-of-band barge-in intent
  classifier (Johnny-k8t): asks Johnny's router ``LLMProvider`` whether the
  latest speech is an actionable interruption and calls LiveKit's interrupt API
  for ``stop`` / ``correct`` / ``new_question`` only, guarded against stale
  verdicts by the live reply ``SpeechHandle`` (the LiveKit-turn-keyed analogue
  of the legacy generation counter). The fast VAD-onset interrupt itself is
  LiveKit-native (configured in :func:`~johnny.agent.session.build_agent_session`).
  ``livekit``-free; imported only by :mod:`johnny.agent.session`.

* :mod:`johnny.agent.job_config` — the per-session agent **job-payload
  contract** (:class:`~johnny.agent.job_config.SessionJobConfig`): the
  serialisable description of one Meet session (providers, personality, mode,
  instructions, approval/redis wiring) that the API dispatches to the agent
  worker as room/dispatch metadata, mirroring the legacy ``JOHNNY_*`` launcher
  env contract (spike Johnny-y4j; consumed by Johnny-7we). Stdlib-only.
* :mod:`johnny.agent.room_auth` — per-room JWT minting for the meet-worker
  bridge and (non-framework) agent participants (spike Johnny-y4j). Lazy
  ``livekit.api`` import.
* :mod:`johnny.agent.dispatch` — explicit one-room-per-session agent dispatch
  (``api.AgentDispatch``) carrying the :class:`SessionJobConfig` metadata
  (spike Johnny-y4j). Lazy ``livekit.api`` import.

Importing this top-level package is intentionally side-effect free and
does **not** import ``livekit`` — only the submodules that subclass the
LiveKit SDK pull it in, mirroring the lazy-import discipline of
``johnny/voice_pipeline/livekit_transport.py``. That keeps ``import
johnny.agent`` cheap and safe in images/tests where the ``agent`` extra
(``livekit-agents``) is absent; the SDK-backed pieces live behind explicit
submodule imports (``from johnny.agent.session import build_agent_session``).
The job-payload contract (:class:`SessionJobConfig`) is stdlib-only, so it is
re-exported here for convenience.
"""

from __future__ import annotations

from johnny.agent.job_config import (
    SessionJobConfig,
    agent_identity_for_session,
    bridge_identity_for_session,
    room_name_for_session,
)

__all__: list[str] = [
    "SessionJobConfig",
    "agent_identity_for_session",
    "bridge_identity_for_session",
    "room_name_for_session",
]
