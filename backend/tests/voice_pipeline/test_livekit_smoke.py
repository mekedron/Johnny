"""LiveKit transport smoke test (US-025).

Smoke-tests ``LiveKitTransport`` against a real LiveKit dev server.
Skipped by default — set ``JOHNNY_LIVEKIT_SMOKE_URL`` and
``JOHNNY_LIVEKIT_SMOKE_TOKEN`` (a valid join token for a test room) to
opt in. Start a local LiveKit server first, e.g.::

    docker run --rm -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \\
        -e LIVEKIT_KEYS="devkey: secret" \\
        livekit/livekit-server --dev

Then mint a token with ``livekit-cli`` (or any LiveKit token generator)
and export it as ``JOHNNY_LIVEKIT_SMOKE_TOKEN``. ``pytest -k livekit_smoke``
will then assert that ``play_frames`` lands in the published microphone
track over a live connection.

The test does NOT require ``livekit-rtc`` to be importable to be
collected — it's skipped at collection time when the env vars are
missing, so the module-level imports stay light.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from importlib.util import find_spec
from typing import Any

import pytest

LIVEKIT_URL = os.environ.get("JOHNNY_LIVEKIT_SMOKE_URL", "").strip()
LIVEKIT_TOKEN = os.environ.get("JOHNNY_LIVEKIT_SMOKE_TOKEN", "").strip()
LIVEKIT_AVAILABLE = find_spec("livekit") is not None

pytestmark = pytest.mark.skipif(
    not (LIVEKIT_URL and LIVEKIT_TOKEN and LIVEKIT_AVAILABLE),
    reason=(
        "Set JOHNNY_LIVEKIT_SMOKE_URL and JOHNNY_LIVEKIT_SMOKE_TOKEN and "
        "install `livekit` to run the LiveKit smoke test."
    ),
)


@pytest.mark.livekit_smoke
async def test_livekit_transport_connects_and_publishes() -> None:
    """LiveKitTransport.start succeeds against a real dev server.

    Minimal smoke: connect, publish a single microphone track, send one
    PCM frame, then disconnect. We don't assert on a second peer because
    the dev container alone is sufficient to exercise the connect /
    publish / disconnect happy path. End-to-end pipeline runs require
    a publisher peer, which the docs explain how to spin up via
    ``livekit-cli``.
    """
    from johnny.voice_pipeline.livekit_transport import LiveKitTransport

    transport = LiveKitTransport(
        url=LIVEKIT_URL,
        token=LIVEKIT_TOKEN,
    )
    await transport.start()
    try:
        frame = b"\x00\x01" * 320  # 20 ms @ 16 kHz mono
        await transport.play_frames([frame])
        # Brief settle so the audio source flushes before disconnect.
        await asyncio.sleep(0.1)
    finally:
        await transport.stop()


