"""Producer-side threading: LaunchContext -> SessionJobConfig -> dispatch (Johnny-7we).

Covers :mod:`app.services.agent_dispatch`:

* :func:`session_job_config_from_launch_context` — the field-for-field translation
  from the API's :class:`~app.services.session_scheduler.LaunchContext` to the
  dispatch :class:`~johnny.agent.job_config.SessionJobConfig` (room-name derivation,
  ``identity_account_id`` → ``account_id``, blank-mode leniency, redis pass-through,
  provider_config copy);
* :func:`dispatch_session_agent` — that it builds the config and hands the right
  room + metadata to :func:`johnny.agent.dispatch.dispatch_agent`.

``livekit`` is not required: the config builder is stdlib, and the dispatch test
stubs ``johnny.agent.dispatch.dispatch_agent`` so the SDK is never imported.
"""

from __future__ import annotations

import johnny.agent.dispatch as dispatch_mod
import johnny.agent.room_auth as room_auth_mod
from app.services.agent_dispatch import (
    agent_orchestrator_enabled,
    bridge_launch_environment,
    dispatch_session_agent,
    maybe_dispatch_session_agent,
    session_job_config_from_launch_context,
)
from app.services.session_scheduler import LaunchContext
from johnny.agent.job_config import (
    DEFAULT_MODE,
    SessionJobConfig,
)

_SNAPSHOT = {
    "agent_id": 4,
    "name": "Aria",
    "character_prompt": "[personality: Aria]\nWarm and concise.",
    "mode": "approval_required",
    "allowed_replies": [],
    "confidence_threshold": 0.7,
    "assignment_context": "Internal sync.",
}


def _full_ctx() -> LaunchContext:
    return LaunchContext(
        bot_session_id=42,
        meeting_config_id=11,
        calendar_event_id=99,
        identity_account_id=3,
        meet_link="https://meet.example/abc",
        container_name="meet-worker-session-42",
        agent_id=4,
        agent_snapshot=dict(_SNAPSHOT),
        calendar_context="Q3 planning",
        calendar_attachments_text="doc body",
        prior_session_context="Last week: agreed on X.",
        provider_config={"llm": {"provider_name": "openai"}},
    )


def test_config_from_launch_context_maps_every_field() -> None:
    config = session_job_config_from_launch_context(_full_ctx(), redis_url="redis://r:6379/0")

    assert config.bot_session_id == 42
    assert config.room_name == "johnny-session-42"  # derived from the session id
    assert config.meet_link == "https://meet.example/abc"
    assert config.meeting_config_id == 11
    assert config.calendar_event_id == 99
    assert config.account_id == 3  # identity_account_id -> account_id
    assert config.agent_id == 4
    assert config.agent_snapshot == _SNAPSHOT
    # Behavior derives from the snapshot (Johnny-trt.45).
    assert config.mode == "approval_required"
    assert config.character_prompt == "[personality: Aria]\nWarm and concise."
    assert config.context == "Internal sync."
    assert config.calendar_context == "Q3 planning"
    assert config.calendar_attachments_text == "doc body"
    assert config.prior_session_context == "Last week: agreed on X."
    assert config.provider_config == {"llm": {"provider_name": "openai"}}
    assert config.redis_url == "redis://r:6379/0"


def test_config_copies_provider_config() -> None:
    src = {"llm": {"provider_name": "openai"}}
    ctx = LaunchContext(
        bot_session_id=1,
        meeting_config_id=1,
        calendar_event_id=1,
        identity_account_id=1,
        meet_link="",
        container_name="c",
        provider_config=src,
    )
    config = session_job_config_from_launch_context(ctx)
    assert config.provider_config == src
    assert config.provider_config is not src  # fresh copy, not the launcher's dict


def test_config_empty_snapshot_and_default_redis_are_lenient() -> None:
    # LaunchContext's struct default (empty snapshot) degrades to the
    # contract default mode, and redis defaults to None when not supplied.
    ctx = LaunchContext(
        bot_session_id=8,
        meeting_config_id=1,
        calendar_event_id=1,
        identity_account_id=1,
        meet_link="",
        container_name="c",
    )
    config = session_job_config_from_launch_context(ctx)
    assert config.mode == DEFAULT_MODE
    assert config.agent_id is None
    assert config.agent_snapshot == {}
    assert config.redis_url is None


async def test_dispatch_session_agent_calls_dispatch_with_room_and_metadata(
    monkeypatch: object,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_dispatch(
        *,
        room: str,
        config: SessionJobConfig,
        url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> str:
        captured["room"] = room
        captured["config"] = config
        captured["url"] = url
        return "dispatch-sentinel"

    monkeypatch.setattr(dispatch_mod, "dispatch_agent", _fake_dispatch)  # type: ignore[attr-defined]

    result = await dispatch_session_agent(
        _full_ctx(), redis_url="redis://r:6379/0", url="ws://lk:7880"
    )

    assert result == "dispatch-sentinel"
    assert captured["room"] == "johnny-session-42"
    assert captured["url"] == "ws://lk:7880"
    config = captured["config"]
    assert isinstance(config, SessionJobConfig)
    assert config.room_name == "johnny-session-42"
    assert config.redis_url == "redis://r:6379/0"
    assert config.account_id == 3
    # The metadata the agent worker will parse carries the same config.
    assert SessionJobConfig.from_metadata(config.to_metadata()) == config


# --- Orchestrator gating + lifecycle hook (Johnny-9eh) ----------------------


def test_orchestrator_enabled_default_is_agentsession() -> None:
    # No env var set -> default agentsession (Johnny-n22) -> agent path ON.
    assert agent_orchestrator_enabled({}) is True
    # Only an explicit ``legacy`` opts a session out.
    assert agent_orchestrator_enabled({"JOHNNY_ORCHESTRATOR": "legacy"}) is False


def test_orchestrator_enabled_when_agentsession() -> None:
    assert agent_orchestrator_enabled({"JOHNNY_ORCHESTRATOR": "agentsession"}) is True
    # Case/space tolerant.
    assert agent_orchestrator_enabled({"JOHNNY_ORCHESTRATOR": "  AgentSession "}) is True
    # An unrecognised value fails safe to the proven agent path (default flip, Johnny-n22).
    assert agent_orchestrator_enabled({"JOHNNY_ORCHESTRATOR": "experimental"}) is True


async def test_maybe_dispatch_is_noop_in_legacy(monkeypatch: object) -> None:
    called = False

    async def _fake_dispatch(**_kwargs: object) -> str:
        nonlocal called
        called = True
        return "x"

    monkeypatch.setattr(dispatch_mod, "dispatch_agent", _fake_dispatch)  # type: ignore[attr-defined]

    result = await maybe_dispatch_session_agent(
        _full_ctx(), environ={"JOHNNY_ORCHESTRATOR": "legacy"}
    )

    assert result is None
    assert called is False  # legacy mode never reaches the SDK


async def test_maybe_dispatch_dispatches_in_agentsession(monkeypatch: object) -> None:
    captured: dict[str, object] = {}

    async def _fake_dispatch(
        *, room: str, config: SessionJobConfig, **_kwargs: object
    ) -> str:
        captured["room"] = room
        captured["redis_url"] = config.redis_url
        return "dispatch-sentinel"

    monkeypatch.setattr(dispatch_mod, "dispatch_agent", _fake_dispatch)  # type: ignore[attr-defined]

    result = await maybe_dispatch_session_agent(
        _full_ctx(),
        environ={"JOHNNY_ORCHESTRATOR": "agentsession", "REDIS_URL": "redis://r:6379/0"},
    )

    assert result == "dispatch-sentinel"
    assert captured["room"] == "johnny-session-42"
    # REDIS_URL from the API env is threaded into the dispatched config.
    assert captured["redis_url"] == "redis://r:6379/0"


async def test_maybe_dispatch_swallows_failure(monkeypatch: object) -> None:
    # A dispatch failure must NOT propagate — the legacy meet-worker is already
    # running the session, so session start must not break on the agent path.
    async def _boom(**_kwargs: object) -> str:
        raise RuntimeError("livekit unreachable")

    monkeypatch.setattr(dispatch_mod, "dispatch_agent", _boom)  # type: ignore[attr-defined]

    result = await maybe_dispatch_session_agent(
        _full_ctx(), environ={"JOHNNY_ORCHESTRATOR": "agentsession"}
    )

    assert result is None


# --- Bridge-mode launch env (Johnny-wz5) ------------------------------------


def test_bridge_launch_environment_pins_legacy() -> None:
    # Legacy mode pins JOHNNY_ORCHESTRATOR=legacy (no LiveKit vars) so the
    # meet-worker runs the in-worker pipeline. It must NOT return {} — an unset
    # var makes the meet-worker default to agentsession and crash with no token
    # (Johnny-9xt). The two halves must agree on the mode.
    assert bridge_launch_environment(
        bot_session_id=42, environ={"JOHNNY_ORCHESTRATOR": "legacy"}
    ) == {"JOHNNY_ORCHESTRATOR": "legacy"}


def test_bridge_launch_environment_agentsession(monkeypatch: object) -> None:
    captured: dict[str, object] = {}

    def _fake_mint(
        *,
        bot_session_id: int | str,
        api_key: str | None = None,
        api_secret: str | None = None,
        **_kwargs: object,
    ) -> str:
        captured["bot_session_id"] = bot_session_id
        captured["api_key"] = api_key
        captured["api_secret"] = api_secret
        return "bridge-jwt"

    # Lazy `from johnny.agent.room_auth import mint_bridge_token` resolves the
    # patched module attribute at call time.
    monkeypatch.setattr(room_auth_mod, "mint_bridge_token", _fake_mint)  # type: ignore[attr-defined]

    env = bridge_launch_environment(
        bot_session_id=42,
        environ={
            "JOHNNY_ORCHESTRATOR": "agentsession",
            "LIVEKIT_URL": "ws://livekit:7880",
            "LIVEKIT_API_KEY": "devkey",
            "LIVEKIT_API_SECRET": "secret",
        },
    )

    assert env == {
        "JOHNNY_ORCHESTRATOR": "agentsession",
        "LIVEKIT_URL": "ws://livekit:7880",
        "LIVEKIT_ROOM": "johnny-session-42",  # one room per session
        "LIVEKIT_IDENTITY": "meet-bridge-42",  # the bridge participant
        "LIVEKIT_TOKEN": "bridge-jwt",  # minted per-room bridge token
    }
    # The creds from the env are forwarded to the minter (so the same key/secret
    # pair the SFU validates against is used).
    assert captured["bot_session_id"] == 42
    assert captured["api_key"] == "devkey"
    assert captured["api_secret"] == "secret"


def test_bridge_launch_environment_degrades_on_mint_failure(
    monkeypatch: object,
) -> None:
    # Missing creds → the minter raises → degrade to a pinned legacy flag so the
    # meet-worker runs the proven legacy pipeline instead of a dead bridge (never
    # break a launch). Pinning (not {}) is what actually forces legacy — an unset
    # var would default the meet-worker to agentsession and crash (Johnny-9xt).
    def _boom(**_kwargs: object) -> str:
        raise ValueError("missing LIVEKIT_API_KEY, LIVEKIT_API_SECRET")

    monkeypatch.setattr(room_auth_mod, "mint_bridge_token", _boom)  # type: ignore[attr-defined]

    env = bridge_launch_environment(
        bot_session_id=7, environ={"JOHNNY_ORCHESTRATOR": "agentsession"}
    )

    assert env == {"JOHNNY_ORCHESTRATOR": "legacy"}
