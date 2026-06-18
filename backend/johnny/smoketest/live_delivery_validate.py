"""Live real-engine delivery/barge validation (Johnny-d6w.33).

Builds a **real** :class:`~johnny.agent.browser_session.BrowserAgentSession`
(task wiring on, so the real :class:`TaskSpeechDeliverer` + the session's real
``say()``/TTS/audio + ``interrupt()`` are all live), enqueues a synthetic task
result, lets the deliverer speak it as real (paced) audio, and times
``session.interrupt()`` onto the playout — a precise barge the chrome-devtools
button could never land mid-result.

It asserts the Johnny-d6w.33 contract end-to-end through the REAL
SpeechHandle/interrupt/audio path (the deliverer unit test in
``tests/agent/test_task_wiring.py`` covers the same logic with a fake handle):

* barge AFTER :data:`~johnny.agent.task_wiring.TASK_RESULT_HEARD_AFTER_S` of
  playout  → the result is marked **delivered** (heard = delivered), never
  re-queued, so it can't re-surface on a later, unrelated turn (the session-49
  "stale weather before the dashboards list" bleed);
* barge BEFORE the threshold → the result **re-queues** for re-delivery (the
  trt.28 "a barge-in never loses a result" default still holds).

No gate / LLM / worker / Redis is exercised — only the delivery + barge path —
so it is deterministic given the stack's Postgres. Run inside the api container
against the ``./run-dev.sh`` stack::

    docker compose exec api python -m johnny.smoketest.live_delivery_validate

Exits non-zero if any case fails. Creates (and deletes) one throwaway
``bot_sessions`` row per case under id 970_5xx; writes nothing to git.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.providers.base import ProviderConfig, ProviderKind, TTSProvider, get_registry
from johnny.agent.ensemble_scenario import PCM_SAMPLE_RATE_HZ, RecordingBus
from johnny.agent.job_config import AUTONOMOUS_MODE, SessionJobConfig
from johnny.agent.latency_harness import register_stub_providers, stub_provider_config
from johnny.agent.task_wiring import TASK_RESULT_HEARD_AFTER_S
from johnny.agent.tasks import TaskSpec
from johnny.voice_pipeline.browser_transport import BrowserAudioTransport

logger = logging.getLogger(__name__)

LONG_TTS_PROVIDER_NAME = "d6w33-long-tts"
_RESULT_TEXT = "Right now in Helsinki: clear, plus thirteen degrees, light wind."
_RESULT_KIND = "weather"
# Stream long enough that a barge can land both before AND after the heard
# threshold with comfortable margin.
_RESULT_AUDIO_S = max(6.0, TASK_RESULT_HEARD_AFTER_S * 2.0)


class _LongSilenceTTSProvider(TTSProvider):
    """Stub TTS that streams ``_RESULT_AUDIO_S`` of paced PCM for any text.

    Content is irrelevant here (no VAD reads the bot's own out-bound result
    playout — the barge is the programmatic ``session.interrupt()``); only the
    DURATION matters, so a long run of paced silence frames gives a wide,
    deterministic window to time the barge against the playout clock.
    """

    def __init__(self, config: ProviderConfig | None = None) -> None:
        del config

    @property
    def name(self) -> str:
        return LONG_TTS_PROVIDER_NAME

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        del text, voice_id
        chunk = bytes(int(PCM_SAMPLE_RATE_HZ * 0.1) * 2)  # 100 ms of S16LE silence
        for _ in range(int(_RESULT_AUDIO_S / 0.1)):
            yield chunk


def _register_long_tts() -> None:
    get_registry().register(
        ProviderKind.TTS, LONG_TTS_PROVIDER_NAME, _LongSilenceTTSProvider, replace=True
    )


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str


async def _wait_until(predicate, *, timeout_s: float, poll_s: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll_s)
    return predicate()


def _create_bot_session_row(bot_session_id: int) -> None:
    """Insert the throwaway browser ``bot_sessions`` row the coordinator FKs to."""
    from app.db.models import BotSession, BotSessionSource, BotSessionStatus
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        if db.get(BotSession, bot_session_id) is None:
            db.add(
                BotSession(
                    id=bot_session_id,
                    source=BotSessionSource.BROWSER,
                    status=BotSessionStatus.JOINED,
                )
            )
            db.commit()


def _delete_session_rows(bot_session_id: int) -> None:
    from app.db.models import AgentTask, AgentWorkstream, BotSession
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        for model in (AgentWorkstream, AgentTask):
            for row in db.query(model).filter_by(bot_session_id=bot_session_id).all():
                db.delete(row)
        row = db.get(BotSession, bot_session_id)
        if row is not None:
            db.delete(row)
        db.commit()


async def _run_case(*, heard: bool, bot_session_id: int) -> CaseResult:
    """Deliver one synthetic result, barge it, and check the d6w.33 contract."""
    from johnny.agent.browser_session import BrowserAgentSession, load_browser_vad

    name = "barge-after-threshold (heard=delivered)" if heard else (
        "barge-before-threshold (re-queued)"
    )
    register_stub_providers()
    _register_long_tts()

    provider_config = stub_provider_config()
    provider_config["tts"] = {
        "provider_name": LONG_TTS_PROVIDER_NAME,
        "display_name": "d6w.33 long silence TTS",
        "credentials": {},
        "options": {},
    }

    _create_bot_session_row(bot_session_id)
    config = SessionJobConfig(
        bot_session_id=bot_session_id,
        room_name=f"d6w33-{bot_session_id}",
        agent_snapshot={"name": "Johnny", "mode": AUTONOMOUS_MODE},
        provider_config=provider_config,
    )
    bus = RecordingBus()
    transport = BrowserAudioTransport()
    await transport.start()
    session = await BrowserAgentSession.build(
        transport, config, event_bus=bus, vad=load_browser_vad(), task_wiring=True
    )
    wiring = None
    try:
        await session.start()
        runtime = session._runtime  # noqa: SLF001 — harness reaches the live seams
        coordinator = runtime.task_coordinator
        wiring = runtime.task_speech
        if coordinator is None or wiring is None:
            return CaseResult(name, False, "task wiring not assembled (need task_wiring=True)")
        deliverer = wiring.deliverer
        queue = deliverer._queue  # noqa: SLF001

        queued = await coordinator.begin(TaskSpec(kind=_RESULT_KIND, turn_id=1))
        if queued is None:
            return CaseResult(name, False, "coordinator.begin returned None")
        task_id = queued.task_id
        entry = coordinator.note_task_settled(
            task_id, status="done", result_text=_RESULT_TEXT, turn_id=1
        )
        if entry is None:
            return CaseResult(name, False, "note_task_settled returned None")
        item = deliverer.enqueue_result(entry)
        if item is None:
            return CaseResult(name, False, "enqueue_result returned None (blank result?)")

        # Wait until the deliverer actually started speaking the result.
        started = await _wait_until(
            lambda: queue.in_flight is item, timeout_s=15.0
        )
        if not started:
            return CaseResult(name, False, "result never reached the deliverer in-flight")

        # Let it play past / short-of the heard threshold, then barge.
        await asyncio.sleep(TASK_RESULT_HEARD_AFTER_S + 1.0 if heard else 0.2)
        session.interrupt()

        # The deliverer settles the item after the cut: delivered (heard) or
        # re-queued (short cut). Wait for whichever terminal we expect.
        from johnny.agent.speech_queue import ItemState

        if heard:
            settled = await _wait_until(
                lambda: item.state is ItemState.SPOKEN, timeout_s=10.0
            )
        else:
            settled = await _wait_until(
                lambda: item.state is ItemState.QUEUED, timeout_s=10.0
            )
        registry = coordinator.registry_entry(task_id)
        delivered = registry is not None and registry.delivered

        if heard:
            ok = settled and delivered
            detail = (
                f"item.state={item.state.name} delivered={delivered} "
                f"(expected SPOKEN + delivered=True)"
            )
        else:
            ok = settled and not delivered
            detail = (
                f"item.state={item.state.name} delivered={delivered} "
                f"(expected QUEUED + delivered=False — re-delivers next boundary)"
            )
        return CaseResult(name, ok, detail)
    finally:
        # Stop the deliverer loop first so a re-queued (short-cut) item does not
        # try to re-deliver through a closing session (benign, but noisy).
        if wiring is not None:
            try:
                await wiring.aclose()
            except Exception:
                logger.debug("harness: wiring aclose failed", exc_info=True)
        try:
            await session.aclose()
        finally:
            await transport.stop()
            transport.close_playback()
            _delete_session_rows(bot_session_id)


async def _amain() -> int:
    parser = argparse.ArgumentParser(description="Live d6w.33 delivery/barge validation")
    parser.add_argument("--base-session-id", type=int, default=970_500)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    results = [
        await _run_case(heard=True, bot_session_id=args.base_session_id),
        await _run_case(heard=False, bot_session_id=args.base_session_id + 1),
    ]
    print("\n=== Johnny-d6w.33 live delivery/barge validation ===")
    for r in results:
        print(f"  [{'PASS' if r.ok else 'FAIL'}] {r.name}\n        {r.detail}")
    ok = all(r.ok for r in results)
    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'}\n")
    return 0 if ok else 1


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
