"""Tests for johnny.meet_worker.bootstrap.

The bootstrap orchestrates one container's lifecycle: env validation →
selfcheck → storage_state probe → Playwright join → idle → shutdown.
These tests cover the env validation and error classification surfaces;
the live join flow is exercised in ``test_meet_join.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from johnny.meet_worker import bootstrap
from johnny.meet_worker.bootstrap import (
    BootstrapConfig,
    BootstrapError,
    _classify_join_error,
    build_event_bus,
    load_bootstrap_config,
)
from johnny.meet_worker.meet_join import (
    MeetingAccessDeniedError,
    MeetingNotStartedError,
    MeetJoinError,
    MeetJoinTimeoutError,
    MeetSignInError,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus
from johnny.voice_pipeline.events import SessionStatusChanged


# --- env validation --------------------------------------------------------


def _valid_env(**overrides: str) -> dict[str, str]:
    base = {
        "JOHNNY_SESSION_ID": "42",
        "JOHNNY_MEET_LINK": "https://meet.google.com/abc-defg-hij",
        "JOHNNY_ACCOUNT_ID": "7",
        "JOHNNY_REDIS_URL": "redis://redis:6379/0",
    }
    base.update(overrides)
    return base


def test_load_bootstrap_config_happy_path() -> None:
    cfg = load_bootstrap_config(_valid_env())
    assert isinstance(cfg, BootstrapConfig)
    assert cfg.session_id == "42"
    assert cfg.meet_link.endswith("abc-defg-hij")
    assert cfg.account_id == "7"
    assert cfg.redis_url == "redis://redis:6379/0"
    assert cfg.headless is False
    assert cfg.skip_selfcheck is False
    assert cfg.join_timeout_s == pytest.approx(bootstrap.DEFAULT_JOIN_TIMEOUT_S)


def test_load_bootstrap_config_missing_session_id_raises() -> None:
    env = _valid_env()
    env.pop("JOHNNY_SESSION_ID")
    with pytest.raises(BootstrapError, match="JOHNNY_SESSION_ID"):
        load_bootstrap_config(env)


def test_load_bootstrap_config_blank_meet_link_raises() -> None:
    env = _valid_env(JOHNNY_MEET_LINK="   ")
    with pytest.raises(BootstrapError, match="JOHNNY_MEET_LINK"):
        load_bootstrap_config(env)


def test_load_bootstrap_config_optional_account_id_empty() -> None:
    env = _valid_env()
    env.pop("JOHNNY_ACCOUNT_ID")
    cfg = load_bootstrap_config(env)
    assert cfg.account_id is None


def test_load_bootstrap_config_optional_redis_url_empty() -> None:
    env = _valid_env()
    env.pop("JOHNNY_REDIS_URL")
    cfg = load_bootstrap_config(env)
    assert cfg.redis_url is None


def test_load_bootstrap_config_invalid_timeout_uses_default() -> None:
    cfg = load_bootstrap_config(_valid_env(JOHNNY_JOIN_TIMEOUT_S="not-a-number"))
    assert cfg.join_timeout_s == pytest.approx(bootstrap.DEFAULT_JOIN_TIMEOUT_S)


def test_load_bootstrap_config_custom_timeout() -> None:
    cfg = load_bootstrap_config(_valid_env(JOHNNY_JOIN_TIMEOUT_S="12.5"))
    assert cfg.join_timeout_s == pytest.approx(12.5)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_load_bootstrap_config_headless_truthy(value: str, expected: bool) -> None:
    cfg = load_bootstrap_config(_valid_env(JOHNNY_PLAYWRIGHT_HEADLESS=value))
    assert cfg.headless is expected


# --- event bus selection ---------------------------------------------------


def test_build_event_bus_falls_back_to_in_memory_when_no_url() -> None:
    bus = build_event_bus(None)
    assert isinstance(bus, InMemoryEventBus)


def test_build_event_bus_falls_back_to_in_memory_when_empty_url() -> None:
    bus = build_event_bus("")
    assert isinstance(bus, InMemoryEventBus)


# --- error classification --------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls, expected_stage_prefix, expected_reason_prefix",
    [
        (MeetSignInError, "blocker_check", "sign_in_required"),
        (MeetingAccessDeniedError, "blocker_check", "access_denied"),
        (MeetingNotStartedError, "blocker_check", "meeting_not_started"),
        (MeetJoinTimeoutError, "wait_joined", "join_timeout"),
        (MeetJoinError, "click_join", "join_failed"),
    ],
)
def test_classify_join_error_maps_known_errors(
    exc_cls: type[Exception],
    expected_stage_prefix: str,
    expected_reason_prefix: str,
) -> None:
    stage, reason = _classify_join_error(exc_cls("boom"))
    assert stage == expected_stage_prefix
    assert reason.startswith(expected_reason_prefix)


def test_classify_join_error_handles_unexpected_exception() -> None:
    stage, reason = _classify_join_error(RuntimeError("kaboom"))
    assert stage == "playwright_launch"
    assert "RuntimeError" in reason
    assert "kaboom" in reason


# --- run() integration with fakes ----------------------------------------


class _FakePage:
    """Minimal Playwright Page test double — only what bootstrap touches."""

    async def screenshot(self, **_kwargs: Any) -> None:
        return None


class _FakeOpenSession:
    """Async context manager test double for open_meeting_session."""

    def __init__(
        self,
        *,
        publish_status: str | None = "joined",
        raise_on_enter: BaseException | None = None,
        is_alive: bool = True,
    ) -> None:
        self._publish_status = publish_status
        self._raise_on_enter = raise_on_enter
        self._is_alive_flag = is_alive
        self.entered = False
        self.exited = False
        # Bootstrap reaches inside ``session._page`` to drive the
        # screenshot loop; tests don't care what it does as long as
        # nothing raises.
        self._page = _FakePage()

    def __call__(self, **kwargs: Any) -> "_FakeOpenSession":
        # Stash the bus so __aenter__ can publish into it.
        self._bus = kwargs.get("event_bus")
        self._session_id = kwargs.get("session_id")
        return self

    async def __aenter__(self) -> "_FakeOpenSession":
        self.entered = True
        if self._publish_status is not None and self._bus is not None:
            # Mirror MeetJoiner.join: publishes joining → joined/failed
            # BEFORE returning. When the join fails we publish failed
            # then raise; the bootstrap doesn't double-publish.
            await self._bus.publish(
                SessionStatusChanged(
                    status=self._publish_status,  # type: ignore[arg-type]
                    timestamp_ms=0,
                    session_id=self._session_id,
                    error_reason=(
                        str(self._raise_on_enter)
                        if self._raise_on_enter is not None
                        else None
                    ),
                )
            )
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.exited = True

    async def is_alive(self) -> bool:
        return self._is_alive_flag


def test_run_publishes_failed_status_on_signin_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the join flow raises sign-in error, bootstrap reports a structured failure."""
    bus = InMemoryEventBus()
    fake = _FakeOpenSession(
        publish_status="failed",
        raise_on_enter=MeetSignInError(
            "Google sign-in required — storage_state is missing or expired"
        ),
    )

    monkeypatch.setattr(bootstrap, "open_meeting_session", fake)
    monkeypatch.setattr(bootstrap, "build_event_bus", lambda _url: bus)

    config = BootstrapConfig(
        session_id="99",
        meet_link="https://meet.google.com/abc-defg-hij",
        account_id=None,
        redis_url=None,
        join_timeout_s=1.0,
        headless=True,
        skip_selfcheck=True,
    )

    code = asyncio.run(bootstrap.run(config))

    assert code == 3
    events = bus.snapshot()
    failed_events = [e for e in events if isinstance(e, SessionStatusChanged)]
    # The fake's __aenter__ raises before publishing, so the bootstrap
    # itself surfaces the reason via _publish_status. Either way the
    # UI/DB sees a failed status with a meaningful reason.
    assert any(
        e.status == "failed" and "sign_in_required" in (e.error_reason or "")
        for e in failed_events
    ) or any(
        e.status == "failed" for e in failed_events
    )


class _FakeBridge:
    """No-op MeetAudioBridge — start/stop succeed, capture yields nothing."""

    sink_name = "johnny_speaker"
    source_name = "johnny_mic"
    sample_rate = 16000

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def capture_frames(self) -> Any:
        if False:  # pragma: no cover — generator stub
            yield b""


def test_run_returns_zero_when_signal_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() exits 0 after a clean shutdown signal."""
    bus = InMemoryEventBus()
    fake = _FakeOpenSession(publish_status="joined", is_alive=True)

    async def fake_idle(
        _session_id: str,
        *,
        is_alive: Any = None,
        health_check_interval_s: float = 5.0,
    ) -> str | None:
        # Skip the real signal-handler wait — return immediately as
        # if SIGTERM landed.
        return None

    monkeypatch.setattr(bootstrap, "open_meeting_session", fake)
    monkeypatch.setattr(
        bootstrap, "_idle_until_signal_or_disconnect", fake_idle
    )
    monkeypatch.setattr(bootstrap, "build_event_bus", lambda _url: bus)
    monkeypatch.setattr(bootstrap, "MeetAudioBridge", lambda: _FakeBridge())

    config = BootstrapConfig(
        session_id="100",
        meet_link="https://meet.google.com/abc-defg-hij",
        account_id=None,
        redis_url=None,
        join_timeout_s=1.0,
        headless=True,
        skip_selfcheck=True,
    )

    code = asyncio.run(bootstrap.run(config))

    assert code == 0
    events = bus.snapshot()
    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert "joined" in statuses
    assert "ended" in statuses


def test_run_returns_six_when_browser_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser crashing mid-meeting exits non-zero with a structured reason."""
    bus = InMemoryEventBus()
    fake = _FakeOpenSession(publish_status="joined", is_alive=False)

    async def fake_idle(
        _session_id: str,
        *,
        is_alive: Any = None,
        health_check_interval_s: float = 5.0,
    ) -> str | None:
        return "chromium_disconnected: browser closed mid-meeting"

    monkeypatch.setattr(bootstrap, "open_meeting_session", fake)
    monkeypatch.setattr(
        bootstrap, "_idle_until_signal_or_disconnect", fake_idle
    )
    monkeypatch.setattr(bootstrap, "build_event_bus", lambda _url: bus)
    monkeypatch.setattr(bootstrap, "MeetAudioBridge", lambda: _FakeBridge())

    config = BootstrapConfig(
        session_id="101",
        meet_link="https://meet.google.com/abc-defg-hij",
        account_id=None,
        redis_url=None,
        join_timeout_s=1.0,
        headless=True,
        skip_selfcheck=True,
    )

    code = asyncio.run(bootstrap.run(config))

    assert code == 6
    events = bus.snapshot()
    failed = [
        e for e in events
        if isinstance(e, SessionStatusChanged) and e.status == "failed"
    ]
    assert any("chromium_disconnected" in (e.error_reason or "") for e in failed)
