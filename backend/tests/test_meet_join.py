"""Tests for johnny.meet_worker.meet_join."""

from __future__ import annotations

from typing import Any

import pytest

from johnny.meet_worker.meet_join import (
    ACCESS_DENIED_SELECTORS,
    CAM_OFF_SELECTORS,
    JOIN_BUTTON_SELECTORS,
    MEETING_NOT_STARTED_SELECTORS,
    MIC_OFF_SELECTORS,
    SIGN_IN_REQUIRED_SELECTORS,
    JoinResult,
    MeetingAccessDeniedError,
    MeetingNotStartedError,
    MeetJoiner,
    MeetJoinError,
    MeetJoinTimeoutError,
    MeetSignInError,
    join_meeting,
)
from johnny.voice_pipeline.event_bus import InMemoryEventBus
from johnny.voice_pipeline.events import SessionStatusChanged

MEET_LINK = "https://meet.google.com/abc-defg-hij"
SESSION_ID = "sess-1"


class _FakeElement:
    """Truthy placeholder returned by ``query_selector`` on a match."""

    def __init__(self, selector: str) -> None:
        self.selector = selector


class _FakePage:
    """Stateful Page double for the join-flow tests.

    ``visible`` is the current set of selectors that "exist" in the DOM.
    Tests pre-populate it; click handlers may mutate it (e.g. the join
    button vanishes after it's clicked, simulating navigation past the
    preview screen).

    ``vanish_on_click`` is a convenience helper: any selector clicked
    that's listed here is removed from ``visible``. Avoids hand-writing
    a callback for the common "click makes element disappear" case.
    """

    def __init__(
        self,
        *,
        initial_visible: set[str] | None = None,
        vanish_on_click: set[str] | None = None,
        goto_raises: BaseException | None = None,
        query_raises: dict[str, BaseException] | None = None,
        click_raises: dict[str, BaseException] | None = None,
    ) -> None:
        self.visible: set[str] = set(initial_visible or ())
        self.vanish_on_click: set[str] = set(vanish_on_click or ())
        self._goto_raises = goto_raises
        self._query_raises = dict(query_raises or {})
        self._click_raises = dict(click_raises or {})
        self.url = ""
        self.actions: list[tuple[str, str]] = []
        self.closed = False

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.actions.append(("goto", url))
        if self._goto_raises is not None:
            raise self._goto_raises
        self.url = url

    async def query_selector(self, selector: str) -> Any | None:
        self.actions.append(("query_selector", selector))
        if selector in self._query_raises:
            raise self._query_raises[selector]
        if selector in self.visible:
            return _FakeElement(selector)
        return None

    async def click(self, selector: str, **_kwargs: Any) -> None:
        self.actions.append(("click", selector))
        if selector in self._click_raises:
            raise self._click_raises[selector]
        if selector in self.vanish_on_click:
            self.visible.discard(selector)

    async def title(self) -> str:
        return "Meet"

    async def content(self) -> str:
        return "<html>fake</html>"

    async def close(self) -> None:
        self.actions.append(("close", ""))
        self.closed = True


def _joiner(page: _FakePage, **overrides: Any) -> MeetJoiner:
    """Build a MeetJoiner with short timeouts so tests run fast."""
    bus = overrides.pop("event_bus", None)
    defaults: dict[str, Any] = dict(
        meet_link=MEET_LINK,
        session_id=SESSION_ID,
        event_bus=bus,
        join_timeout_s=2.0,
        preview_timeout_s=0.5,
        poll_interval_s=0.02,
    )
    defaults.update(overrides)
    return MeetJoiner(page, **defaults)


# --- Constructor validation ------------------------------------------------


def test_constructor_rejects_non_positive_timeouts() -> None:
    page = _FakePage()
    with pytest.raises(ValueError):
        MeetJoiner(
            page, meet_link=MEET_LINK, session_id=SESSION_ID, join_timeout_s=0
        )
    with pytest.raises(ValueError):
        MeetJoiner(
            page, meet_link=MEET_LINK, session_id=SESSION_ID, preview_timeout_s=-1
        )
    with pytest.raises(ValueError):
        MeetJoiner(
            page, meet_link=MEET_LINK, session_id=SESSION_ID, poll_interval_s=0
        )


# --- Happy path -----------------------------------------------------------


async def test_join_navigates_to_meet_link() -> None:
    page = _FakePage(
        initial_visible={JOIN_BUTTON_SELECTORS[0]},
        vanish_on_click={JOIN_BUTTON_SELECTORS[0]},
    )
    result = await _joiner(page).join()
    assert isinstance(result, JoinResult)
    assert result.meet_link == MEET_LINK
    assert result.session_id == SESSION_ID
    assert result.joined_at_ms > 0
    assert ("goto", MEET_LINK) in page.actions


async def test_join_clicks_join_now_button() -> None:
    page = _FakePage(
        initial_visible={JOIN_BUTTON_SELECTORS[0]},
        vanish_on_click={JOIN_BUTTON_SELECTORS[0]},
    )
    await _joiner(page).join()
    click_actions = [a for a in page.actions if a[0] == "click"]
    assert (("click", JOIN_BUTTON_SELECTORS[0])) in click_actions


async def test_join_dismisses_camera_and_mic_prompts() -> None:
    page = _FakePage(
        initial_visible={
            JOIN_BUTTON_SELECTORS[0],
            MIC_OFF_SELECTORS[0],
            CAM_OFF_SELECTORS[0],
        },
        vanish_on_click={
            JOIN_BUTTON_SELECTORS[0],
            MIC_OFF_SELECTORS[0],
            CAM_OFF_SELECTORS[0],
        },
    )
    await _joiner(page).join()
    clicked = [a[1] for a in page.actions if a[0] == "click"]
    assert MIC_OFF_SELECTORS[0] in clicked
    assert CAM_OFF_SELECTORS[0] in clicked


async def test_join_does_not_click_mic_when_disabled() -> None:
    page = _FakePage(
        initial_visible={JOIN_BUTTON_SELECTORS[0], MIC_OFF_SELECTORS[0]},
        vanish_on_click={JOIN_BUTTON_SELECTORS[0], MIC_OFF_SELECTORS[0]},
    )
    await _joiner(page, mute_mic=False).join()
    clicked = [a[1] for a in page.actions if a[0] == "click"]
    assert MIC_OFF_SELECTORS[0] not in clicked


async def test_join_does_not_click_camera_when_disabled() -> None:
    page = _FakePage(
        initial_visible={JOIN_BUTTON_SELECTORS[0], CAM_OFF_SELECTORS[0]},
        vanish_on_click={JOIN_BUTTON_SELECTORS[0], CAM_OFF_SELECTORS[0]},
    )
    await _joiner(page, disable_camera=False).join()
    clicked = [a[1] for a in page.actions if a[0] == "click"]
    assert CAM_OFF_SELECTORS[0] not in clicked


async def test_join_falls_back_to_secondary_join_selector() -> None:
    """When the primary join selector isn't present, the fallback is used."""
    fallback = JOIN_BUTTON_SELECTORS[1]
    page = _FakePage(
        initial_visible={fallback},
        vanish_on_click={fallback},
    )
    await _joiner(page).join()
    clicked = [a[1] for a in page.actions if a[0] == "click"]
    assert fallback in clicked
    assert JOIN_BUTTON_SELECTORS[0] not in clicked


# --- Blocker detection ----------------------------------------------------


async def test_join_raises_meeting_not_started() -> None:
    page = _FakePage(initial_visible={MEETING_NOT_STARTED_SELECTORS[0]})
    with pytest.raises(MeetingNotStartedError):
        await _joiner(page).join()


async def test_join_raises_access_denied() -> None:
    page = _FakePage(initial_visible={ACCESS_DENIED_SELECTORS[0]})
    with pytest.raises(MeetingAccessDeniedError):
        await _joiner(page).join()


async def test_join_raises_sign_in_required() -> None:
    page = _FakePage(initial_visible={SIGN_IN_REQUIRED_SELECTORS[0]})
    with pytest.raises(MeetSignInError):
        await _joiner(page).join()


async def test_blocker_check_prefers_sign_in_over_other_states() -> None:
    """If multiple blockers are visible, sign-in wins (most specific failure)."""
    page = _FakePage(
        initial_visible={
            SIGN_IN_REQUIRED_SELECTORS[0],
            ACCESS_DENIED_SELECTORS[0],
        }
    )
    with pytest.raises(MeetSignInError):
        await _joiner(page).join()


# --- Timeouts -------------------------------------------------------------


async def test_join_raises_timeout_when_join_button_persists() -> None:
    """Join button stays visible after click — joined-state poll times out."""
    page = _FakePage(initial_visible={JOIN_BUTTON_SELECTORS[0]})
    with pytest.raises(MeetJoinTimeoutError):
        await _joiner(page, join_timeout_s=0.15).join()


async def test_join_fails_when_join_button_never_appears() -> None:
    page = _FakePage(initial_visible=set())
    with pytest.raises(MeetJoinError):
        await _joiner(page).join()


async def test_navigation_failure_is_wrapped() -> None:
    page = _FakePage(goto_raises=RuntimeError("connection refused"))
    with pytest.raises(MeetJoinError, match="navigation"):
        await _joiner(page).join()


# --- Status events --------------------------------------------------------


async def test_join_emits_joining_then_joined_on_success() -> None:
    page = _FakePage(
        initial_visible={JOIN_BUTTON_SELECTORS[0]},
        vanish_on_click={JOIN_BUTTON_SELECTORS[0]},
    )
    bus = InMemoryEventBus()
    await _joiner(page, event_bus=bus).join()
    statuses = [
        e for e in bus.snapshot() if isinstance(e, SessionStatusChanged)
    ]
    assert [e.status for e in statuses] == ["joining", "joined"]
    assert all(e.session_id == SESSION_ID for e in statuses)


async def test_join_emits_joining_then_failed_on_blocker() -> None:
    page = _FakePage(initial_visible={ACCESS_DENIED_SELECTORS[0]})
    bus = InMemoryEventBus()
    with pytest.raises(MeetingAccessDeniedError):
        await _joiner(page, event_bus=bus).join()
    statuses = [
        e for e in bus.snapshot() if isinstance(e, SessionStatusChanged)
    ]
    assert statuses[0].status == "joining"
    assert statuses[-1].status == "failed"
    assert statuses[-1].error_reason is not None
    assert "not allowed" in statuses[-1].error_reason


async def test_join_emits_failed_on_unexpected_exception() -> None:
    """Non-MeetJoinError exceptions are wrapped + reported as failed."""
    page = _FakePage(
        initial_visible={JOIN_BUTTON_SELECTORS[0]},
        click_raises={JOIN_BUTTON_SELECTORS[0]: RuntimeError("boom")},
    )
    bus = InMemoryEventBus()
    with pytest.raises(MeetJoinError):
        await _joiner(page, event_bus=bus).join()
    statuses = [
        e for e in bus.snapshot() if isinstance(e, SessionStatusChanged)
    ]
    assert statuses[-1].status == "failed"


async def test_join_no_event_bus_is_a_noop() -> None:
    """When no event bus is wired, the join still runs end-to-end."""
    page = _FakePage(
        initial_visible={JOIN_BUTTON_SELECTORS[0]},
        vanish_on_click={JOIN_BUTTON_SELECTORS[0]},
    )
    result = await _joiner(page, event_bus=None).join()
    assert result.session_id == SESSION_ID


async def test_publish_failure_does_not_break_join() -> None:
    """A flaky event bus is logged but never crashes the join."""

    class _BrokenBus(InMemoryEventBus):
        async def publish(self, event: Any) -> None:
            raise RuntimeError("bus down")

    page = _FakePage(
        initial_visible={JOIN_BUTTON_SELECTORS[0]},
        vanish_on_click={JOIN_BUTTON_SELECTORS[0]},
    )
    # Should NOT raise — publish errors are swallowed.
    result = await _joiner(page, event_bus=_BrokenBus()).join()
    assert isinstance(result, JoinResult)


# --- Error hierarchy ------------------------------------------------------


def test_error_hierarchy() -> None:
    """All structured failures derive from MeetJoinError so callers can
    catch the base type when they don't care which one fired."""
    assert issubclass(MeetSignInError, MeetJoinError)
    assert issubclass(MeetingNotStartedError, MeetJoinError)
    assert issubclass(MeetingAccessDeniedError, MeetJoinError)
    assert issubclass(MeetJoinTimeoutError, MeetJoinError)


# --- join_meeting() entry point ------------------------------------------


async def test_join_meeting_raises_when_playwright_missing() -> None:
    """The high-level entry point fails loud if playwright isn't installed.

    The meet-worker image ships playwright; this guards against accidental
    invocation from the API/worker containers where playwright is absent.
    """
    import importlib
    import sys

    original_import = importlib.import_module
    original_module = sys.modules.pop("playwright.async_api", None)
    original_root = sys.modules.pop("playwright", None)

    def _fake_import(name: str, package: str | None = None) -> Any:
        if name == "playwright.async_api":
            raise ImportError("no playwright in this env")
        return original_import(name, package)

    importlib.import_module = _fake_import
    try:
        with pytest.raises(MeetJoinError, match="playwright is not installed"):
            await join_meeting(meet_link=MEET_LINK, session_id=SESSION_ID)
    finally:
        importlib.import_module = original_import
        if original_module is not None:
            sys.modules["playwright.async_api"] = original_module
        if original_root is not None:
            sys.modules["playwright"] = original_root
