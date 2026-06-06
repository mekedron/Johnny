"""Tests for the HTTP-backed transcript history loader.

The meet-worker container is SQLAlchemy-free, so the production loader
talks to the API over HTTP to fetch past transcripts on restart. These
tests pin the wire format (payload shape from /sessions/{id}) and the
loader's resilience to network / parsing failures.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from johnny.meet_worker.transcript_loader import (
    DEFAULT_HISTORY_LIMIT,
    HttpTranscriptHistoryLoader,
    _payload_to_transcripts,
)
from johnny.voice_pipeline.events import TranscriptFinalized
from johnny.voice_pipeline.transcript_history import BOT_SPEAKER_LABEL


def test_payload_to_transcripts_maps_session_detail_payload() -> None:
    payload: dict[str, Any] = {
        "transcripts": [
            {
                "id": 1,
                "bot_session_id": 42,
                "start_offset_ms": 0,
                "end_offset_ms": 1500,
                "speaker": "alice",
                "text": "hello team",
                "created_at": "2026-06-06T10:00:00Z",
            },
            {
                "id": 2,
                "bot_session_id": 42,
                "start_offset_ms": 2000,
                "end_offset_ms": 3500,
                "speaker": None,
                "text": "any updates",
                "created_at": "2026-06-06T10:00:05Z",
            },
        ],
    }

    out = _payload_to_transcripts(payload)

    assert out == [
        TranscriptFinalized(
            text="hello team", timestamp_ms=1500, speaker="alice"
        ),
        TranscriptFinalized(
            text="any updates", timestamp_ms=3500, speaker=None
        ),
    ]


def test_payload_to_transcripts_skips_invalid_rows() -> None:
    payload: dict[str, Any] = {
        "transcripts": [
            {"text": "valid", "end_offset_ms": 100, "speaker": "x"},
            {"end_offset_ms": 100},  # no text
            "not a dict",
            {"text": "", "end_offset_ms": 200},  # empty text
            {"text": "ok", "end_offset_ms": "bad"},  # bad timestamp
        ],
    }

    out = _payload_to_transcripts(payload)

    assert [t.text for t in out] == ["valid", "ok"]
    assert out[1].timestamp_ms == 0  # bad timestamp falls back to 0


def test_payload_to_transcripts_returns_empty_for_unexpected_payload() -> None:
    assert _payload_to_transcripts({"transcripts": "not a list"}) == []
    assert _payload_to_transcripts({}) == []
    assert _payload_to_transcripts("not a dict") == []


@pytest.mark.asyncio
async def test_load_returns_empty_when_bot_session_id_missing() -> None:
    loader = HttpTranscriptHistoryLoader(api_base_url="http://api:8000")
    out = await loader.load(session_id="42", bot_session_id=None)
    assert out == []


@pytest.mark.asyncio
async def test_load_fetches_and_parses_session_detail() -> None:
    """The loader GETs ``/sessions/{id}`` and converts the response."""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "transcripts": [
                        {
                            "text": "earlier line",
                            "end_offset_ms": 1000,
                            "speaker": "alice",
                        }
                    ]
                }
            ),
            headers={"Content-Type": "application/json"},
        )

    transport = httpx.MockTransport(_handler)
    loader = HttpTranscriptHistoryLoader(api_base_url="http://api:8000")
    # Inject a client built on the MockTransport.
    loader._client = httpx.AsyncClient(transport=transport, timeout=1.0)
    try:
        out = await loader.load(session_id="ignored", bot_session_id=42)
    finally:
        await loader.close()

    assert captured["method"] == "GET"
    assert captured["url"].endswith(
        f"/sessions/42?limit={DEFAULT_HISTORY_LIMIT}"
    )
    assert out == [
        TranscriptFinalized(
            text="earlier line", timestamp_ms=1000, speaker="alice"
        )
    ]


@pytest.mark.asyncio
async def test_load_swallows_network_errors_and_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 500 response logs and falls back to an empty list."""
    import logging

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(_handler)
    loader = HttpTranscriptHistoryLoader(api_base_url="http://api:8000")
    loader._client = httpx.AsyncClient(transport=transport, timeout=1.0)
    try:
        with caplog.at_level(logging.ERROR):
            out = await loader.load(session_id=None, bot_session_id=99)
    finally:
        await loader.close()

    assert out == []
    assert any(
        "transcript rehydration HTTP call failed" in rec.message
        for rec in caplog.records
    )


# --- Johnny-7qp: HTTP loader pulls in bot utterances --------------------


def test_payload_to_transcripts_merges_utterances_chronologically() -> None:
    """Bot utterances interleave with participant transcripts by ``created_at``."""
    payload: dict[str, Any] = {
        "transcripts": [
            {
                "text": "status?",
                "end_offset_ms": 1000,
                "speaker": "alice",
                "created_at": "2026-06-06T10:00:00Z",
            },
            {
                "text": "what did you just say?",
                "end_offset_ms": 4000,
                "speaker": "alice",
                "created_at": "2026-06-06T10:00:10Z",
            },
        ],
        "utterances": [
            {
                "output_text": "infra is being upgraded",
                "audio_duration_ms": 800,
                "created_at": "2026-06-06T10:00:05Z",
            }
        ],
    }

    out = _payload_to_transcripts(payload)

    texts = [t.text for t in out]
    speakers = [t.speaker for t in out]
    assert texts == [
        "status?",
        "infra is being upgraded",
        "what did you just say?",
    ]
    assert speakers == ["alice", BOT_SPEAKER_LABEL, "alice"]


def test_payload_to_transcripts_handles_missing_utterances_key() -> None:
    """Older payload shapes without ``utterances`` still produce transcripts."""
    payload: dict[str, Any] = {
        "transcripts": [
            {"text": "hi", "end_offset_ms": 100, "speaker": "alice"},
        ],
    }
    out = _payload_to_transcripts(payload)
    assert [t.text for t in out] == ["hi"]


def test_payload_to_transcripts_skips_blank_utterance_text() -> None:
    """Empty / whitespace ``output_text`` values are dropped — they carry no recall."""
    payload: dict[str, Any] = {
        "utterances": [
            {"output_text": "   ", "created_at": "2026-06-06T10:00:00Z"},
            {"output_text": "", "created_at": "2026-06-06T10:00:01Z"},
            {"output_text": "real reply", "created_at": "2026-06-06T10:00:02Z"},
        ],
    }
    out = _payload_to_transcripts(payload)
    assert [t.text for t in out] == ["real reply"]
    assert out[0].speaker == BOT_SPEAKER_LABEL


def test_payload_to_transcripts_handles_unparseable_created_at() -> None:
    """A bogus created_at value falls back to wire order, not a crash."""
    payload: dict[str, Any] = {
        "transcripts": [
            {"text": "first", "end_offset_ms": 1, "created_at": "not-a-date"},
        ],
        "utterances": [
            {"output_text": "second", "created_at": "also-bad"},
        ],
    }
    out = _payload_to_transcripts(payload)
    assert [t.text for t in out] == ["first", "second"]


def test_payload_to_transcripts_handles_utc_offset_form() -> None:
    """Non-Z ISO timestamps (``+00:00`` form) sort correctly."""
    payload: dict[str, Any] = {
        "transcripts": [
            {
                "text": "later",
                "end_offset_ms": 2000,
                "created_at": "2026-06-06T10:00:10+00:00",
            }
        ],
        "utterances": [
            {
                "output_text": "earlier bot",
                "created_at": "2026-06-06T10:00:00+00:00",
            }
        ],
    }
    out = _payload_to_transcripts(payload)
    assert [t.text for t in out] == ["earlier bot", "later"]


@pytest.mark.asyncio
async def test_load_includes_utterances_from_api_response() -> None:
    """Live HTTP loader pulls both transcripts and utterances from /sessions/{id}."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "transcripts": [
                        {
                            "text": "hi",
                            "end_offset_ms": 100,
                            "speaker": "alice",
                            "created_at": "2026-06-06T10:00:00Z",
                        }
                    ],
                    "utterances": [
                        {
                            "output_text": "hello alice",
                            "created_at": "2026-06-06T10:00:01Z",
                        }
                    ],
                }
            ),
            headers={"Content-Type": "application/json"},
        )

    transport = httpx.MockTransport(_handler)
    loader = HttpTranscriptHistoryLoader(api_base_url="http://api:8000")
    loader._client = httpx.AsyncClient(transport=transport, timeout=1.0)
    try:
        out = await loader.load(session_id=None, bot_session_id=42)
    finally:
        await loader.close()

    assert [t.text for t in out] == ["hi", "hello alice"]
    assert out[1].speaker == BOT_SPEAKER_LABEL


@pytest.mark.asyncio
async def test_load_strips_trailing_slash_from_base_url() -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            content=json.dumps({"transcripts": []}),
            headers={"Content-Type": "application/json"},
        )

    transport = httpx.MockTransport(_handler)
    loader = HttpTranscriptHistoryLoader(api_base_url="http://api:8000/")
    loader._client = httpx.AsyncClient(transport=transport, timeout=1.0)
    try:
        await loader.load(session_id=None, bot_session_id=1)
    finally:
        await loader.close()

    # No double slash from concatenating "/sessions" onto base_url
    assert "http://api:8000/sessions/1" in captured["url"]
