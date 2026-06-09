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
from collections.abc import AsyncIterator, Sequence
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

DEFAULT_ADMISSION_TIMEOUT_S = 300.0
"""How long to wait in the lobby for a participant to admit an external
guest after clicking "Ask to join". Far longer than the preview timeout
because a human has to notice the knock and click "Admit"."""


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
# External guests — accounts not on the meeting host's org, or not invited —
# see "Ask to join" instead of "Join now" (Meet's knock-to-join flow).
# Clicking it puts the bot in the lobby until a human participant admits it,
# so without these selectors the bot finds no "Join now" button and times out
# (Johnny: gmail guest account couldn't join). Tried after the Join-now set so
# a direct join is always preferred when both could exist.
ASK_TO_JOIN_SELECTORS: tuple[str, ...] = (
    'button:has-text("Ask to join")',
    '[aria-label*="Ask to join"]',
    'div[role="button"]:has-text("Ask to join")',
)
# Shown once the knock is resolved negatively: a participant declined, or no
# one responded before Meet gave up. Lets the admission wait fail fast with a
# clear reason instead of burning the full admission timeout.
ADMISSION_DENIED_SELECTORS: tuple[str, ...] = (
    "text=/You can.?t join this( video)? call/i",
    "text=/no one responded to your request/i",
    "text=/request to join was denied/i",
    "text=/Your request to join wasn.?t accepted/i",
)
# Positive in-meeting markers. We wait for one of these to APPEAR after
# clicking Join so a browser disconnect (which would also make the Join
# button disappear) doesn't falsely register as "joined". Tried in order;
# first visible match wins.
IN_MEETING_SELECTORS: tuple[str, ...] = (
    '[aria-label*="Leave call"]',
    'button[jsname="CQylAd"]',  # the red leave-call button
    '[data-call-ended]',
    '[aria-label*="You can now talk"]',
    'div[role="region"][aria-label*="Meeting"]',
)
# Google shows this "Switch the call here" page when the bot account is
# *already* in the call from another browser/device — common when a
# previous meet-worker container didn't fully tear down before a new one
# tried to join. Without explicit handling the new session sees no Join
# Now button and times out (Johnny-d2g reliability bug). We click
# "Switch here" so the new container takes over the call; the older
# session loses the room (which it would have anyway when its container
# was stopped).
SWITCH_CALL_HERE_SELECTORS: tuple[str, ...] = (
    'button:has-text("Switch here")',
    '[aria-label*="Switch here"]',
    'button:has-text("Switch the call here")',
)
# The smaller "Got it" button next to the Switch prompt — useful when
# we'd rather dismiss the banner than switch. Currently unused; kept
# documented for the alternative dismiss-and-look-for-Join-Now path.
SWITCH_CALL_DISMISS_SELECTORS: tuple[str, ...] = (
    'button:has-text("Got it")',
    '[aria-label*="Dismiss"]',
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
        admission_timeout_s: float = DEFAULT_ADMISSION_TIMEOUT_S,
        join_button_selectors: Sequence[str] = JOIN_BUTTON_SELECTORS,
        ask_to_join_selectors: Sequence[str] = ASK_TO_JOIN_SELECTORS,
        admission_denied_selectors: Sequence[str] = ADMISSION_DENIED_SELECTORS,
        mic_off_selectors: Sequence[str] = MIC_OFF_SELECTORS,
        cam_off_selectors: Sequence[str] = CAM_OFF_SELECTORS,
        meeting_not_started_selectors: Sequence[str] = MEETING_NOT_STARTED_SELECTORS,
        access_denied_selectors: Sequence[str] = ACCESS_DENIED_SELECTORS,
        sign_in_required_selectors: Sequence[str] = SIGN_IN_REQUIRED_SELECTORS,
        in_meeting_selectors: Sequence[str] = IN_MEETING_SELECTORS,
        switch_call_selectors: Sequence[str] = SWITCH_CALL_HERE_SELECTORS,
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
        self._admission_timeout_s = admission_timeout_s
        self._join_button_selectors = tuple(join_button_selectors)
        self._ask_to_join_selectors = tuple(ask_to_join_selectors)
        self._admission_denied_selectors = tuple(admission_denied_selectors)
        self._mic_off_selectors = tuple(mic_off_selectors)
        self._cam_off_selectors = tuple(cam_off_selectors)
        self._meeting_not_started_selectors = tuple(meeting_not_started_selectors)
        self._access_denied_selectors = tuple(access_denied_selectors)
        self._sign_in_required_selectors = tuple(sign_in_required_selectors)
        self._in_meeting_selectors = tuple(in_meeting_selectors)
        self._switch_call_selectors = tuple(switch_call_selectors)

    async def join(self) -> JoinResult:
        """Run the full join flow. Emits status events on transitions."""
        logger.info(
            "join: starting flow session_id=%s meet_link=%s",
            self._session_id,
            self._meet_link,
        )
        await self._publish_status("joining")
        try:
            logger.info("join: stage=navigate session_id=%s", self._session_id)
            await self._navigate()
            logger.info(
                "join: stage=navigate done session_id=%s url=%s",
                self._session_id,
                getattr(self._page, "url", ""),
            )

            logger.info(
                "join: stage=blocker_check session_id=%s", self._session_id
            )
            await self._check_for_blockers()
            logger.info(
                "join: stage=blocker_check done session_id=%s (no blockers)",
                self._session_id,
            )

            logger.info("join: stage=mute_av session_id=%s", self._session_id)
            await self._mute_av()
            logger.info("join: stage=mute_av done session_id=%s", self._session_id)

            # Handle the "Switch the call here" page Google shows when
            # the bot account is already in the call from another
            # session. Clicking "Switch here" takes over the room and
            # transitions straight to in-meeting (no Join Now button
            # exists in that flow).
            switched = await self._handle_switch_call_prompt()

            if switched:
                action = "switched"
            else:
                action = await self._click_join_or_ask()
            logger.info(
                "join: stage=click_join session_id=%s switched=%s action=%s",
                self._session_id,
                switched,
                action,
            )

            if action == "ask_to_join":
                # External guest: the knock is in; a human in the meeting now
                # has to click "Admit". Wait far longer than a normal join and
                # watch for an explicit decline so we fail fast if rejected.
                logger.info(
                    "join: stage=await_admission session_id=%s "
                    "(external guest knocked; waiting up to %.0fs for a "
                    "participant to admit)",
                    self._session_id,
                    self._admission_timeout_s,
                )
                await self._wait_for_joined_state(
                    timeout_s=self._admission_timeout_s, check_denied=True
                )
            else:
                logger.info(
                    "join: stage=wait_joined session_id=%s "
                    "(polling in-meeting selector)",
                    self._session_id,
                )
                await self._wait_for_joined_state()
            logger.info(
                "join: stage=wait_joined done session_id=%s", self._session_id
            )
        except MeetJoinError as exc:
            logger.warning(
                "join: failure session_id=%s exc=%s",
                self._session_id,
                exc,
            )
            await self._capture_failure_screenshot()
            await self._publish_status("failed", error_reason=str(exc))
            raise
        except Exception as exc:
            msg = f"unexpected error during Meet join: {exc}"
            logger.exception(
                "join: unexpected error session_id=%s", self._session_id
            )
            await self._capture_failure_screenshot()
            await self._publish_status("failed", error_reason=msg)
            raise MeetJoinError(msg) from exc
        result = JoinResult(
            session_id=self._session_id,
            meet_link=self._meet_link,
            joined_at_ms=_now_ms(),
        )
        await self._publish_status("joined")
        logger.info("join: COMPLETE session_id=%s", self._session_id)
        return result

    async def _capture_failure_screenshot(self) -> None:
        """Best-effort: dump the current page to /tmp on a join failure.

        The periodic screenshot loop only starts once the bot is
        in-meeting, so a join that fails *before* admission (an
        "Ask to join" knock screen, a sign-in wall, a denied request)
        otherwise leaves no visual to debug from. Writes
        ``/tmp/johnny-screenshots/session-<id>-failure.png`` so the
        operator can ``docker cp`` it out. Never raises — diagnostics
        must not mask the original join error.
        """
        screenshot = getattr(self._page, "screenshot", None)
        if not callable(screenshot):
            return
        try:
            output_dir = Path("/tmp/johnny-screenshots")
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"session-{self._session_id}-failure.png"
            await screenshot(path=str(path), full_page=False)
            logger.info(
                "join: failure screenshot written session_id=%s path=%s "
                "(docker cp meet-worker-session-%s:%s .)",
                self._session_id,
                path,
                self._session_id,
                path,
            )
        except Exception as exc:  # noqa: BLE001 — screenshot is best-effort
            logger.warning(
                "join: failure screenshot failed session_id=%s: %s",
                self._session_id,
                exc,
            )

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

    async def _click_join_or_ask(self) -> str:
        """Click "Join now" if present, else "Ask to join" (external guest).

        Returns ``"join_now"`` for a direct join or ``"ask_to_join"`` when
        only the knock button was available (the caller then waits for a
        participant to admit the bot). Raises :class:`MeetJoinError` if
        neither button appears within the preview timeout — the same
        failure as before, now also covering the knock-to-join case.

        Join-now is tried first so an account that *can* join directly
        never falls into the (much slower) admission wait.
        """
        if await self._click_first_present(
            self._join_button_selectors,
            timeout_s=self._preview_timeout_s,
            what="Join now button",
            required=False,
        ):
            return "join_now"
        if await self._click_first_present(
            self._ask_to_join_selectors,
            timeout_s=self._preview_timeout_s,
            what="Ask to join button",
            required=False,
        ):
            return "ask_to_join"
        raise MeetJoinError(
            "neither 'Join now' nor 'Ask to join' button visible within "
            f"{self._preview_timeout_s:.1f}s "
            f"(join: {list(self._join_button_selectors)}; "
            f"ask: {list(self._ask_to_join_selectors)})"
        )

    async def _handle_switch_call_prompt(self) -> bool:
        """Click "Switch here" if Meet shows the multi-device prompt.

        Returns ``True`` when we clicked (and the caller should not
        treat a missing Join Now button as a failure); ``False`` when
        the prompt wasn't visible.

        Best-effort: when the prompt isn't visible we return immediately.
        When it is, clicking it transfers the call to this bot session
        and Meet skips straight to in-meeting state in that flow.
        """
        if not await self._any_selector_visible(self._switch_call_selectors):
            return False
        logger.info(
            "join: switch_call prompt detected — clicking 'Switch here' session_id=%s",
            self._session_id,
        )
        clicked = await self._click_first_present(
            self._switch_call_selectors,
            timeout_s=self._preview_timeout_s,
            what="Switch here button",
            required=False,
        )
        return clicked

    async def _wait_for_joined_state(
        self, *, timeout_s: float | None = None, check_denied: bool = False
    ) -> None:
        """Confirm the page is actually IN the meeting (positive signal).

        Originally the check was "Join now selectors disappeared" — but
        that fires falsely when the browser crashes mid-navigation: the
        page disconnects, query_selector raises, ``_safe_query_selector``
        returns None, and the bot misreports "joined" while the
        meeting room sees nothing (the symptom that fooled
        Johnny-ckz.1's initial fix).

        Positive signal: poll for one of :data:`IN_MEETING_SELECTORS`
        (the leave-call button etc.) to APPEAR. We still treat the Join
        now button disappearing as progress, but require the in-meeting
        signal to confirm the join.

        ``timeout_s`` overrides the default join timeout — the knock flow
        passes the (much longer) admission timeout. ``check_denied`` also
        polls :data:`ADMISSION_DENIED_SELECTORS` so a declined knock
        raises :class:`MeetingAccessDeniedError` immediately instead of
        waiting out the whole timeout.
        """
        timeout = self._join_timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._any_selector_visible(self._in_meeting_selectors):
                return
            if check_denied and await self._any_selector_visible(
                self._admission_denied_selectors
            ):
                raise MeetingAccessDeniedError(
                    "Admission request was declined or expired — a meeting "
                    "participant did not admit the bot"
                )
            await asyncio.sleep(self._poll_interval_s)
        raise MeetJoinTimeoutError(
            f"Did not reach in-meeting state within {timeout:.1f}s "
            f"(no in-meeting selector visible: {list(self._in_meeting_selectors)})"
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
    """Open a real Chromium browser, join the meeting, then tear down.

    Production entry point for one-shot joins (and unit tests). The
    browser is closed before the function returns; the meet-worker
    bootstrap that needs the browser to STAY in the meeting uses
    :func:`open_meeting_session` instead.
    """
    async with open_meeting_session(
        meet_link=meet_link,
        session_id=session_id,
        storage_state_path=storage_state_path,
        event_bus=event_bus,
        mute_mic=mute_mic,
        disable_camera=disable_camera,
        join_timeout_s=join_timeout_s,
        preview_timeout_s=preview_timeout_s,
        headless=headless,
    ) as session:
        return session.result


@dataclass
class OpenMeetingSession:
    """Handle a meet-worker holds while the bot is in the meeting.

    Yielded by :func:`open_meeting_session`. The caller idles until the
    parent container is told to stop, then exits the ``async with``
    block — Playwright tears down the browser cleanly.

    ``result`` is the :class:`JoinResult` from the join flow.
    ``is_alive()`` returns ``False`` once the page or browser has
    disconnected, which the bootstrap polls to detect a mid-meeting
    Chromium crash.
    """

    result: JoinResult
    _browser: Any
    _context: Any
    _page: Any

    async def is_alive(self) -> bool:
        """Whether the underlying Chromium is still connected.

        Polls a cheap selector. A disconnected browser raises and we
        return ``False`` so the bootstrap can exit the idle loop with
        a real ``error_reason`` instead of waiting forever.
        """
        try:
            # ``page.title()`` is a round-trip that fails fast on a
            # disconnected browser, unlike pure-Python attributes which
            # cache the last known value.
            await self._page.title()
            return True
        except Exception:
            return False


@contextlib.asynccontextmanager
async def open_meeting_session(
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
) -> AsyncIterator[OpenMeetingSession]:
    """Open Chromium, run the join flow, and hold the browser open.

    Production entry point for the meet-worker bootstrap. The browser
    stays alive for the lifetime of the ``async with`` block — so the
    bot stays in the meeting until the caller exits. Closing the
    context manager tears the browser down cleanly (the bot leaves the
    meeting).

    Playwright is imported lazily so this module stays importable in
    environments without the package (the API and worker containers,
    the tests).
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
            _attach_browser_log_forwarders(page, session_id=session_id)
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
            result = await joiner.join()
            yield OpenMeetingSession(
                result=result,
                _browser=browser,
                _context=context,
                _page=page,
            )
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


def _attach_browser_log_forwarders(page: Any, *, session_id: str) -> None:
    """Forward Chromium events to ``johnny.meet_worker.browser`` logger.

    Without this, Meet's behaviour is opaque to the operator — JS errors
    that crash the page never reach ``docker compose logs``. We forward:

    * ``console`` — every console.log/info/warn/error from Meet's JS bundle
    * ``pageerror`` — uncaught exceptions / promise rejections
    * ``crash`` — renderer process crash (separate from page close)
    * ``framenavigated`` — URL changes (useful for detecting silent
      sign-out or "kicked from meeting" redirects)
    * ``requestfailed`` — failed network requests (esp. to meet.google.com)

    Lots of noise in steady state; cheap to filter at ``docker logs |
    grep`` time. Worth it: when a join silently breaks, the first
    diagnostic question is "what did the browser actually do?".
    """
    browser_logger = logging.getLogger("johnny.meet_worker.browser")

    def _on_console(msg: Any) -> None:
        try:
            level = (msg.type or "log").lower()
            text = msg.text
            location = getattr(msg, "location", None) or {}
            url = location.get("url") if isinstance(location, dict) else ""
            browser_logger.log(
                logging.INFO if level not in {"error", "warning"} else logging.WARNING,
                "console session_id=%s level=%s url=%s text=%r",
                session_id,
                level,
                url,
                text,
            )
        except Exception:  # noqa: BLE001 — listener must never raise
            browser_logger.exception("console handler crashed")

    def _on_page_error(exc: Any) -> None:
        try:
            browser_logger.error(
                "page_error session_id=%s error=%r", session_id, exc
            )
        except Exception:  # noqa: BLE001
            browser_logger.exception("page_error handler crashed")

    def _on_crash(_page: Any) -> None:
        browser_logger.error(
            "page_crash session_id=%s — renderer died", session_id
        )

    def _on_framenavigated(frame: Any) -> None:
        try:
            if frame.parent_frame is None:  # main frame only
                browser_logger.info(
                    "navigation session_id=%s url=%s",
                    session_id,
                    frame.url,
                )
        except Exception:  # noqa: BLE001
            browser_logger.exception("framenavigated handler crashed")

    def _on_request_failed(request: Any) -> None:
        try:
            # Filter to meet.google.com so we don't drown in third-party
            # tracking failures.
            url = request.url
            if "google" not in url:
                return
            failure = (
                request.failure if hasattr(request, "failure") else None
            )
            browser_logger.warning(
                "request_failed session_id=%s method=%s url=%s failure=%s",
                session_id,
                request.method,
                url,
                failure,
            )
        except Exception:  # noqa: BLE001
            browser_logger.exception("request_failed handler crashed")

    page.on("console", _on_console)
    page.on("pageerror", _on_page_error)
    page.on("crash", _on_crash)
    page.on("framenavigated", _on_framenavigated)
    page.on("requestfailed", _on_request_failed)


__all__ = [
    "ACCESS_DENIED_SELECTORS",
    "CAM_OFF_SELECTORS",
    "DEFAULT_JOIN_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_PREVIEW_TIMEOUT_S",
    "IN_MEETING_SELECTORS",
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
    "OpenMeetingSession",
    "SIGN_IN_REQUIRED_SELECTORS",
    "SWITCH_CALL_DISMISS_SELECTORS",
    "SWITCH_CALL_HERE_SELECTORS",
    "join_meeting",
    "open_meeting_session",
]
