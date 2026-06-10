"""The livekit-agents 1.5.17 session-surface smoke as a pytest suite (Johnny-trt.2).

Runs the same in-image checks as ``python -m johnny.agent.sdk_surface_smoke``
(see that module's docstring for what each check proves and why Phases 3/5 of
Johnny-trt depend on them) so the SDK-surface contract stays continuously
verified, not just spot-checked once by the spike.

Guarded by ``importorskip`` so the suite still collects without the ``agent``
extra; the full-run tests load the baked Silero VAD and push real-time-paced
audio, so they pass inside the api/agent image
(``docker compose exec api pytest tests/agent/test_sdk_surface_smoke.py``)
and are skipped where the extra is absent. The user-state test is wall-clock
bound (~9 s of scripted audio timeline) — slow but irreplaceable: the away
timer and playout estimates are real-time by design.
"""

from __future__ import annotations

import pytest

pytest.importorskip("livekit.agents")

from johnny.agent.sdk_surface_smoke import (  # noqa: E402
    CheckResult,
    run_say_checks,
    run_say_no_sink_probe,
    run_user_state_checks,
)

# asyncio_mode = "auto" — async tests need no mark.


@pytest.fixture(scope="module")
def shared_vad() -> object:
    """Load Silero VAD once for the module (the slow part of session setup)."""
    from johnny.agent.session import load_vad

    return load_vad()


def _failures(checks: list[CheckResult]) -> list[str]:
    return [f"{c.name}: {c.detail}" for c in checks if not c.ok and not c.informational]


async def test_say_speech_handle_lifecycle(shared_vad: object) -> None:
    checks = await run_say_checks(shared_vad)  # type: ignore[arg-type]
    assert not _failures(checks), _failures(checks)
    # The named contracts Phase 3's ack terminal depends on are all present.
    names = {c.name for c in checks}
    assert {
        "say-returns-speech-handle",
        "say-done-callback-after-playout",
        "say-interrupt-surfaces-interrupted",
    } <= names


async def test_say_without_audio_sink_is_documented(shared_vad: object) -> None:
    probe = await run_say_no_sink_probe(shared_vad)  # type: ignore[arg-type]
    assert probe.informational is True
    assert probe.detail  # whatever the SDK did, the outcome is recorded


async def test_user_state_changed_fires_roomless(shared_vad: object) -> None:
    checks = await run_user_state_checks(shared_vad)  # type: ignore[arg-type]
    assert not _failures(checks), _failures(checks)
    names = {c.name for c in checks}
    assert {"user-state-roomless-fires", "user-state-transition-sequence"} <= names
