"""Tests for the agent job-payload contract (spike Johnny-y4j).

Stdlib-only: :mod:`johnny.agent.job_config` imports no ``livekit`` and no
``app.providers``, so these run anywhere the package is importable.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from johnny.agent.job_config import (
    DEFAULT_MODE,
    SUPPORTED_MODES,
    SessionJobConfig,
    agent_identity_for_session,
    bridge_identity_for_session,
    room_name_for_session,
)


def _full_config() -> SessionJobConfig:
    return SessionJobConfig(
        bot_session_id=42,
        room_name=room_name_for_session(42),
        meet_link="https://meet.google.com/abc-defg-hij",
        meeting_config_id=7,
        calendar_event_id=99,
        account_id=3,
        mode="approval_required",
        instructions="Be brief.",
        personality_prompt="[personality: Ada]\nWitty.",
        context="Quarterly review.",
        calendar_context="Q3 numbers.",
        calendar_attachments_text="doc body",
        prior_session_context="last time we agreed X",
        provider_config={
            "stt": {
                "provider_name": "deepgram",
                "display_name": "Deepgram",
                "credentials": {"api_key": "secret"},
                "options": {"model": "nova-3"},
            },
            "llm": {
                "provider_name": "openai",
                "display_name": "OpenAI",
                "credentials": {"api_key": "secret2"},
                "options": {},
            },
        },
        redis_url="redis://redis:6379/0",
    )


def test_metadata_round_trip_preserves_every_field() -> None:
    cfg = _full_config()
    restored = SessionJobConfig.from_metadata(cfg.to_metadata())
    assert restored == cfg


def test_to_metadata_is_deterministic_json() -> None:
    cfg = _full_config()
    # sort_keys makes the serialisation stable, so two dumps match and the
    # nested provider_config survives the trip intact.
    assert cfg.to_metadata() == cfg.to_metadata()
    payload = json.loads(cfg.to_metadata())
    assert payload["provider_config"]["stt"]["options"]["model"] == "nova-3"


def test_minimal_config_uses_safe_defaults() -> None:
    cfg = SessionJobConfig(bot_session_id=1, room_name=room_name_for_session(1))
    assert cfg.mode == DEFAULT_MODE == "listen_only"
    assert cfg.provider_config == {}
    assert cfg.redis_url is None
    assert SessionJobConfig.from_metadata(cfg.to_metadata()) == cfg


@pytest.mark.parametrize("bad", [{}, {"bot_session_id": None}, {"bot_session_id": ""}])
def test_from_dict_requires_session_id(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="bot_session_id"):
        SessionJobConfig.from_dict(bad)


def test_from_dict_requires_room_name() -> None:
    with pytest.raises(ValueError, match="room_name"):
        SessionJobConfig.from_dict({"bot_session_id": 1, "room_name": "  "})


def test_from_dict_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        SessionJobConfig.from_dict({"bot_session_id": 1, "room_name": "r", "mode": "shout"})


def test_from_dict_ignores_retired_pipeline_mode_key() -> None:
    # Sessions dispatched before Johnny-trt.43 carried a ``pipeline_mode``
    # key; the contract must ignore it (unknown keys are dropped) rather
    # than reject an old in-flight payload.
    cfg = SessionJobConfig.from_dict(
        {"bot_session_id": 1, "room_name": "r", "pipeline_mode": "unified"}
    )
    assert not hasattr(cfg, "pipeline_mode")


def test_from_dict_rejects_non_object_provider_config() -> None:
    with pytest.raises(ValueError, match="provider_config"):
        SessionJobConfig.from_dict(
            {"bot_session_id": 1, "room_name": "r", "provider_config": [1, 2]}
        )


@pytest.mark.parametrize("raw", ["not json", "[]", "123", '"a string"'])
def test_from_metadata_rejects_malformed_payload(raw: str) -> None:
    with pytest.raises(ValueError):
        SessionJobConfig.from_metadata(raw)


def test_from_env_mirrors_launcher_contract() -> None:
    # Exactly the keys app.services.docker_launcher._build_environment sets.
    env = {
        "JOHNNY_SESSION_ID": "55",
        "JOHNNY_MEETING_CONFIG_ID": "8",
        "JOHNNY_CALENDAR_EVENT_ID": "None",  # launcher str(None) when absent
        "JOHNNY_ACCOUNT_ID": "2",
        "JOHNNY_MEET_LINK": "https://meet.google.com/x",
        "JOHNNY_MODE": "suggest_only",
        "JOHNNY_INSTRUCTIONS": "hi",
        "JOHNNY_PERSONALITY_PROMPT": "persona",
        "JOHNNY_CONTEXT": "ctx",
        "JOHNNY_CALENDAR_CONTEXT": "cal",
        "JOHNNY_CALENDAR_ATTACHMENTS": "att",
        "JOHNNY_PRIOR_SESSION_CONTEXT": "prior",
        "JOHNNY_PROVIDER_CONFIG": json.dumps({"tts": {"provider_name": "piper"}}),
        "JOHNNY_REDIS_URL": "redis://redis:6379/0",
        "LIVEKIT_ROOM": "johnny-session-55",
    }
    cfg = SessionJobConfig.from_env(env)
    assert cfg.bot_session_id == 55
    assert cfg.meeting_config_id == 8
    assert cfg.calendar_event_id is None  # "None" coerced to absent
    assert cfg.account_id == 2
    assert cfg.mode == "suggest_only"
    assert cfg.room_name == "johnny-session-55"
    assert cfg.provider_config == {"tts": {"provider_name": "piper"}}
    assert cfg.redis_url == "redis://redis:6379/0"


def test_from_env_defaults_room_and_modes_when_blank() -> None:
    cfg = SessionJobConfig.from_env({"JOHNNY_SESSION_ID": "9"})
    assert cfg.room_name == room_name_for_session(9) == "johnny-session-9"
    assert cfg.mode == "listen_only"
    assert cfg.provider_config == {}
    assert cfg.redis_url is None


def test_from_env_requires_session_id() -> None:
    with pytest.raises(ValueError, match="JOHNNY_SESSION_ID"):
        SessionJobConfig.from_env({})


def test_to_env_round_trips_through_from_env() -> None:
    cfg = _full_config()
    assert SessionJobConfig.from_env(cfg.to_env()) == cfg


def test_identity_helpers_are_distinct_and_room_scoped() -> None:
    assert bridge_identity_for_session(5) == "meet-bridge-5"
    assert agent_identity_for_session(5) == "johnny-agent-5"
    assert bridge_identity_for_session(5) != agent_identity_for_session(5)


def test_mode_vocabularies_match_canonical_pipeline_constants() -> None:
    """Drift guard: the duplicated literals must equal the canonical defs.

    job_config re-declares the mode strings to stay
    dependency-free; if the canonical constants ever change, this fails so the
    copy is updated in lockstep. Skipped if the heavy modules can't import in
    this environment.
    """
    try:
        from johnny.voice_pipeline.reasoning import (
            APPROVAL_REQUIRED_MODE,
            AUTONOMOUS_MODE,
            LIMITED_AUTO_SPEAK_MODE,
            LISTEN_ONLY_MODE,
            NON_SPEAKING_MODES,
            SPEAKING_MODES,
            SUGGEST_ONLY_MODE,
        )
    except Exception as exc:  # pragma: no cover - env without heavy deps
        pytest.skip(f"canonical pipeline constants unavailable: {exc}")

    assert SUPPORTED_MODES == {
        LISTEN_ONLY_MODE,
        SUGGEST_ONLY_MODE,
        APPROVAL_REQUIRED_MODE,
        LIMITED_AUTO_SPEAK_MODE,
        AUTONOMOUS_MODE,
    }
    # The real invariant: the dispatch contract must accept EVERY mode a meeting
    # can be configured in — the full union of the engine's non-speaking and
    # speaking modes. autonomous was missing from SUPPORTED_MODES, so a dispatch
    # for an autonomous meeting was rejected at parse and the agent abandoned the
    # job (Johnny-52b). This union assertion catches any future such omission.
    assert SUPPORTED_MODES == NON_SPEAKING_MODES | SPEAKING_MODES


def test_from_metadata_accepts_autonomous_mode() -> None:
    """Autonomous (free-form full-auto-speak) must survive the dispatch round trip.

    Regression guard (Johnny-52b): autonomous is a first-class legacy SPEAKING_MODE
    and the sole FREE_FORM_MODE, and the agent answer path special-cases it — yet it
    was missing from SUPPORTED_MODES, so from_metadata raised ``unknown mode
    'autonomous'`` and the worker abandoned the dispatch (autonomous meetings got no
    bot). A real-provider dispatch into the live agent-worker reproduced exactly this.
    """
    cfg = SessionJobConfig(
        bot_session_id=42,
        room_name=room_name_for_session(42),
        mode="autonomous",
    )
    restored = SessionJobConfig.from_metadata(cfg.to_metadata())
    assert restored.mode == "autonomous"
    assert "autonomous" in SUPPORTED_MODES
