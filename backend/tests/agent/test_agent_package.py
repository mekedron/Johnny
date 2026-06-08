"""Phase-0 smoke tests for the johnny.agent package (Johnny-jue).

Asserts the package is import-safe WITHOUT livekit, and that the SDK-backed
harness imports and exposes its skeleton wherever the ``agent`` extra
(``livekit-agents``) is installed — i.e. the api/agent image.
"""

from __future__ import annotations

import importlib

import pytest


def test_agent_package_imports_without_livekit() -> None:
    """`import johnny.agent` (+ adapters) must not require the livekit SDK."""
    pkg = importlib.import_module("johnny.agent")
    assert pkg.__doc__

    adapters = importlib.import_module("johnny.agent.adapters")
    assert adapters.__doc__


def test_session_harness_imports_with_livekit() -> None:
    """Where livekit is installed, the harness + JohnnyAgent import cleanly."""
    pytest.importorskip("livekit.agents")

    session = importlib.import_module("johnny.agent.session")
    assert hasattr(session, "build_agent_session")
    assert hasattr(session, "load_vad")
    assert session.DEFAULT_INSTRUCTIONS

    # JohnnyAgent must be a real livekit.agents.Agent subclass.
    from livekit.agents import Agent

    assert issubclass(session.JohnnyAgent, Agent)


def test_turn_detector_and_silero_plugins_importable() -> None:
    """The two baked plugins (Silero VAD + multilingual turn detector) import."""
    pytest.importorskip("livekit.agents")

    silero = importlib.import_module("livekit.plugins.silero")
    assert hasattr(silero, "VAD")

    multilingual = importlib.import_module(
        "livekit.plugins.turn_detector.multilingual"
    )
    assert hasattr(multilingual, "MultilingualModel")
