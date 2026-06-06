"""Behaviour tests for the scripted fake providers used by the harness."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.providers import ChatMessage
from johnny.e2e.interrupt.providers import (
    PacedTTS,
    ScriptedAnswerLLM,
    ScriptedSlowSTT,
    SwitchingRouterLLM,
)


async def _drain_pcm(audio: bytes) -> AsyncIterator[bytes]:
    yield audio


# --- ScriptedSlowSTT --------------------------------------------------------


async def test_scripted_stt_yields_transcripts_in_order() -> None:
    stt = ScriptedSlowSTT(transcripts=["one", "two"], sleep_s=0.0)

    events_one = [
        e async for e in stt.transcribe_stream(_drain_pcm(b"\x00\x00" * 320))
    ]
    events_two = [
        e async for e in stt.transcribe_stream(_drain_pcm(b"\x00\x00" * 320))
    ]
    assert [e.text for e in events_one] == ["one"]
    assert [e.text for e in events_two] == ["two"]
    assert events_one[0].is_final is True
    assert events_two[0].is_final is True
    assert stt.calls == 2


async def test_scripted_stt_yields_sentinel_when_exhausted() -> None:
    stt = ScriptedSlowSTT(transcripts=["only"], sleep_s=0.0)
    [
        e async for e in stt.transcribe_stream(_drain_pcm(b"\x00\x00" * 320))
    ]
    overflow = [
        e async for e in stt.transcribe_stream(_drain_pcm(b"\x00\x00" * 320))
    ]
    assert overflow[0].text == ScriptedSlowSTT.SENTINEL


# --- SwitchingRouterLLM -----------------------------------------------------


def _is_barge_in_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "should_interrupt": {"type": "boolean"},
            "category": {"type": "string"},
            "reason": {"type": "string"},
        },
    }


def _is_router_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "should_speak": {"type": "boolean"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
    }


async def test_switching_router_dispatches_by_response_format() -> None:
    router = SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": True, "confidence": 0.9, "reason": "yes"},
        ],
        barge_in_decisions=[
            {
                "should_interrupt": True,
                "category": "stop",
                "reason": "user said stop",
            }
        ],
    )

    router_response = await router.chat(
        [ChatMessage(role="user", content="hi")],
        response_format=_is_router_schema(),
    )
    barge_response = await router.chat(
        [ChatMessage(role="user", content="stop")],
        response_format=_is_barge_in_schema(),
    )
    assert router_response.structured_output["should_speak"] is True
    assert barge_response.structured_output["should_interrupt"] is True
    assert len(router.router_calls) == 1
    assert len(router.barge_in_calls) == 1


async def test_switching_router_defaults_barge_in_to_no_interrupt() -> None:
    router = SwitchingRouterLLM(
        router_decisions=[
            {"should_speak": True, "confidence": 0.9, "reason": "yes"},
        ],
    )
    response = await router.chat(
        [ChatMessage(role="user", content="hi")],
        response_format=_is_barge_in_schema(),
    )
    assert response.structured_output == {
        "should_interrupt": False,
        "category": "noise",
        "reason": "default no-interrupt verdict",
    }


# --- ScriptedAnswerLLM ------------------------------------------------------


async def test_scripted_answer_llm_streams_deltas_per_word() -> None:
    llm = ScriptedAnswerLLM(answers=["hello there friend"], delta_sleep_s=0.0)
    deltas: list[str] = []
    async for delta in llm.stream_chat([ChatMessage(role="user", content="x")]):
        deltas.append(delta)
    assert deltas == ["hello", " there", " friend"]


async def test_scripted_answer_llm_chat_returns_full_text() -> None:
    llm = ScriptedAnswerLLM(answers=["a b"], delta_sleep_s=0.0)
    response = await llm.chat([ChatMessage(role="user", content="x")])
    assert response.text == "a b"


# --- PacedTTS ---------------------------------------------------------------


async def test_paced_tts_yields_frame_count_frames() -> None:
    tts = PacedTTS(frame_count=5, frame_period_s=0.0)
    frames = [f async for f in tts.synthesize_stream("hello")]
    assert len(frames) == 5
    assert all(len(f) == 320 for f in frames)


def test_paced_tts_total_duration_is_frame_count_times_period() -> None:
    tts = PacedTTS(frame_count=100, frame_period_s=0.02)
    # 100 * 0.02 = 2.0
    assert tts.total_duration_s == pytest.approx(2.0, abs=1e-9)
