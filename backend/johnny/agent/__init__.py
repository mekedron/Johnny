"""Johnny — LiveKit Agents orchestration package (epic Johnny-7g5).

This package hosts the ``AgentSession``-based voice orchestration that is
replacing the hand-rolled ``johnny/voice_pipeline/pipeline.py`` engine:

* :mod:`johnny.agent.session` — the ``AgentSession`` harness
  (:func:`~johnny.agent.session.build_agent_session`) and
  :class:`~johnny.agent.session.JohnnyAgent`, the ``livekit.agents.Agent``
  subclass that carries Johnny's instructions/personality and (in later
  phases) the router "should-speak" gate.
* :mod:`johnny.agent.adapters` — the provider adapter layer that wraps
  Johnny's ``STTProvider`` / ``LLMProvider`` / ``TTSProvider`` ABCs as
  LiveKit ``stt.STT`` / ``llm.LLM`` / ``tts.TTS`` plugins (Phase 1).
* :mod:`johnny.agent.gate` — the bounded router-gate harness (timeout +
  barge-in cancellation; spike Johnny-9k2) plus the session-scoped
  :class:`~johnny.agent.gate.TurnLedger` that guarantees INV-1 — exactly one
  terminal per LiveKit turn id across the gate and the reply done-callback
  (spike Johnny-o3z). Stdlib-only and ``livekit``-free.

Importing this top-level package is intentionally side-effect free and
does **not** import ``livekit`` — only the submodules that subclass the
LiveKit SDK pull it in, mirroring the lazy-import discipline of
``johnny/voice_pipeline/livekit_transport.py``. That keeps ``import
johnny.agent`` cheap and safe in images/tests where the ``agent`` extra
(``livekit-agents``) is absent; the SDK-backed pieces live behind explicit
submodule imports (``from johnny.agent.session import build_agent_session``).
"""

from __future__ import annotations

__all__: list[str] = []
