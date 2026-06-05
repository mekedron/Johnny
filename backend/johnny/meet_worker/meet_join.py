"""Playwright-driven Google Meet join automation (US-020).

The meet-worker container spawns this driver per session: it opens
Chromium under Xvfb, authenticates with the selected Google account
(via persisted ``storage_state`` from a prior sign-in), navigates to
the Meet link, dismisses the preview prompts (camera/mic permission,
intro modal), keeps mic muted and camera off by default, and clicks
"Join now".

The driver emits :class:`SessionStatusChanged` events on every
lifecycle transition (``joining`` → ``joined`` or ``joining`` →
``failed``) so the API subscriber can update ``bot_sessions.status``
in PostgreSQL and re-broadcast the change on the global WebSocket
channel (US-031).

Join failures map to structured exceptions:

* :class:`MeetSignInError` — storage_state missing or expired
* :class:`MeetingNotStartedError` — meeting hasn't started yet
* :class:`MeetingAccessDeniedError` — bot account isn't allowed in
* :class:`MeetJoinTimeoutError` — preview UI never resolved
* :class:`MeetJoinError` — anything else (base class)

The driver is split in two pieces for testability:

* :class:`MeetJoiner` runs the join flow against any object that
  satisfies the :class:`_Page` Protocol. Tests inject a fake.
* :func:`join_meeting` is the production entry point — it opens a
  real Chromium browser via Playwright, builds a :class:`MeetJoiner`,
  and runs the join. Playwright is imported lazily so this module
  stays importable in environments that don't ship the package.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from johnny.voice_pipeline.event_bus import EventBus
from johnny.voice_pipeline.events import SessionStatus, SessionStatusChanged

logger = logging.getLogger(__name__)

DEFAULT_JOIN_TIMEOUT_S = 60.0
"""Total wallclock time the join flow may take (navigation + click + wait)."""

DEFAULT_PREVIEW_TIMEOUT_S = 10.0
"""How long to wait for a single preview-UI element (Join button, mic toggle)."""

DEFAULT_POLL_INTERVAL_S = 0.25
"""Re-check selectors at this cadence inside :meth:`MeetJoiner._click_first_present`."""


# Selectors for the preview / in-meeting UI elements. Lists are tried in
# order; first visible match wins. Google Meet's DOM is unstable across
# rollouts so we keep multiple locators (jsname, aria-label, text) for
# each element. Adding new selectors to the front of a tuple is the
# preferred way to keep pace with UI changes — the existing fallbacks
# stay as a safety net.
JOIN_BUTTON_SELECTORS: tuple[str, ...] = (
    'button[jsname="Qx7uuf"]',
    'button:has-text("Join now")',
    '[aria-label*="Join now"]',
    'div[role="button"]:has-text("Join now")',
)
MIC_OFF_SELECTORS: tuple[str, ...] = (
    '[aria-label*="Turn off microphone"]',
    'div[role="button"][data-is-muted="false"][data-tooltip*="microphone"]',
    '[data-tooltip*="Turn off microphone"]',
)
CAM_OFF_SELECTORS: tuple[str, ...] = (
    '[aria-label*="Turn off camera"]',
    'div[role="button"][data-is-muted="false"][data-tooltip*="camera"]',
    '[data-tooltip*="Turn off camera"]',
)
MEETING_NOT_STARTED_SELECTORS: tuple[str, ...] = (
    "text=/This meeting hasn't started/i",
    "text=/Meeting not started/i",
    "text=/The host hasn't started/i",
)
ACCESS_DENIED_SELECTORS: tuple[str, ...] = (
    "text=/You can't join this video call/i",
    "text=/access has been denied/i",
    "text=/You are not allowed to join/i",
    "text=/Sorry, you cannot join/i",
)
SIGN_IN_REQUIRED_SELECTORS: tuple[str, ...] = (
    'a[href*="accounts.google.com/ServiceLogin"]',
    'input[type="email"][name="identifier"]',
    "text=/Sign in to join/i",
)


class MeetJoinError(Exception):
    """Base class for every Google Meet join failure."""


class MeetSignInError(MeetJoinError):
    """Google sign-in is required (no usable cookies / consent expired)."""


class MeetingNotStartedError(MeetJoinError):
    """The meeting has not started yet — try again later."""


class MeetingAccessDeniedError(MeetJoinError):
    """Bot account is not allowed to join this meeting."""


class MeetJoinTimeoutError(MeetJoinError):
    """The join flow exceeded the configured timeout."""


@dataclass(frozen=True, slots=True)
class JoinResult:
    """Successful join outcome surfaced to the caller."""

    session_id: str
    meet_link: str
    joined_at_ms: int


# --- Playwright surface we use -------------------------------------------
#
# Each Protocol exposes only the methods MeetJoiner actually calls. Tests
# substitute fakes that implement the same shape; production passes the
# real ``Page`` / ``BrowserContext`` / ``Browser`` instances returned by
# Playwright. ``runtime_checkable`` lets isinstance() work where needed.


@runtime_checkable
class _Page(Protocol):
    url: str

    async def goto(self, url: str, **kwargs: Any) -> Any: ...

    async def query_selector(self, selector: str) -> Any | None: ...

    async def click(self, selector: str, **kwargs: Any) -> None: ...

    async def title(self) -> str: ...

    async def content(self) -> str: ...

    async def close(self) -> None: ...


def _now_ms() -> int:
    return int(time.time() * 1000)


class MeetJoiner:
    """Drive a Playwright Page through the Google Meet join flow.

    Used by production code (where ``page`` is a real Playwright ``Page``)
    and tests (where ``page`` is a fake implementing the same Protocol).
    Splitting the driver from the Playwright launch keeps the flow logic
    unit-testable without spinning up Chromium.

    The constructor accepts override hooks for every selector list so
    individual tests can scope behaviour without rebuilding the module.
    """

    def __init__(
        self,
        page: _Page,
        *,
        meet_link: str,
        session_id: str,
        event_bus: EventBus | None = None,
        mute_mic: bool = True,
        disable_camera: bool = True,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        preview_timeout_s: float = DEFAULT_PREVIEW_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        join_button_selectors: Sequence[str] = JOIN_BUTTON_SELECTORS,
        mic_off_selectors: Sequence[str] = MIC_OFF_SELECTORS,
        cam_off_selectors: Sequence[str] = CAM_OFF_SELECTORS,
        meeting_not_started_selectors: Sequence[str] = MEETING_NOT_STARTED_SELECTORS,
        access_denied_selectors: Sequence[str] = ACCESS_DENIED_SELECTORS,
        sign_in_required_selectors: Sequence[str] = SIGN_IN_REQUIRED_SELECTORS,
    ) -> None:
        if join_timeout_s <= 0 or preview_timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError(
                "join_timeout_s, preview_timeout_s, and poll_interval_s must be positive"
            )
        self._page = page
        self._meet_link = meet_link
        self._session_id = session_id
        self._event_bus = event_bus
        self._mute_mic = mute_mic
        self._disable_camera = disable_camera
        self._join_timeout_s = join_timeout_s
        self._preview_timeout_s = preview_timeout_s
        self._poll_interval_s = poll_interval_s
        self._join_button_selectors = tuple(join_button_selectors)
        self._mic_off_selectors = tuple(mic_off_selectors)
        self._cam_off_selectors = tuple(cam_off_selectors)
        self._meeting_not_started_selectors = tuple(meeting_not_started_selectors)
        self._access_denied_selectors = tuple(access_denied_selectors)
        self._sign_in_required_selectors = tuple(sign_in_required_selectors)

    async def join(self) -> JoinResult:
        """Run the full join flow. Emits status events on transitions."""
        await self._publish_status("joining")
        try:
            await self._navigate()
            await self._check_for_blockers()
            await self._mute_av()
            await self._click_join_button()
            await self._wait_for_joined_state()
        except MeetJoinError as exc:
            await self._publish_status("failed", error_reason=str(exc))
            raise
        except Exception as exc:
            msg = f"unexpected error during Meet join: {exc}"
            await self._publish_status("failed", error_reason=msg)
            raise MeetJoinError(msg) from exc
        result = JoinResult(
            session_id=self._session_id,
            meet_link=self._meet_link,
            joined_at_ms=_now_ms(),
        )
        await self._publish_status("joined")
        return result

    async def _navigate(self) -> None:
        try:
            await self._page.goto(
                self._meet_link,
                wait_until="domcontentloaded",
                timeout=int(self._join_timeout_s * 1000),
            )
        except Exception as exc:
            raise MeetJoinError(
                f"navigation to {self._meet_link!r} failed: {exc}"
            ) from exc

    async def _check_for_blockers(self) -> None:
        """Look for sign-in / access-denied / not-started states; raise if found."""
        if await self._any_selector_visible(self._sign_in_required_selectors):
            raise MeetSignInError(
                "Google sign-in required — storage_state is missing or expired"
            )
        if await self._any_selector_visible(self._access_denied_selectors):
            raise MeetingAccessDeniedError(
                "Bot account is not allowed to join this meeting"
            )
        if await self._any_selector_visible(self._meeting_not_started_selectors):
            raise MeetingNotStartedError("Meeting has not started yet")

    async def _mute_av(self) -> None:
        """Click mic/camera toggles if visible. Best-effort — never raises."""
        if self._mute_mic:
            await self._click_first_present(
                self._mic_off_selectors,
                timeout_s=self._preview_timeout_s,
                what="mic toggle",
                required=False,
            )
        if self._disable_camera:
            await self._click_first_present(
                self._cam_off_selectors,
                timeout_s=self._preview_timeout_s,
                what="camera toggle",
                required=False,
            )

    async def _click_join_button(self) -> None:
        clicked = await self._click_first_present(
            self._join_button_selectors,
            timeout_s=self._preview_timeout_s,
            what="Join now button",
            required=True,
        )
        if not clicked:
            # _click_first_present(required=True) raises on miss; defensive.
            raise MeetJoinError("Join now button click did not register")

    async def _wait_for_joined_state(self) -> None:
        """Confirm the page navigated past the preview screen.

        Once the join button is clicked, the Meet UI replaces the preview
        with the in-meeting toolbar. The simplest cross-rollout signal:
        the Join now selectors disappear. We poll until that happens or
        the join timeout elapses.
        """
        deadline = time.monotonic() + self._join_timeout_s
        while time.monotonic() < deadline:
            if not await self._any_selector_visible(self._join_button_selectors):
                return
            await asyncio.sleep(self._poll_interval_s)
        raise MeetJoinTimeoutError(
            f"Did not reach in-meeting state within {self._join_timeout_s:.1f}s"
        )

    async def _click_first_present(
        self,
        selectors: Sequence[str],
        *,
        timeout_s: float,
        what: str,
        required: bool,
    ) -> bool:
        """Click the first visible selector. Polls until the timeout elapses.

        Returns ``True`` on a successful click; ``False`` when nothing matched
        and ``required`` is ``False``. When ``required`` is ``True`` and the
        timeout expires without a match, raises :class:`MeetJoinError`.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            for selector in selectors:
                element = await self._safe_query_selector(selector)
                if element is None:
                    continue
                try:
                    await self._page.click(selector)
                    return True
                except Exception as exc:
                    logger.debug("click(%s) for %s failed: %s", selector, what, exc)
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self._poll_interval_s)
        if required:
            raise MeetJoinError(
                f"{what} not visible within {timeout_s:.1f}s "
                f"(tried selectors: {list(selectors)})"
            )
        logger.info("%s not visible within %.1fs — skipping", what, timeout_s)
        return False

    async def _any_selector_visible(self, selectors: Sequence[str]) -> bool:
        for selector in selectors:
            element = await self._safe_query_selector(selector)
            if element is not None:
                return True
        return False

    async def _safe_query_selector(self, selector: str) -> Any | None:
        try:
            return await self._page.query_selector(selector)
        except Exception as exc:
            logger.debug("query_selector(%s) failed: %s", selector, exc)
            return None

    async def _publish_status(
        self,
        status: SessionStatus,
        *,
        error_reason: str | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        event = SessionStatusChanged(
            status=status,
            timestamp_ms=_now_ms(),
            session_id=self._session_id,
            error_reason=error_reason,
        )
        try:
            await self._event_bus.publish(event)
        except Exception:
            logger.exception("failed to publish session status event")


# --- High-level entry point that opens a real Playwright browser --------


async def join_meeting(
    *,
    meet_link: str,
    session_id: str,
    storage_state_path: Path | None = None,
    event_bus: EventBus | None = None,
    mute_mic: bool = True,
    disable_camera: bool = True,
    join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
    preview_timeout_s: float = DEFAULT_PREVIEW_TIMEOUT_S,
    headless: bool = False,
) -> JoinResult:
    """Open a real Chromium browser and run the join flow.

    Production entry point for the meet-worker. Playwright is imported
    lazily so this module stays importable in environments without the
    package (the API and worker containers, the tests).

    The browser is launched with ``--use-fake-ui-for-media-stream`` so
    Chromium auto-accepts microphone/camera permission for the Meet
    origin — combined with PulseAudio's virtual sink/source from the
    entrypoint script (US-019), this lets Meet capture from / play to
    the bridges that the voice pipeline drives.

    When ``storage_state_path`` is provided and exists, the context is
    initialised with the prior sign-in cookies + localStorage so the
    bot loads straight into the joined-in identity. Missing file or
    expired cookies surface as :class:`MeetSignInError` at the blocker
    check.
    """
    try:
        pw_module = importlib.import_module("playwright.async_api")
    except ImportError as exc:
        raise MeetJoinError(
            "playwright is not installed in this environment; ensure the "
            "meet-worker Dockerfile installs the playwright package and "
            "Chromium browser binaries"
        ) from exc

    async_playwright = pw_module.async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 720},
            "permissions": ["microphone", "camera"],
        }
        if storage_state_path is not None and storage_state_path.exists():
            context_kwargs["storage_state"] = str(storage_state_path)
        try:
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            joiner = MeetJoiner(
                page,
                meet_link=meet_link,
                session_id=session_id,
                event_bus=event_bus,
                mute_mic=mute_mic,
                disable_camera=disable_camera,
                join_timeout_s=join_timeout_s,
                preview_timeout_s=preview_timeout_s,
            )
            return await joiner.join()
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


__all__ = [
    "ACCESS_DENIED_SELECTORS",
    "CAM_OFF_SELECTORS",
    "DEFAULT_JOIN_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_PREVIEW_TIMEOUT_S",
    "JOIN_BUTTON_SELECTORS",
    "JoinResult",
    "MEETING_NOT_STARTED_SELECTORS",
    "MIC_OFF_SELECTORS",
    "MeetJoinError",
    "MeetJoinTimeoutError",
    "MeetJoiner",
    "MeetSignInError",
    "MeetingAccessDeniedError",
    "MeetingNotStartedError",
    "SIGN_IN_REQUIRED_SELECTORS",
    "join_meeting",
]
