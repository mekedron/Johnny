"""Tests for the S2S provider loader (Johnny-ckz.22)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from johnny.e2e.interrupt.s2s_providers import (
    S2SProviderError,
    disable_server_vad_options,
    load_s2s_provider_from_env,
    load_s2s_provider_from_json,
    required_env_for,
    supported_s2s_providers,
)


def test_supported_s2s_providers_includes_openai_and_gemini() -> None:
    providers = supported_s2s_providers()
    assert "openai-realtime" in providers
    assert "gemini-live" in providers


def test_required_env_for_openai_returns_openai_key() -> None:
    keys = required_env_for("openai-realtime")
    assert "OPENAI_API_KEY" in keys


def test_required_env_for_gemini_returns_both_keys() -> None:
    keys = required_env_for("gemini-live")
    assert "GEMINI_API_KEY" in keys
    assert "GOOGLE_API_KEY" in keys


def test_required_env_for_unknown_returns_empty() -> None:
    assert required_env_for("nonexistent-provider") == ()


def test_disable_server_vad_options_openai_realtime() -> None:
    opts = disable_server_vad_options("openai-realtime")
    assert opts == {"turn_detection": "none"}


def test_disable_server_vad_options_gemini_live() -> None:
    opts = disable_server_vad_options("gemini-live")
    assert opts == {"disable_server_vad": True}


def test_disable_server_vad_options_unknown_returns_empty() -> None:
    assert disable_server_vad_options("nonexistent") == {}


def test_load_from_env_unsupported_provider_raises() -> None:
    with pytest.raises(S2SProviderError, match="not supported by the harness"):
        load_s2s_provider_from_env("nonexistent-provider")


def test_load_from_env_no_credentials_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(S2SProviderError, match="no API key found"):
        load_s2s_provider_from_env("openai-realtime")


def test_load_from_env_success_with_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    bundle = load_s2s_provider_from_env("openai-realtime")
    assert bundle.provider_name == "openai-realtime"
    assert bundle.voice_id == "marin"


def test_load_from_env_voice_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    bundle = load_s2s_provider_from_env(
        "openai-realtime", voice_id="cedar"
    )
    assert bundle.voice_id == "cedar"


def test_load_from_env_extra_options_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-key-for-test")
    bundle = load_s2s_provider_from_env(
        "openai-realtime", extra_options={"turn_detection": "none"}
    )
    assert bundle.provider.turn_detection == "none"


def test_load_from_json_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(S2SProviderError, match="does not exist"):
        load_s2s_provider_from_json(tmp_path / "nope.json")


def test_load_from_json_wrong_version_raises(tmp_path: Path) -> None:
    f = tmp_path / "providers.json"
    f.write_text(json.dumps({"version": 99, "providers": []}))
    with pytest.raises(S2SProviderError, match="version"):
        load_s2s_provider_from_json(f)


def test_load_from_json_no_s2s_rows_raises(tmp_path: Path) -> None:
    f = tmp_path / "providers.json"
    f.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": [
                    {
                        "kind": "llm",
                        "provider_name": "openai",
                        "credentials": {"api_key": "sk-x"},
                    }
                ],
            }
        )
    )
    with pytest.raises(S2SProviderError, match="no S2S rows"):
        load_s2s_provider_from_json(f)


def test_load_from_json_picks_active_row(tmp_path: Path) -> None:
    f = tmp_path / "providers.json"
    f.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": [
                    {
                        "kind": "s2s",
                        "provider_name": "openai-realtime",
                        "display_name": "Inactive OpenAI",
                        "credentials": {"api_key": "sk-inactive"},
                        "is_active": False,
                    },
                    {
                        "kind": "s2s",
                        "provider_name": "openai-realtime",
                        "display_name": "Active OpenAI",
                        "credentials": {"api_key": "sk-active"},
                        "is_active": True,
                    },
                ],
            }
        )
    )
    bundle = load_s2s_provider_from_json(f)
    assert bundle.display_name == "Active OpenAI"


def test_load_from_json_explicit_provider_name_overrides_active(
    tmp_path: Path,
) -> None:
    f = tmp_path / "providers.json"
    f.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": [
                    {
                        "kind": "s2s",
                        "provider_name": "openai-realtime",
                        "display_name": "OpenAI row",
                        "credentials": {"api_key": "sk-x"},
                        "is_active": True,
                    },
                    {
                        "kind": "s2s",
                        "provider_name": "gemini-live",
                        "display_name": "Gemini row",
                        "credentials": {"api_key": "g-x"},
                        "is_active": False,
                    },
                ],
            }
        )
    )
    bundle = load_s2s_provider_from_json(f, provider_name="gemini-live")
    assert bundle.provider_name == "gemini-live"
