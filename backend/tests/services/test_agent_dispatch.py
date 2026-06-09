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
from app.services.agent_dispatch import (
    dispatch_session_agent,
    session_job_config_from_launch_context,
)
from app.services.session_scheduler import LaunchContext
from johnny.agent.job_config import (
    DEFAULT_MODE,
    DEFAULT_PIPELINE_MODE,
    SessionJobConfig,
)


def _full_ctx() -> LaunchContext:
    return LaunchContext(
        bot_session_id=42,
        meeting_config_id=11,
        calendar_event_id=99,
        identity_account_id=3,
        meet_link="https://meet.example/abc",
        container_name="meet-worker-session-42",
        mode="approval_required",
        instructions="Stick to the agenda.",
        personality_prompt="[personality: Aria]\nWarm and concise.",
        context="Internal sync.",
        calendar_context="Q3 planning",
        calendar_attachments_text="doc body",
        prior_session_context="Last week: agreed on X.",
        provider_config={"llm": {"provider_name": "openai"}},
        pipeline_mode="unified",
    )


def test_config_from_launch_context_maps_every_field() -> None:
    config = session_job_config_from_launch_context(_full_ctx(), redis_url="redis://r:6379/0")

    assert config.bot_session_id == 42
    assert config.room_name == "johnny-session-42"  # derived from the session id
    assert config.meet_link == "https://meet.example/abc"
    assert config.meeting_config_id == 11
    assert config.calendar_event_id == 99
    assert config.account_id == 3  # identity_account_id -> account_id
    assert config.mode == "approval_required"
    assert config.pipeline_mode == "unified"
    assert config.instructions == "Stick to the agenda."
    assert config.personality_prompt == "[personality: Aria]\nWarm and concise."
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


def test_config_blank_mode_and_default_redis_are_lenient() -> None:
    # LaunchContext's struct defaults (mode="" , pipeline_mode="split") coerce to
    # the contract defaults, and redis defaults to None when not supplied.
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
    assert config.pipeline_mode == DEFAULT_PIPELINE_MODE
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
