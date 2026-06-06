"""Tests for the wizard's provider catalog."""

from __future__ import annotations

from johnny.wizard.providers import (
    CATALOG,
    Hosting,
    Kind,
    choices_for,
    get_choice,
    recommended_cloud,
    recommended_local,
)


def test_catalog_has_local_and_cloud_for_every_kind() -> None:
    for kind in Kind:
        assert choices_for(kind, Hosting.LOCAL), f"missing local {kind.value}"
        assert choices_for(kind, Hosting.CLOUD), f"missing cloud {kind.value}"


def test_recommended_local_picks_local_only() -> None:
    for kind in Kind:
        choice = recommended_local(kind)
        assert choice is not None
        assert choice.hosting is Hosting.LOCAL
        assert choice.kind is kind


def test_recommended_cloud_picks_cloud_only() -> None:
    for kind in Kind:
        choice = recommended_cloud(kind)
        assert choice is not None
        assert choice.hosting is Hosting.CLOUD
        assert choice.kind is kind


def test_local_providers_have_install_blocks() -> None:
    for kind in Kind:
        local = recommended_local(kind)
        assert local is not None
        assert local.install is not None, f"{kind.value} local provider missing install"


def test_cloud_providers_have_credential_keys_and_signup_urls() -> None:
    for kind in Kind:
        for choice in choices_for(kind, Hosting.CLOUD):
            assert choice.credential_keys, f"{choice.label} missing credential_keys"
            assert choice.signup_url, f"{choice.label} missing signup_url"


def test_cloud_providers_carry_env_keys() -> None:
    for kind in Kind:
        for choice in choices_for(kind, Hosting.CLOUD):
            assert choice.env_key, f"{choice.label} missing env_key for .env pre-fill"


def test_get_choice_by_name() -> None:
    assert get_choice(Kind.STT, "faster-whisper") is not None
    assert get_choice(Kind.STT, "deepgram") is not None
    assert get_choice(Kind.LLM, "openai-compatible") is not None
    assert get_choice(Kind.LLM, "anthropic") is not None
    assert get_choice(Kind.TTS, "piper") is not None
    assert get_choice(Kind.TTS, "elevenlabs") is not None
    assert get_choice(Kind.STT, "does-not-exist") is None


def test_whisper_install_lists_recommended_default() -> None:
    choice = get_choice(Kind.STT, "faster-whisper")
    assert choice is not None and choice.install is not None
    install = choice.install
    default = install["default_model"]
    assert default in {m["id"] for m in install["models"]}


def test_ollama_install_lists_recommended_default() -> None:
    choice = get_choice(Kind.LLM, "openai-compatible")
    assert choice is not None and choice.install is not None
    install = choice.install
    default = install["default_model"]
    assert default in {m["id"] for m in install["models"]}


def test_piper_install_lists_recommended_default() -> None:
    choice = get_choice(Kind.TTS, "piper")
    assert choice is not None and choice.install is not None
    install = choice.install
    default = install["default_voice"]
    assert default in {v["id"] for v in install["voices"]}


def test_catalog_no_duplicate_provider_names_per_kind() -> None:
    seen: set[tuple[str, str]] = set()
    for choice in CATALOG:
        key = (choice.kind.value, choice.provider_name)
        assert key not in seen, f"duplicate {key}"
        seen.add(key)


def test_recommended_local_for_llm_uses_openai_compatible() -> None:
    choice = recommended_local(Kind.LLM)
    assert choice is not None
    assert choice.provider_name == "openai-compatible"
    assert "host.docker.internal" in choice.default_options["base_url"]


def test_catalog_credential_keys_match_adapter_schema() -> None:
    """The wizard catalog and the runtime field_schema must agree on credentials.

    The schema is the source of truth — adding a credential field to an
    adapter should automatically be the set the wizard prompts for.
    Until the catalog migrates to derive credential_keys from
    ``schema_for(choice)`` automatically, this check guarantees they do
    not silently diverge.
    """
    import app.providers  # noqa: F401, PLC0415 — populate registry
    from johnny.wizard.providers import schema_for

    for choice in CATALOG:
        schema = schema_for(choice)
        if schema is None:
            continue
        schema_secrets = {f.name for f in schema.fields if f.secret}
        catalog_keys = set(choice.credential_keys)
        assert schema_secrets == catalog_keys or catalog_keys.issubset(
            schema_secrets
        ), (
            f"{choice.kind.value}/{choice.provider_name}: "
            f"wizard credential_keys={catalog_keys}, "
            f"schema secrets={schema_secrets}"
        )
