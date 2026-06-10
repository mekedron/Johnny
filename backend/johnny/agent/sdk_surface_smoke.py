"""In-image ``livekit-agents==1.5.17`` session-surface smoke (Johnny-trt.2).

Phases 3-5 of the fast-core epic (Johnny-trt) rest on two SDK behaviors that
were documented upstream but unverified on our pinned ``livekit-agents==1.5.17``
inside the api image. This smoke proves both empirically, roomless, with stub
providers — the same no-room/no-creds/no-network posture as
:mod:`johnny.agent.console_smoke` (its sibling):

1. **``AgentSession.say()`` lifecycle** (Phase 3's delegated-turn ack terminal):
   ``say()`` returns a :class:`~livekit.agents.voice.SpeechHandle`; its
   done-callback fires after the audio finishes playing out; interrupting the
   speech mid-playout still fires the done-callback and surfaces
   ``handle.interrupted == True``. INV-1 hangs on this: a delegated turn's
   terminal is its ack utterance, so the ack's ``SpeechHandle`` MUST reach a
   terminal done-callback in every outcome (played out / barged-in).

2. **``user_state_changed`` on a roomless session** (Phase 5's speech-queue
   delivery gating): with ``session.input.audio`` set to a synthetic source (no
   LiveKit room, no ``RoomIO``), real Silero VAD speech onsets/offsets drive
   ``user_state_changed`` events (``listening`` → ``speaking`` → ``listening``)
   and the ``user_away_timeout`` timer drives ``listening`` → ``away``. The
   speech queue delivers pending utterances only at turn boundaries / silence,
   so Phase 5 needs these events to exist and fire roomless.

Audio I/O mirrors the playground's roomless seams
(:mod:`johnny.agent.browser_audio_io`): a queue-fed
:class:`~livekit.agents.voice.io.AudioInput` stands in for the browser mic and
a blind-sink :class:`~livekit.agents.voice.io.AudioOutput` (estimated playout,
``BrowserAudioOutput``'s contract minus the transport) stands in for browser
playback. The reply ``SpeechHandle`` completes only after the sink reports
``on_playback_finished`` — exactly the seam Phase 3's ack terminal rides.

Why the speech fixture is a real recording (``fixtures/sdk_smoke_speech.pcm``):
Silero VAD is a trained speech classifier and DSP-synthetic audio does NOT
trigger it — verified in-image before writing this smoke (white noise and
formant-shaped harmonic "vowels" with syllable-rate AM: zero events; the real
sample: one clean START/END pair). See ``fixtures/README.md`` for provenance.
``tests/`` is excluded from the prod image, so the smoke carries its own copy.

Why ``session._opts.user_away_timeout`` is mutated pre-start: the away timer is
an ``AgentSession`` constructor knob (``user_away_timeout``, default 15.0 s)
that :func:`~johnny.agent.session.build_agent_session` does not expose, and 15 s
per away-transition would triple the smoke's runtime. The private mutation is
read at timer-arm time (verified 1.5.17: ``_set_user_away_timer`` reads
``self._opts.user_away_timeout`` on every arm), so mutating before ``start()``
is safe here. If Phase 5 needs a non-default timeout in production it must add
the constructor passthrough instead.

Run it (CI-friendly, exits 0 when every non-informational check passes)::

    docker compose exec api python -m johnny.agent.sdk_surface_smoke

Requires the ``agent`` extra (``livekit-agents``); like its siblings it is
imported only where that extra is installed, never from the import-safe
top-level :mod:`johnny.agent` package.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from livekit import rtc
from livekit.agents.llm import ChatMessage as LKChatMessage
from livekit.agents.voice import SpeechHandle
from livekit.agents.voice.io import AudioInput, AudioOutput, AudioOutputCapabilities

from app.providers.base import PCM_SAMPLE_RATE_HZ, TTSProvider
from johnny.agent.adapters.johnny_llm import JohnnyLLM
from johnny.agent.adapters.johnny_stt import JohnnySTT
from johnny.agent.adapters.johnny_tts import JohnnyTTS
from johnny.agent.console_smoke import _ConsoleStubLLMProvider, _ConsoleStubSTTProvider
from johnny.agent.session import JohnnyAgent, build_agent_session, load_vad

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from livekit.agents import AgentSession
    from livekit.agents.vad import VAD
    from livekit.agents.voice.events import UserStateChangedEvent

logger = logging.getLogger(__name__)

# Real-speech sample for the Silero VAD (see module docstring + fixtures/README.md).
_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "sdk_smoke_speech.pcm"

# Stub-TTS playout length for the say() checks. Long enough that the interrupt
# check has a comfortable mid-playout window, short enough to keep the smoke fast.
SAY_TTS_DURATION_S = 1.6

# How long after playback starts the interrupt check barges in.
_INTERRUPT_AFTER_S = 0.3

# Away timer for the user-state checks (production default is 15.0 s — see
# module docstring for why the smoke shortens it via the private _opts seam).
AWAY_TIMEOUT_S = 2.0

# The user-state audio timeline (real-time paced, 20 ms frames): silence until
# the away timer fires, then the ~2 s speech fixture, then silence long enough
# for VAD end-of-speech (~0.55 s default min-silence) plus a re-armed away.
_PRE_SPEECH_SILENCE_S = 3.5
_POST_SPEECH_SILENCE_S = 4.5

_FRAME_S = 0.02
_FRAME_SAMPLES = int(PCM_SAMPLE_RATE_HZ * _FRAME_S)
_SILENCE_FRAME = b"\x00\x00" * _FRAME_SAMPLES

SAY_TEXT = "Let me check on that and get back to you."
_SMOKE_INSTRUCTIONS = "You are Johnny in an SDK-surface smoke test. Reply briefly."


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One named smoke check: ``ok`` drives the exit code unless informational.

    ``informational`` marks probes whose *outcome* is the deliverable (recorded
    into docs/PIPELINE.md) rather than a pass/fail contract — e.g. the no-sink
    ``say()`` probe documents whatever the SDK does without failing the smoke.
    """

    name: str
    ok: bool
    detail: str
    informational: bool = False


class _SmokeTTSProvider(TTSProvider):
    """TTS stub emitting ``duration_s`` of silence in 50 ms chunks.

    Real-time playout length is what the say() checks measure (the blind sink
    estimates playout from pushed duration), so the stub controls it precisely;
    the text is ignored.
    """

    def __init__(self, duration_s: float = SAY_TTS_DURATION_S) -> None:
        self._duration_s = duration_s

    @property
    def name(self) -> str:
        return "sdk-smoke-tts"

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncIterator[bytes]:
        chunk = b"\x00\x00" * (PCM_SAMPLE_RATE_HZ // 20)  # 50 ms
        for _ in range(max(1, round(self._duration_s * 20))):
            yield chunk


class _QueuePCMAudioInput(AudioInput):
    """Synthetic roomless mic: queued 16 kHz mono S16LE chunks → ``rtc.AudioFrame``.

    The smoke's stand-in for :class:`~johnny.agent.browser_audio_io.BrowserAudioInput`
    — same seam (``session.input.audio``), but fed from an in-process queue so
    the user-state timeline is scripted. ``end()`` raises ``StopAsyncIteration``
    on the consumer, the roomless analogue of a track unpublishing.
    """

    def __init__(self) -> None:
        super().__init__(label="SdkSmokeAudioInput")
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

    def push(self, pcm: bytes) -> None:
        self._chunks.put_nowait(pcm)

    def end(self) -> None:
        self._chunks.put_nowait(None)

    async def __anext__(self) -> rtc.AudioFrame:
        while True:
            data = await self._chunks.get()
            if data is None:
                raise StopAsyncIteration
            samples = len(data) // 2
            if samples <= 0:
                continue
            return rtc.AudioFrame(
                data=data,
                sample_rate=PCM_SAMPLE_RATE_HZ,
                num_channels=1,
                samples_per_channel=samples,
            )


@dataclass(frozen=True, slots=True)
class _SinkEvent:
    """One observed playback edge on the blind sink (monotonic-stamped)."""

    kind: str  # "playback_started" | "playback_finished"
    t: float
    position: float = 0.0
    interrupted: bool = False


class _BlindSinkAudioOutput(AudioOutput):
    """Blind-sink audio output: discard frames, estimate playout in real time.

    :class:`~johnny.agent.browser_audio_io.BrowserAudioOutput`'s contract minus
    the transport: ``flush()`` starts a timer for the captured audio's real-time
    duration, then fires ``on_playback_finished``; ``clear_buffer()`` (the SDK's
    barge-in path) cuts the timer short and reports ``interrupted=True``. The
    reply ``SpeechHandle`` completes only after that callback — the exact seam
    the ack terminal rides in the playground/Meet paths. Records every edge in
    ``events`` so checks can assert ordering against handle callbacks.
    """

    def __init__(self) -> None:
        super().__init__(
            label="SdkSmokeAudioOutput",
            next_in_chain=None,
            sample_rate=PCM_SAMPLE_RATE_HZ,
            capabilities=AudioOutputCapabilities(pause=False),
        )
        self.events: list[_SinkEvent] = []
        self._segment_active = False
        self._segment_started_at = 0.0
        self._pushed_duration = 0.0
        self._interrupted_event = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        if not self._segment_active:
            self._segment_active = True
            self._segment_started_at = time.monotonic()
            self.events.append(_SinkEvent(kind="playback_started", t=time.monotonic()))
            self.on_playback_started(created_at=time.time())
        self._pushed_duration += frame.duration

    def flush(self) -> None:
        super().flush()
        if not self._segment_active:
            return
        self._segment_active = False
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = asyncio.create_task(self._wait_for_playout())

    def clear_buffer(self) -> None:
        if self._pushed_duration:
            self._interrupted_event.set()

    async def aclose(self) -> None:
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _wait_for_playout(self) -> None:
        elapsed = time.monotonic() - self._segment_started_at
        remaining = max(0.0, self._pushed_duration - elapsed)
        interrupt_task = asyncio.create_task(self._interrupted_event.wait())
        sleep_task = asyncio.create_task(asyncio.sleep(remaining))
        try:
            await asyncio.wait(
                {sleep_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (sleep_task, interrupt_task):
                if not t.done():
                    t.cancel()
        interrupted = self._interrupted_event.is_set()
        if interrupted:
            played = time.monotonic() - self._segment_started_at
            position = min(played, self._pushed_duration)
        else:
            position = self._pushed_duration
        self._pushed_duration = 0.0
        self._interrupted_event.clear()
        self.events.append(
            _SinkEvent(
                kind="playback_finished",
                t=time.monotonic(),
                position=position,
                interrupted=interrupted,
            )
        )
        self.on_playback_finished(playback_position=position, interrupted=interrupted)


def _build_smoke_session(
    vad: VAD, *, tts_duration_s: float = SAY_TTS_DURATION_S
) -> AgentSession[Any]:
    """Roomless stub session via the real :func:`build_agent_session` harness.

    Same shape as :func:`johnny.agent.console_smoke.build_console_session` (stub
    STT that never transcribes + stub LLM, ``turn_detection="vad"`` — no job
    context), but with the duration-controlled TTS stub the say() checks need.
    """
    return build_agent_session(
        stt=JohnnySTT(_ConsoleStubSTTProvider()),
        llm=JohnnyLLM(_ConsoleStubLLMProvider()),
        tts=JohnnyTTS(_SmokeTTSProvider(duration_s=tts_duration_s)),
        vad=vad,
        turn_detection="vad",
    )


async def _settle() -> None:
    """Yield long enough for loop-scheduled done-callbacks to run."""
    await asyncio.sleep(0.05)


async def run_say_checks(vad: VAD) -> list[CheckResult]:
    """Prove the ``say()`` → ``SpeechHandle`` lifecycle on a roomless session."""
    checks: list[CheckResult] = []
    sink = _BlindSinkAudioOutput()
    session = _build_smoke_session(vad)
    session.output.audio = sink

    # say() before start(): the SDK's documented guard.
    try:
        session.say("too early")
        checks.append(CheckResult("say-before-start-raises", False, "no exception raised"))
    except RuntimeError as exc:
        checks.append(CheckResult("say-before-start-raises", True, f"RuntimeError: {exc}"))

    await session.start(agent=JohnnyAgent(instructions=_SMOKE_INSTRUCTIONS))
    try:
        # --- completed playout -------------------------------------------------
        done_seen: list[tuple[float, bool, bool]] = []  # (t, done(), interrupted)
        t0 = time.monotonic()
        handle = session.say(SAY_TEXT)
        checks.append(
            CheckResult(
                "say-returns-speech-handle",
                isinstance(handle, SpeechHandle) and not handle.done(),
                f"type={type(handle).__name__} id={handle.id} done_at_return={handle.done()}",
            )
        )
        handle.add_done_callback(
            lambda h: done_seen.append((time.monotonic(), h.done(), h.interrupted))
        )
        await handle
        elapsed = time.monotonic() - t0
        await _settle()

        finished = [e for e in sink.events if e.kind == "playback_finished"]
        cb_after_playout = (
            len(done_seen) == 1
            and done_seen[0][1]
            and not done_seen[0][2]
            and len(finished) == 1
            and not finished[0].interrupted
            and finished[0].t <= done_seen[0][0]
        )
        checks.append(
            CheckResult(
                "say-done-callback-after-playout",
                cb_after_playout,
                (
                    f"done_callbacks={len(done_seen)} interrupted={handle.interrupted} "
                    f"sink_finished={[(round(e.position, 2), e.interrupted) for e in finished]}"
                ),
            )
        )
        checks.append(
            CheckResult(
                "say-done-elapsed-realtime",
                SAY_TTS_DURATION_S * 0.8 <= elapsed <= SAY_TTS_DURATION_S + 2.0,
                f"elapsed={elapsed:.2f}s for {SAY_TTS_DURATION_S}s of stub audio",
            )
        )
        say_texts = [
            item.text_content
            for item in handle.chat_items
            if isinstance(item, LKChatMessage) and item.role == "assistant"
        ]
        checks.append(
            CheckResult(
                "say-chat-items-text",
                SAY_TEXT in [t for t in say_texts if t],
                f"chat_items assistant texts={say_texts!r}",
            )
        )

        # add_done_callback on an already-done handle still fires (call_soon).
        late_seen: list[bool] = []
        handle.add_done_callback(lambda h: late_seen.append(h.done()))
        await _settle()
        checks.append(
            CheckResult(
                "say-late-done-callback-fires",
                late_seen == [True],
                f"late callback invocations={late_seen!r}",
            )
        )

        # --- interrupted mid-playout ------------------------------------------
        sink_mark = len(sink.events)
        done_seen2: list[tuple[float, bool, bool]] = []
        h2 = session.say("This longer ack is going to be barged into mid-playout.")
        h2.add_done_callback(
            lambda h: done_seen2.append((time.monotonic(), h.done(), h.interrupted))
        )
        started = await _wait_until(
            lambda: any(e.kind == "playback_started" for e in sink.events[sink_mark:]),
            timeout_s=5.0,
        )
        await asyncio.sleep(_INTERRUPT_AFTER_S)
        t_int = time.monotonic()
        same_handle = h2.interrupt() is h2
        await h2
        int_elapsed = time.monotonic() - t_int
        await _settle()
        finished2 = [e for e in sink.events[sink_mark:] if e.kind == "playback_finished"]
        checks.append(
            CheckResult(
                "say-interrupt-surfaces-interrupted",
                (
                    started
                    and same_handle
                    and h2.interrupted
                    and len(done_seen2) == 1
                    and done_seen2[0][2]
                    and int_elapsed < 1.0
                    and len(finished2) == 1
                    and finished2[0].interrupted
                ),
                (
                    f"interrupted={h2.interrupted} done_callbacks={len(done_seen2)} "
                    f"resolved_in={int_elapsed:.2f}s "
                    f"sink_finished={[(round(e.position, 2), e.interrupted) for e in finished2]}"
                ),
            )
        )

        # --- allow_interruptions=False guard -----------------------------------
        h3 = session.say("Uninterruptible ack.", allow_interruptions=False)
        try:
            h3.interrupt()
            guard = False
            guard_detail = "interrupt() did not raise"
        except RuntimeError as exc:
            guard = True
            guard_detail = f"RuntimeError: {exc}"
        h3.interrupt(force=True)
        await h3
        await _settle()
        checks.append(
            CheckResult(
                "say-allow-interruptions-false-guard",
                guard and h3.done(),
                f"{guard_detail}; force-interrupt completes handle "
                f"(done={h3.done()} interrupted={h3.interrupted})",
            )
        )
    finally:
        await session.aclose()
        await sink.aclose()
    return checks


async def run_say_no_sink_probe(vad: VAD) -> CheckResult:
    """Document what ``say()`` does with NO audio sink attached (informational).

    Phase 3's ack rides sessions that always have an audio output (playground /
    Meet bridge), but the failure mode matters: if the sink is ever detached,
    does the ack handle complete (terminal fires) or hang (terminal lost)?
    """
    session = _build_smoke_session(vad)
    await session.start(agent=JohnnyAgent(instructions=_SMOKE_INSTRUCTIONS))
    try:
        handle = session.say(SAY_TEXT)
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=5.0)
            return CheckResult(
                "say-no-audio-sink",
                True,
                f"completes without an audio sink (interrupted={handle.interrupted})",
                informational=True,
            )
        except TimeoutError:
            handle.interrupt(force=True)
            return CheckResult(
                "say-no-audio-sink",
                True,
                "does NOT complete within 5s without an audio sink — an ack terminal "
                "would hang if output.audio were ever detached",
                informational=True,
            )
    finally:
        await session.aclose()


async def _wait_until(predicate: Any, *, timeout_s: float, poll_s: float = 0.02) -> bool:
    """Poll ``predicate()`` until truthy or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll_s)
    return bool(predicate())


async def _push_timeline(source: _QueuePCMAudioInput, fixture: bytes) -> None:
    """Real-time-paced synthetic mic: silence → speech fixture → silence.

    20 ms frames against a drift-free deadline (the away timer and the blind
    sink are wall-clock, so the input must flow at real speed for the observed
    timings to mean anything).
    """
    loop = asyncio.get_running_loop()
    next_at = loop.time()

    async def pace(frames: Iterable[bytes]) -> None:
        nonlocal next_at
        for frame in frames:
            source.push(frame)
            next_at += _FRAME_S
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    def fixture_frames(data: bytes) -> Iterable[bytes]:
        step = _FRAME_SAMPLES * 2
        for i in range(0, len(data), step):
            yield data[i : i + step]

    await pace(itertools.repeat(_SILENCE_FRAME, round(_PRE_SPEECH_SILENCE_S / _FRAME_S)))
    await pace(fixture_frames(fixture))
    await pace(itertools.repeat(_SILENCE_FRAME, round(_POST_SPEECH_SILENCE_S / _FRAME_S)))


async def run_user_state_checks(vad: VAD) -> list[CheckResult]:
    """Prove ``user_state_changed`` fires on a roomless session from synthetic audio.

    Timeline (real time): silence long enough for the away timer (shortened to
    :data:`AWAY_TIMEOUT_S`) → the real-speech fixture (Silero start-of-speech) →
    silence (Silero end-of-speech, then a re-armed away). Expected transitions::

        listening → away        (away timer, armed at session start)
        away → speaking         (VAD start-of-speech)
        speaking → listening    (VAD end-of-speech)
        listening → away        (away timer re-armed on the listening edge)
    """
    checks: list[CheckResult] = []
    fixture = _FIXTURE_PATH.read_bytes()
    source = _QueuePCMAudioInput()
    sink = _BlindSinkAudioOutput()
    session = _build_smoke_session(vad)
    # Shorten the away timer via the private seam (see module docstring).
    session._opts.user_away_timeout = AWAY_TIMEOUT_S

    seen: list[tuple[str, str, float]] = []  # (old_state, new_state, offset_s)
    t_start = time.monotonic()

    def _on_state(ev: UserStateChangedEvent) -> None:
        seen.append((str(ev.old_state), str(ev.new_state), time.monotonic() - t_start))

    session.on("user_state_changed", _on_state)
    session.input.audio = source
    session.output.audio = sink
    await session.start(agent=JohnnyAgent(instructions=_SMOKE_INSTRUCTIONS))
    t_start = time.monotonic()
    try:
        await _push_timeline(source, fixture)
        await asyncio.sleep(0.5)  # let trailing VAD/away events land
    finally:
        source.end()
        await session.aclose()
        await sink.aclose()

    pairs = [(old, new) for old, new, _ in seen]
    timeline = ", ".join(f"{old}→{new}@{off:.2f}s" for old, new, off in seen)
    expected = [
        ("listening", "away"),
        ("away", "speaking"),
        ("speaking", "listening"),
        ("listening", "away"),
    ]
    checks.append(
        CheckResult(
            "user-state-roomless-fires",
            bool(seen),
            f"events={len(seen)} [{timeline}]",
        )
    )
    checks.append(
        CheckResult(
            "user-state-transition-sequence",
            pairs == expected,
            f"got {pairs!r}, expected {expected!r}",
        )
    )
    if pairs == expected:
        away1, speaking, listening, away2 = (off for _, _, off in seen)
        speech_start = _PRE_SPEECH_SILENCE_S
        speech_end = _PRE_SPEECH_SILENCE_S + len(fixture) / 2 / PCM_SAMPLE_RATE_HZ
        away2_lo = listening + AWAY_TIMEOUT_S - 0.5
        away2_hi = listening + AWAY_TIMEOUT_S + 2.0
        checks.append(
            CheckResult(
                "user-state-timing-sanity",
                (
                    AWAY_TIMEOUT_S - 0.5 <= away1 <= AWAY_TIMEOUT_S + 2.0
                    and speech_start - 0.2 <= speaking <= speech_end
                    and speech_end <= listening <= speech_end + 2.0
                    and away2_lo <= away2 <= away2_hi
                ),
                (
                    f"away1={away1:.2f}s speaking={speaking:.2f}s listening={listening:.2f}s "
                    f"away2={away2:.2f}s (speech window {speech_start:.2f}–{speech_end:.2f}s, "
                    f"away_timeout={AWAY_TIMEOUT_S}s)"
                ),
            )
        )
    return checks


async def run_all() -> list[CheckResult]:
    """Run every check group against one shared Silero VAD load."""
    vad = load_vad()
    checks = list(await run_say_checks(vad))
    checks.append(await run_say_no_sink_probe(vad))
    checks.extend(await run_user_state_checks(vad))
    return checks


def main() -> None:
    """CLI entry: ``python -m johnny.agent.sdk_surface_smoke``."""
    logging.basicConfig(level=logging.INFO)
    try:
        checks = asyncio.run(run_all())
    except Exception:
        logger.exception("sdk surface smoke FAILED (exception)")
        sys.exit(1)
    failed = [c for c in checks if not c.ok and not c.informational]
    for c in checks:
        status = "PASS" if c.ok else ("INFO" if c.informational else "FAIL")
        logger.info("%-4s %-38s %s", status, c.name, c.detail)
    if failed:
        logger.error("sdk surface smoke FAILED: %d check(s)", len(failed))
        sys.exit(1)
    logger.info("sdk surface smoke PASSED (%d checks)", len(checks))
    sys.exit(0)


__all__ = [
    "AWAY_TIMEOUT_S",
    "SAY_TEXT",
    "SAY_TTS_DURATION_S",
    "CheckResult",
    "main",
    "run_all",
    "run_say_checks",
    "run_say_no_sink_probe",
    "run_user_state_checks",
]


if __name__ == "__main__":
    main()
