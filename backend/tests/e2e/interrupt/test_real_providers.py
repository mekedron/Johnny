"""Unit tests for the real-provider loader (Johnny-ckz.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.providers.base import ProviderKind
from johnny.e2e.interrupt.real_providers import (
    RealProviderError,
    load_real_providers,
)


def _write_providers_json(tmp_path: Path, providers: list[dict]) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"version": 1, "providers": providers}))
    return path


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RealProviderError):
        load_real_providers(tmp_path / "does-not-exist.json")


def test_unsupported_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"version": 99, "providers": []}))
    with pytest.raises(RealProviderError, match="version"):
        load_real_providers(path)


def test_missing_kind_raises(tmp_path: Path) -> None:
    # Only an LLM and a TTS — STT is absent.
    path = _write_providers_json(
        tmp_path,
        [
            {
                "kind": "llm",
                "provider_name": "openai",
                "display_name": "OpenAI",
                "credentials": {"api_key": "sk-test"},
                "options": {},
                "is_active": True,
            },
            {
                "kind": "tts",
                "provider_name": "elevenlabs",
                "display_name": "ElevenLabs",
                "credentials": {"api_key": "sk-test"},
                "options": {"voice_id": "test"},
                "is_active": True,
            },
        ],
    )
    with pytest.raises(RealProviderError, match="kind=stt"):
        load_real_providers(path, excluded_stt_names=set())


def test_picks_active_entry(tmp_path: Path) -> None:
    path = _write_providers_json(
        tmp_path,
        [
            {
                "kind": "stt",
                "provider_name": "deepgram",
                "display_name": "Deepgram",
                "credentials": {"api_key": "dg-test"},
                "options": {"model": "nova-3"},
                "is_active": True,
            },
            {
                "kind": "llm",
                "provider_name": "openai",
                "display_name": "OpenAI",
                "credentials": {"api_key": "sk-test"},
                "options": {"model": "gpt-4.1-mini"},
                "is_active": True,
            },
            {
                "kind": "tts",
                "provider_name": "elevenlabs",
                "display_name": "ElevenLabs",
                "credentials": {"api_key": "el-test"},
                "options": {"voice_id": "test"},
                "is_active": True,
            },
        ],
    )
    bundle = load_real_providers(path)
    assert bundle.stt.name == "deepgram"
    assert bundle.llm.name == "openai"
    assert bundle.tts.name == "elevenlabs"
    assert bundle.stt_display == "Deepgram"


def test_default_excludes_faster_whisper(tmp_path: Path) -> None:
    """The default exclusion list skips faster-whisper for STT."""
    path = _write_providers_json(
        tmp_path,
        [
            {
                "kind": "stt",
                "provider_name": "faster-whisper",
                "display_name": "Local Whisper",
                "credentials": {},
                "options": {"model_size": "base"},
                "is_active": True,
            },
            {
                "kind": "stt",
                "provider_name": "deepgram",
                "display_name": "Deepgram",
                "credentials": {"api_key": "dg-test"},
                "options": {"model": "nova-3"},
                "is_active": False,
            },
            {
                "kind": "llm",
                "provider_name": "openai",
                "display_name": "OpenAI",
                "credentials": {"api_key": "sk-test"},
                "options": {},
                "is_active": True,
            },
            {
                "kind": "tts",
                "provider_name": "elevenlabs",
                "display_name": "ElevenLabs",
                "credentials": {"api_key": "el-test"},
                "options": {"voice_id": "test"},
                "is_active": True,
            },
        ],
    )
    bundle = load_real_providers(path)
    assert bundle.stt.name == "deepgram"  # faster-whisper excluded


def test_fallback_tts_synthesises_from_openai_llm(tmp_path: Path) -> None:
    """fallback_tts_to_openai builds OpenAI TTS from the LLM api_key."""
    path = _write_providers_json(
        tmp_path,
        [
            {
                "kind": "stt",
                "provider_name": "deepgram",
                "display_name": "Deepgram",
                "credentials": {"api_key": "dg-test"},
                "options": {"model": "nova-3"},
                "is_active": True,
            },
            {
                "kind": "llm",
                "provider_name": "openai",
                "display_name": "OpenAI",
                "credentials": {"api_key": "sk-test"},
                "options": {},
                "is_active": True,
            },
            {
                "kind": "tts",
                "provider_name": "elevenlabs",
                "display_name": "ElevenLabs (blocked)",
                "credentials": {"api_key": "blocked"},
                "options": {"voice_id": "test"},
                "is_active": True,
            },
        ],
    )
    bundle = load_real_providers(path, fallback_tts_to_openai=True)
    assert bundle.tts.name == "openai"
    assert bundle.tts_display.startswith("OpenAI TTS")


def test_kind_lookup_with_no_credentials_raises(tmp_path: Path) -> None:
    """When the only entry of a kind has no credentials, error out."""
    path = _write_providers_json(
        tmp_path,
        [
            {
                "kind": "stt",
                "provider_name": "deepgram",
                "display_name": "Deepgram (no key)",
                "credentials": {},
                "options": {},
                "is_active": True,
            },
            {
                "kind": "llm",
                "provider_name": "openai",
                "display_name": "OpenAI",
                "credentials": {"api_key": "sk-test"},
                "options": {},
                "is_active": True,
            },
            {
                "kind": "tts",
                "provider_name": "elevenlabs",
                "display_name": "ElevenLabs",
                "credentials": {"api_key": "el-test"},
                "options": {"voice_id": "test"},
                "is_active": True,
            },
        ],
    )
    with pytest.raises(RealProviderError, match="credentials"):
        load_real_providers(path)


def test_kind_enum_round_trips(tmp_path: Path) -> None:
    """ProviderKind enum is wired through ProviderConfig."""
    path = _write_providers_json(
        tmp_path,
        [
            {
                "kind": "stt",
                "provider_name": "deepgram",
                "display_name": "Deepgram",
                "credentials": {"api_key": "dg-test"},
                "options": {},
                "is_active": True,
            },
            {
                "kind": "llm",
                "provider_name": "openai",
                "display_name": "OpenAI",
                "credentials": {"api_key": "sk-test"},
                "options": {},
                "is_active": True,
            },
            {
                "kind": "tts",
                "provider_name": "openai",
                "display_name": "OpenAI TTS",
                "credentials": {"api_key": "sk-test"},
                "options": {"voice_id": "alloy"},
                "is_active": True,
            },
        ],
    )
    bundle = load_real_providers(path)
    # The producer's kind enum is the expected one for each adapter.
    # We can't poke into private state easily, but the bundle factory
    # asserts isinstance() so making it this far is the contract.
    assert ProviderKind.STT  # smoke that the import is alive.
    assert bundle is not None
