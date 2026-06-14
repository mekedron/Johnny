"""Tests for the agent job-payload contract (spike Johnny-y4j).

Stdlib-only: :mod:`johnny.agent.job_config` imports no ``livekit`` and no
``app.providers``, so these run anywhere the package is importable.

Johnny-trt.45: behavior (mode / character / context / allowlist / threshold)
rides the frozen ``agent_snapshot`` blob; the per-field overrides and their
``JOHNNY_*`` env vars are gone. The drift guard against the docker launcher's
``_build_environment`` pins the new env key set.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from johnny.agent.job_config import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODE,
    DEFAULT_ROUTER_LLM_TIMEOUT_S,
    DEFAULT_ROUTER_TIMEOUT_FALLBACK_MODE,
    DEFAULT_ROUTER_TIMEOUT_FALLBACK_TEXT,
    DEFAULT_ROUTER_TIMEOUT_RETRIES,
    MAX_ROUTER_TIMEOUT_RETRIES,
    SUPPORTED_MODES,
    SessionJobConfig,
    agent_identity_for_session,
    bridge_identity_for_session,
    room_name_for_session,
)


def _snapshot() -> dict[str, Any]:
    """A realistic frozen agent snapshot (build_agent_snapshot's shape)."""
    return {
        "agent_id": 4,
        "name": "Ada",
        "avatar": None,
        "character_prompt": "[character: Ada]\nWitty.",
        "mode": "approval_required",
        "allowed_replies": ["Yes.", "No.", "Could you repeat that?"],
        "confidence_threshold": 0.62,
        "router_llm_timeout_s": 12.5,
        "router_timeout_retries": 2,
        "router_timeout_fallback_mode": "llm",
        "router_timeout_fallback_text": "One moment — could you repeat that?",
        "providers": {"tts_provider_id": 3, "tts_voice_id": "amy"},
        "assignment_context": "Quarterly review.",
    }


def _full_config() -> SessionJobConfig:
    return SessionJobConfig(
        bot_session_id=42,
        room_name=room_name_for_session(42),
        meet_link="https://meet.google.com/abc-defg-hij",
        meeting_config_id=7,
        calendar_event_id=99,
        account_id=3,
        agent_id=4,
        agent_snapshot=_snapshot(),
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
    # nested provider_config / agent_snapshot survive the trip intact.
    assert cfg.to_metadata() == cfg.to_metadata()
    payload = json.loads(cfg.to_metadata())
    assert payload["provider_config"]["stt"]["options"]["model"] == "nova-3"
    assert payload["agent_snapshot"]["assignment_context"] == "Quarterly review."


def test_behavior_derives_from_snapshot() -> None:
    """The Johnny-trt.45 invariant: behavior has ONE source — the snapshot."""
    cfg = _full_config()
    assert cfg.mode == "approval_required"
    assert cfg.character_prompt == "[character: Ada]\nWitty."
    assert cfg.context == "Quarterly review."
    assert cfg.allowed_replies == ("Yes.", "No.", "Could you repeat that?")
    assert cfg.confidence_threshold == pytest.approx(0.62)
    # Router-triage timeout + on-timeout fallback (Johnny-xql).
    assert cfg.router_llm_timeout_s == pytest.approx(12.5)
    assert cfg.router_timeout_retries == 2
    assert cfg.router_timeout_fallback_mode == "llm"
    assert cfg.router_timeout_fallback_text == "One moment — could you repeat that?"


def test_minimal_config_uses_safe_defaults() -> None:
    cfg = SessionJobConfig(bot_session_id=1, room_name=room_name_for_session(1))
    assert cfg.mode == DEFAULT_MODE == "listen_only"
    assert cfg.character_prompt == ""
    assert cfg.context == ""
    assert cfg.allowed_replies == ()
    assert cfg.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD
    # Router-triage timeout + on-timeout fallback (Johnny-xql) defaults.
    assert cfg.router_llm_timeout_s == DEFAULT_ROUTER_LLM_TIMEOUT_S
    assert cfg.router_timeout_retries == DEFAULT_ROUTER_TIMEOUT_RETRIES
    assert cfg.router_timeout_fallback_mode == DEFAULT_ROUTER_TIMEOUT_FALLBACK_MODE
    assert cfg.router_timeout_fallback_text == DEFAULT_ROUTER_TIMEOUT_FALLBACK_TEXT
    assert cfg.agent_id is None
    assert cfg.agent_snapshot == {}
    assert cfg.provider_config == {}
    assert cfg.redis_url is None
    assert SessionJobConfig.from_metadata(cfg.to_metadata()) == cfg


def test_router_timeout_fields_are_lenient_and_clamped() -> None:
    """Johnny-xql: a corrupt snapshot degrades / clamps, never crashes."""

    def _cfg(snapshot: dict[str, Any]) -> SessionJobConfig:
        return SessionJobConfig(
            bot_session_id=1, room_name="r", agent_snapshot=snapshot
        )

    # Unparseable / missing → defaults.
    assert _cfg({"router_llm_timeout_s": "nope"}).router_llm_timeout_s == (
        DEFAULT_ROUTER_LLM_TIMEOUT_S
    )
    assert _cfg({"router_timeout_retries": "nope"}).router_timeout_retries == (
        DEFAULT_ROUTER_TIMEOUT_RETRIES
    )
    # timeout is floored at 0 (<= 0 means "disable the bound" downstream) but not
    # capped; retries clamp into [0, MAX].
    assert _cfg({"router_llm_timeout_s": -3}).router_llm_timeout_s == 0.0
    assert _cfg({"router_llm_timeout_s": 0}).router_llm_timeout_s == 0.0
    assert _cfg({"router_timeout_retries": 99}).router_timeout_retries == (
        MAX_ROUTER_TIMEOUT_RETRIES
    )
    assert _cfg({"router_timeout_retries": -5}).router_timeout_retries == 0
    # An unknown mode degrades to the default; a blank text degrades to the
    # canonical line (never an empty utterance).
    assert _cfg({"router_timeout_fallback_mode": "shout"}).router_timeout_fallback_mode == (
        DEFAULT_ROUTER_TIMEOUT_FALLBACK_MODE
    )
    assert _cfg({"router_timeout_fallback_text": "   "}).router_timeout_fallback_text == (
        DEFAULT_ROUTER_TIMEOUT_FALLBACK_TEXT
    )


def test_with_mode_rewrites_the_snapshot_copy_only() -> None:
    """The no-TTS degrade seam: a new config whose snapshot carries the
    effective mode; the original config (and its snapshot) is untouched."""
    cfg = _full_config()
    degraded = cfg.with_mode("suggest_only")
    assert degraded.mode == "suggest_only"
    assert degraded.agent_snapshot["mode"] == "suggest_only"
    assert cfg.mode == "approval_required"
    # Every other snapshot key survives the rewrite.
    assert degraded.character_prompt == cfg.character_prompt
    assert degraded.context == cfg.context


def test_snapshot_mode_is_lenient_at_read_time() -> None:
    """A hand-built/corrupt snapshot mutes the bot instead of crashing."""
    cfg = SessionJobConfig(
        bot_session_id=1, room_name="r", agent_snapshot={"mode": "shout"}
    )
    assert cfg.mode == DEFAULT_MODE


def test_snapshot_peer_names_lenient_read(  # Johnny-trt.47
) -> None:
    """The co-agent roster degrades like every optional snapshot field."""

    def cfg(value: Any) -> SessionJobConfig:
        return SessionJobConfig(
            bot_session_id=1, room_name="r", agent_snapshot={"peer_names": value}
        )

    assert cfg(["Echo", " Nova ", ""]).peer_names == ("Echo", "Nova")
    assert cfg(None).peer_names == ()
    assert cfg("Echo").peer_names == ()  # non-list shape degrades to absent
    assert (
        SessionJobConfig(bot_session_id=1, room_name="r").peer_names == ()
    )  # absent key — every single-agent session


def test_snapshot_workspace_lenient_read() -> None:  # Johnny-wks.1
    """The workspace stamp degrades like every optional snapshot field:
    absent / malformed → (None, default) so legacy snapshots keep the
    global sandbox byte-identically."""

    def cfg(snapshot: dict[str, Any]) -> SessionJobConfig:
        return SessionJobConfig(bot_session_id=1, room_name="r", agent_snapshot=snapshot)

    # Legacy snapshot: no workspace info at all.
    legacy = cfg({})
    assert legacy.workspace_id is None
    assert legacy.workspace_is_default is True
    assert legacy.workspace_slug is None

    # The producer's shape: id + identity object.
    stamped = cfg(
        {
            "workspace_id": 7,
            "workspace": {"id": 7, "name": "Finance", "slug": "finance", "is_default": False},
        }
    )
    assert stamped.workspace_id == 7
    assert stamped.workspace_is_default is False
    assert stamped.workspace_slug == "finance"

    # Default workspace stamped explicitly.
    default = cfg(
        {
            "workspace_id": 1,
            "workspace": {"id": 1, "name": "Default", "slug": "default", "is_default": True},
        }
    )
    assert default.workspace_id == 1
    assert default.workspace_is_default is True
    assert default.workspace_slug == "default"

    # Malformed id degrades to absent (= default workspace).
    junk = cfg({"workspace_id": "lots"})
    assert junk.workspace_id is None
    assert junk.workspace_is_default is True

    # Slug reads degrade like the rest (Johnny-wks.3): blank / non-mapping
    # identity objects yield None, never a guessed directory key.
    assert cfg({"workspace_id": 7, "workspace": {"slug": "  "}}).workspace_slug is None
    assert cfg({"workspace_id": 7, "workspace": "junk"}).workspace_slug is None


def test_workspace_from_agent_snapshot_sanitizes() -> None:  # Johnny-wks.1
    from johnny.agent.job_config import workspace_from_agent_snapshot

    # No stamp → None (legacy rows stay byte-identical).
    assert workspace_from_agent_snapshot({}) is None
    assert workspace_from_agent_snapshot({"workspace_id": "junk"}) is None

    # Full stamp: identity fields survive, unknown keys are dropped.
    payload = workspace_from_agent_snapshot(
        {
            "workspace_id": 7,
            "workspace": {
                "id": 7,
                "name": "Finance",
                "slug": "finance",
                "is_default": False,
                "smuggled": {"credentials": "nope"},
            },
        }
    )
    assert payload == {"id": 7, "is_default": False, "name": "Finance", "slug": "finance"}

    # The id can come from the identity object alone.
    assert workspace_from_agent_snapshot(
        {"workspace": {"id": 3, "is_default": True}}
    ) == {"id": 3, "is_default": True}


@pytest.mark.parametrize("bad", [{}, {"bot_session_id": None}, {"bot_session_id": ""}])
def test_from_dict_requires_session_id(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="bot_session_id"):
        SessionJobConfig.from_dict(bad)


def test_from_dict_requires_room_name() -> None:
    with pytest.raises(ValueError, match="room_name"):
        SessionJobConfig.from_dict({"bot_session_id": 1, "room_name": "  "})


def test_from_dict_rejects_unknown_snapshot_mode() -> None:
    """The wire format stays strict: a dispatched payload whose snapshot
    names a mode the engine doesn't know fails loud at the worker."""
    with pytest.raises(ValueError, match="unknown agent_snapshot mode"):
        SessionJobConfig.from_dict(
            {
                "bot_session_id": 1,
                "room_name": "r",
                "agent_snapshot": {"mode": "shout"},
            }
        )


def test_from_dict_rejects_non_object_snapshot() -> None:
    with pytest.raises(ValueError, match="agent_snapshot"):
        SessionJobConfig.from_dict(
            {"bot_session_id": 1, "room_name": "r", "agent_snapshot": [1, 2]}
        )


def test_from_dict_ignores_retired_keys() -> None:
    # Sessions dispatched before Johnny-trt.43/.45 carried ``pipeline_mode``
    # and the per-field behavior overrides; the contract must ignore them
    # (unknown keys are dropped) rather than reject an old in-flight payload.
    cfg = SessionJobConfig.from_dict(
        {
            "bot_session_id": 1,
            "room_name": "r",
            "pipeline_mode": "unified",
            "mode": "autonomous",
            "instructions": "old",
            "character_prompt": "old",
            "context": "old",
            "allowed_replies": ["old"],
            "confidence_threshold": 0.1,
        }
    )
    assert not hasattr(cfg, "pipeline_mode")
    # The retired top-level fields do NOT leak into behavior — only the
    # snapshot drives it, and this payload has none.
    assert cfg.mode == DEFAULT_MODE
    assert cfg.character_prompt == ""
    assert cfg.allowed_replies == ()
    assert cfg.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD


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
    snapshot = _snapshot()
    env = {
        "JOHNNY_SESSION_ID": "55",
        "JOHNNY_MEETING_CONFIG_ID": "8",
        "JOHNNY_CALENDAR_EVENT_ID": "None",  # launcher str(None) when absent
        "JOHNNY_ACCOUNT_ID": "2",
        "JOHNNY_MEET_LINK": "https://meet.google.com/x",
        "JOHNNY_AGENT_ID": "4",
        "JOHNNY_AGENT_SNAPSHOT": json.dumps(snapshot),
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
    assert cfg.agent_id == 4
    assert cfg.agent_snapshot == snapshot
    assert cfg.mode == "approval_required"
    assert cfg.character_prompt == snapshot["character_prompt"]
    assert cfg.allowed_replies == ("Yes.", "No.", "Could you repeat that?")
    assert cfg.confidence_threshold == pytest.approx(0.62)
    assert cfg.room_name == "johnny-session-55"
    assert cfg.provider_config == {"tts": {"provider_name": "piper"}}
    assert cfg.redis_url == "redis://redis:6379/0"


def test_from_env_retired_behavior_vars_are_inert() -> None:
    """Johnny-trt.45 acceptance: the removed override env vars are GONE —
    setting them changes nothing (behavior comes from the snapshot alone)."""
    cfg = SessionJobConfig.from_env(
        {
            "JOHNNY_SESSION_ID": "9",
            "JOHNNY_MODE": "autonomous",
            "JOHNNY_INSTRUCTIONS": "old override",
            "JOHNNY_CHARACTER_PROMPT": "old persona",
            "JOHNNY_CONTEXT": "old ctx",
            "JOHNNY_ALLOWED_REPLIES": json.dumps(["Yes."]),
            "JOHNNY_CONFIDENCE_THRESHOLD": "0.11",
        }
    )
    assert cfg.mode == DEFAULT_MODE
    assert cfg.character_prompt == ""
    assert cfg.context == ""
    assert cfg.allowed_replies == ()
    assert cfg.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD


def test_from_env_defaults_room_and_modes_when_blank() -> None:
    cfg = SessionJobConfig.from_env({"JOHNNY_SESSION_ID": "9"})
    assert cfg.room_name == room_name_for_session(9) == "johnny-session-9"
    assert cfg.mode == "listen_only"
    assert cfg.allowed_replies == ()
    assert cfg.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD
    assert cfg.agent_snapshot == {}
    assert cfg.provider_config == {}
    assert cfg.redis_url is None


def test_from_env_tolerates_malformed_snapshot_values() -> None:
    """A sloppy snapshot degrades to the contract defaults, never raises."""
    cfg = SessionJobConfig.from_env(
        {"JOHNNY_SESSION_ID": "9", "JOHNNY_AGENT_SNAPSHOT": "not json"}
    )
    assert cfg.agent_snapshot == {}
    assert cfg.mode == DEFAULT_MODE
    # Junk inside an otherwise-valid snapshot degrades per-field.
    sloppy = SessionJobConfig.from_env(
        {
            "JOHNNY_SESSION_ID": "9",
            "JOHNNY_AGENT_SNAPSHOT": json.dumps(
                {"allowed_replies": "not a list", "confidence_threshold": "much"}
            ),
        }
    )
    assert sloppy.allowed_replies == ()
    assert sloppy.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD
    # Out-of-range values clamp instead of poisoning the gate.
    high = SessionJobConfig.from_env(
        {
            "JOHNNY_SESSION_ID": "9",
            "JOHNNY_AGENT_SNAPSHOT": json.dumps({"confidence_threshold": 7.5}),
        }
    )
    assert high.confidence_threshold == 1.0


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


def test_default_confidence_threshold_matches_canonical_constant() -> None:
    """Drift guard (Johnny-trt.41): job_config re-declares the router gate's
    default speak floor to stay dependency-free; it must equal the canonical
    ``johnny.voice_pipeline.reasoning.DEFAULT_CONFIDENCE_THRESHOLD``."""
    try:
        from johnny.voice_pipeline.reasoning import (
            DEFAULT_CONFIDENCE_THRESHOLD as CANONICAL_THRESHOLD,
        )
    except Exception as exc:  # pragma: no cover - env without heavy deps
        pytest.skip(f"canonical pipeline constants unavailable: {exc}")

    assert DEFAULT_CONFIDENCE_THRESHOLD == CANONICAL_THRESHOLD


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
        agent_snapshot={"mode": "autonomous"},
    )
    restored = SessionJobConfig.from_metadata(cfg.to_metadata())
    assert restored.mode == "autonomous"
    assert "autonomous" in SUPPORTED_MODES
