"""Unit tests for the MCP result voicer (Johnny-d6w.30).

``_LlmVoicer`` resolves the active LLM provider and turns a structured tool
payload into ear-ready prose. These tests monkeypatch the provider loader +
crypto so they exercise the voicer's contract — prompt shape, lifecycle
(session + provider closed), and the "never raise, return None on any failure"
guarantee — without a DB or a real model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.providers.base import LLMProvider, LLMResponse, ProviderKind
from app.services.task_worker import build_llm_voicer


class _FakeDB:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _factory(db: _FakeDB) -> Callable[[], Any]:
    """A session factory whose 'session' is a ``_FakeDB`` — the provider loader
    is monkeypatched, so the session is only opened and closed, never queried."""
    return lambda: db


class _RecordingLLM(LLMProvider):
    def __init__(self, text: str) -> None:
        self._text = text
        self.messages: list[Any] | None = None
        self.closed = False

    @property
    def name(self) -> str:
        return "recording"

    async def chat(
        self,
        messages: Any,
        tools: Any = None,
        response_format: Any = None,
    ) -> LLMResponse:
        self.messages = list(messages)
        return LLMResponse(text=self._text, finish_reason="stop")

    async def close(self) -> None:
        self.closed = True


def _patch_providers(
    monkeypatch: pytest.MonkeyPatch, active: dict[ProviderKind, Any]
) -> None:
    def fake_load(session: Any, **kwargs: Any) -> dict[ProviderKind, Any]:
        return active

    monkeypatch.setattr("app.providers.loader.load_active_providers", fake_load)
    monkeypatch.setattr("app.security.crypto.get_crypto", lambda: object())


async def test_voicer_builds_prompt_and_returns_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _RecordingLLM("It is 21 degrees in Paris.")
    fake_db = _FakeDB()
    _patch_providers(monkeypatch, {ProviderKind.LLM: llm})

    voicer = build_llm_voicer(_factory(fake_db), timeout_s=5.0)
    out = await voicer.voice(
        '{"temp_c": 21, "city": "Paris"}',
        tool="get_weather",
        server="weather",
        arguments={"city": "Paris"},
    )

    assert out == "It is 21 degrees in Paris."
    # Lifecycle: both the DB session and the provider are released.
    assert fake_db.closed is True
    assert llm.closed is True
    # The system prompt carries the never-read-JSON / never-invent contract.
    assert llm.messages is not None
    system, user = llm.messages[0], llm.messages[1]
    assert system.role == "system"
    assert "Never read JSON" in (system.content or "")
    assert "never invent" in (system.content or "")
    # The raw JSON is handed to the model in the user message.
    assert user.role == "user"
    assert '"temp_c": 21' in (user.content or "")


async def test_voicer_no_active_llm_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _FakeDB()
    _patch_providers(monkeypatch, {})  # no LLM row active

    voicer = build_llm_voicer(_factory(fake_db), timeout_s=5.0)
    out = await voicer.voice("{}", tool="t", server="s", arguments={})

    assert out is None
    assert fake_db.closed is True


async def test_voicer_chat_error_returns_none_and_closes_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomLLM(LLMProvider):
        def __init__(self) -> None:
            self.closed = False

        @property
        def name(self) -> str:
            return "boom"

        async def chat(self, *a: Any, **k: Any) -> LLMResponse:
            raise RuntimeError("boom")

        async def close(self) -> None:
            self.closed = True

    boom = _BoomLLM()
    _patch_providers(monkeypatch, {ProviderKind.LLM: boom})

    voicer = build_llm_voicer(_factory(_FakeDB()), timeout_s=5.0)
    out = await voicer.voice('{"a": 1}', tool="t", server="s", arguments={})

    assert out is None
    assert boom.closed is True  # released even when the call fails


async def test_voicer_empty_completion_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_providers(monkeypatch, {ProviderKind.LLM: _RecordingLLM("   ")})
    voicer = build_llm_voicer(_factory(_FakeDB()), timeout_s=5.0)
    out = await voicer.voice('{"a": 1}', tool="t", server="s", arguments={})
    assert out is None


async def test_voicer_missing_crypto_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom_crypto() -> Any:
        raise RuntimeError("no FERNET_KEY")

    monkeypatch.setattr("app.security.crypto.get_crypto", boom_crypto)
    fake_db = _FakeDB()
    voicer = build_llm_voicer(_factory(fake_db), timeout_s=5.0)
    out = await voicer.voice('{"a": 1}', tool="t", server="s", arguments={})
    assert out is None
    # We bailed before opening a DB session, so nothing to close here.
    assert fake_db.closed is False
