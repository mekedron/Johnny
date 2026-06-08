"""Tests for the bot-session Playwright probe orchestration (Johnny-ckz.24).

The unit tests inject a fake Docker client so the spawn/wait/parse/cleanup
logic is exercised without a daemon. ``test_live_*`` actually spawns the
real probe container against fake cookies and asserts Google rejects them
— it is opt-in (``JOHNNY_PROBE_LIVE=1``) and skips cleanly otherwise, so
normal CI never needs Docker or the network.
"""

from __future__ import annotations

import json
import os
from typing import Any, cast

import pytest

from app.services.bot_session_probe import (
    PROBE_ACCOUNT_LABEL,
    RESULT_PREFIX,
    BotSessionProber,
    BotSessionProbeResult,
    BotSessionProbeUnavailableError,
    get_auth_volume,
    parse_probe_result,
)

# --- Fakes -----------------------------------------------------------------


class _FakeNotFoundError(Exception):
    """Stands in for docker.errors.NotFound (detected by class name)."""


class _FakeImageNotFoundError(Exception):
    """Stands in for docker.errors.ImageNotFound (detected by class name)."""


# The probe detects these by ``type(exc).__name__``, so override the
# runtime names to the exact SDK names (the source names keep the
# Error suffix for the linter).
_FakeImageNotFoundError.__name__ = "ImageNotFound"
_FakeNotFoundError.__name__ = "NotFound"


class _FakeContainer:
    def __init__(
        self,
        *,
        name: str = "johnny-bot-probe",
        status: str = "exited",
        logs: bytes = b"",
    ) -> None:
        self.id = name
        self.name = name
        self.status = status
        self.attrs: dict[str, Any] = {}
        self._logs = logs
        self.reload_calls = 0
        self.stop_calls = 0
        self.remove_calls = 0
        self.removed = False

    def reload(self) -> None:
        self.reload_calls += 1

    def logs(self, *, tail: Any = None, stdout: bool = True, stderr: bool = True) -> bytes:
        return self._logs

    def stop(self, *, timeout: int = 0) -> None:
        self.stop_calls += 1

    def remove(self, *, force: bool = False) -> None:
        self.remove_calls += 1
        self.removed = True


class _FakeContainers:
    def __init__(self) -> None:
        self.run_calls: list[tuple[str, dict[str, Any]]] = []
        self.next_container: _FakeContainer | None = None
        self.raise_on_run: BaseException | None = None

    def run(self, image: str, **kwargs: Any) -> _FakeContainer:
        self.run_calls.append((image, kwargs))
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return self.next_container or _FakeContainer()


class _FakeClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubProber(BotSessionProber):
    def __init__(self, client: _FakeClient, **kwargs: Any) -> None:
        super().__init__(client=cast(Any, client), **kwargs)
        self._fake = client

    def _create_client(self) -> Any:
        return self._fake


def _result_logs(payload: dict[str, Any]) -> bytes:
    body = (
        b"some startup noise\n"
        + (RESULT_PREFIX + json.dumps(payload)).encode("utf-8")
        + b"\ntrailing line\n"
    )
    return body


@pytest.fixture
def fake_client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def prober(fake_client: _FakeClient) -> _StubProber:
    return _StubProber(fake_client, auth_volume="google_auth_state", network=None)


# --- parse_probe_result ----------------------------------------------------


def test_parse_probe_result_takes_last_line() -> None:
    logs = (
        "boot\n"
        + RESULT_PREFIX
        + '{"signed_in": false}\n'
        + "more\n"
        + RESULT_PREFIX
        + '{"signed_in": true, "email": "x@y.com", "final_url": "u"}\n'
    )
    result = parse_probe_result(logs)
    assert result.signed_in is True
    assert result.email == "x@y.com"
    assert result.final_url == "u"


def test_parse_probe_result_no_line_raises() -> None:
    with pytest.raises(BotSessionProbeUnavailableError, match="no result line"):
        parse_probe_result("just logs, no verdict\n")


def test_parse_probe_result_bad_json_raises() -> None:
    with pytest.raises(BotSessionProbeUnavailableError, match="not valid JSON"):
        parse_probe_result(RESULT_PREFIX + "{not json}")


# --- BotSessionProber.probe ------------------------------------------------


def test_probe_signed_in(prober: _StubProber, fake_client: _FakeClient) -> None:
    fake_client.containers.next_container = _FakeContainer(
        logs=_result_logs(
            {"signed_in": True, "email": "bot@x.com", "final_url": "m"}
        )
    )
    result = prober.probe(7)
    assert isinstance(result, BotSessionProbeResult)
    assert result.signed_in is True
    assert result.email == "bot@x.com"


def test_probe_run_kwargs(prober: _StubProber, fake_client: _FakeClient) -> None:
    fake_client.containers.next_container = _FakeContainer(
        logs=_result_logs({"signed_in": False})
    )
    prober.probe(42)
    image, kwargs = fake_client.containers.run_calls[0]
    assert image == prober.image
    # Override the noVNC entrypoint with a plain headless python run, and
    # pin command so the image CMD (the supervisor) is NOT appended.
    assert kwargs["entrypoint"] == ["python"]
    assert kwargs["command"] == ["-m", "johnny.bot_signin.probe"]
    assert kwargs["environment"]["JOHNNY_PROBE_ACCOUNT_ID"] == "42"
    assert kwargs["labels"][PROBE_ACCOUNT_LABEL] == "42"
    # Cookies mounted READ-ONLY at the meet-worker's auth target.
    mount = kwargs["volumes"]["google_auth_state"]
    assert mount["mode"] == "ro"
    assert mount["bind"] == "/var/lib/johnny/google-auth"


def test_probe_removes_container_on_success(
    prober: _StubProber, fake_client: _FakeClient
) -> None:
    container = _FakeContainer(logs=_result_logs({"signed_in": True}))
    fake_client.containers.next_container = container
    prober.probe(1)
    assert container.removed is True


def test_probe_no_result_line_raises_and_cleans_up(
    prober: _StubProber, fake_client: _FakeClient
) -> None:
    container = _FakeContainer(logs=b"crashed before verdict\n")
    fake_client.containers.next_container = container
    with pytest.raises(BotSessionProbeUnavailableError):
        prober.probe(1)
    # Even when we can't parse a verdict, the container must be removed.
    assert container.removed is True


def test_probe_image_not_found_raises_unavailable(
    prober: _StubProber, fake_client: _FakeClient
) -> None:
    fake_client.containers.raise_on_run = _FakeImageNotFoundError("no image")
    with pytest.raises(BotSessionProbeUnavailableError, match="not built"):
        prober.probe(1)


def test_probe_run_failure_raises_unavailable(
    prober: _StubProber, fake_client: _FakeClient
) -> None:
    fake_client.containers.raise_on_run = RuntimeError("daemon exploded")
    with pytest.raises(BotSessionProbeUnavailableError, match="failed to start"):
        prober.probe(1)


def test_probe_timeout_raises_and_cleans_up(
    prober: _StubProber, fake_client: _FakeClient
) -> None:
    # Container that never exits → wait loop must time out, then stop+remove.
    container = _FakeContainer(status="running", logs=b"")
    fake_client.containers.next_container = container
    prober._wait_timeout = 0.2  # type: ignore[attr-defined]
    prober._poll_interval = 0.02  # type: ignore[attr-defined]
    with pytest.raises(BotSessionProbeUnavailableError, match="did not finish"):
        prober.probe(1)
    assert container.removed is True


def test_probe_container_gone_during_wait_is_treated_as_exited(
    fake_client: _FakeClient,
) -> None:
    class _VanishingContainer(_FakeContainer):
        def reload(self) -> None:
            super().reload()
            raise _FakeNotFoundError("gone")

    container = _VanishingContainer(
        status="running", logs=_result_logs({"signed_in": True})
    )
    fake_client.containers.next_container = container
    prober = _StubProber(fake_client, auth_volume="google_auth_state", network=None)
    result = prober.probe(1)
    assert result.signed_in is True


# --- config helpers --------------------------------------------------------


def test_get_auth_volume_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOHNNY_MEET_WORKER_AUTH_VOLUME", "none")
    assert get_auth_volume() is None
    monkeypatch.setenv("JOHNNY_MEET_WORKER_AUTH_VOLUME", "custom_vol")
    assert get_auth_volume() == "custom_vol"


# --- live (opt-in) ---------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("JOHNNY_PROBE_LIVE") != "1",
    reason="set JOHNNY_PROBE_LIVE=1 (needs Docker + built bot-signin image + network)",
)
def test_live_fake_cookies_report_not_signed_in() -> None:
    """Real round-trip: a fake storage_state must come back signed_in=False.

    Runs the actual probe container against real Google. Writes the fake
    cookies straight into the shared auth volume (this test is meant to run
    INSIDE the api container, where that volume is mounted RW), then reads
    the verdict back. Google bounces the fake cookies to the sign-in page,
    so the probe must report the session is not live.
    """
    from app.services.bot_auth_seed import bot_session_path, save_bot_session
    from app.services.bot_session_probe import probe_bot_session

    account_id = 990099  # high id unlikely to collide with a real row
    fake = json.dumps(
        {
            "cookies": [
                {
                    "name": "SID",
                    "value": "totally-fake",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 9999999999,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ],
            "origins": [],
        }
    ).encode("utf-8")
    save_bot_session(account_id, fake)
    try:
        result = probe_bot_session(account_id)
    finally:
        bot_session_path(account_id).unlink(missing_ok=True)
    assert result.signed_in is False
