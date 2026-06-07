"""Tests for :mod:`app.providers.schema` and schema-based validation."""

from __future__ import annotations

import pytest

from app.providers import (
    AnthropicLLM,
    DeepgramSTT,
    ElevenLabsTTS,
    FasterWhisperSTT,
    GeminiLLM,
    OpenAICompatibleLLM,
    OpenAILLM,
    OpenAIRealtimeSTT,
    OpenAITTS,
    ParakeetSTT,
    PiperTTS,
)
from app.providers.schema import (
    FieldGroup,
    FieldType,
    ProviderSchema,
    ProviderTip,
)
from app.providers.schema_validation import (
    FieldValidationError,
    split_values,
    validate_payload,
)

ADAPTERS = [
    AnthropicLLM,
    DeepgramSTT,
    ElevenLabsTTS,
    FasterWhisperSTT,
    GeminiLLM,
    OpenAICompatibleLLM,
    OpenAILLM,
    OpenAIRealtimeSTT,
    OpenAITTS,
    ParakeetSTT,
    PiperTTS,
]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_each_adapter_declares_a_schema(adapter: type) -> None:
    schema = adapter.field_schema()
    assert isinstance(schema, ProviderSchema)
    assert schema.fields, f"{adapter.__name__} has no fields"
    assert schema.kind.value in {"stt", "llm", "tts"}
    assert schema.provider_name
    assert schema.display_name
    assert schema.summary


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_field_names_are_unique_within_a_schema(adapter: type) -> None:
    schema = adapter.field_schema()
    names = [f.name for f in schema.fields]
    assert len(names) == len(set(names)), f"duplicate field names in {adapter.__name__}: {names}"


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_required_fields_have_meaningful_labels(adapter: type) -> None:
    schema = adapter.field_schema()
    for field in schema.fields:
        assert field.label, f"{adapter.__name__}.{field.name} has no label"
        if field.type is FieldType.SELECT:
            assert field.options, (
                f"{adapter.__name__}.{field.name} is select but declares no options"
            )


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_to_dict_roundtrip_is_json_friendly(adapter: type) -> None:
    import json

    schema = adapter.field_schema()
    payload = schema.to_dict()
    json.dumps(payload)  # must not raise — the endpoint returns JSON


def test_validate_payload_flags_missing_required_field() -> None:
    schema = OpenAILLM.field_schema()
    errors = validate_payload(schema, {"model": "gpt-4o-mini"})
    names = [e.field for e in errors]
    assert "api_key" in names
    api_key_error = next(e for e in errors if e.field == "api_key")
    assert api_key_error.error_type == "missing"


def test_validate_payload_passes_with_required_fields() -> None:
    schema = OpenAILLM.field_schema()
    errors = validate_payload(schema, {"api_key": "sk-test"})
    assert errors == []


def test_validate_payload_rejects_unknown_select_option() -> None:
    schema = OpenAILLM.field_schema()
    errors = validate_payload(
        schema, {"api_key": "sk-test", "model": "claude-totally-fake-model"}
    )
    assert any(e.field == "model" for e in errors)


def test_validate_payload_rejects_non_numeric_for_number_field() -> None:
    schema = OpenAILLM.field_schema()
    errors = validate_payload(
        schema, {"api_key": "sk-test", "temperature": "warmish"}
    )
    assert any(e.field == "temperature" for e in errors)


def test_validate_payload_accepts_numeric_string() -> None:
    schema = OpenAILLM.field_schema()
    errors = validate_payload(
        schema, {"api_key": "sk-test", "temperature": "0.5"}
    )
    assert errors == []


def test_validate_payload_rejects_non_url_for_url_field() -> None:
    schema = OpenAILLM.field_schema()
    errors = validate_payload(
        schema, {"api_key": "sk-test", "base_url": "not a url"}
    )
    assert any(e.field == "base_url" for e in errors)


def test_validate_payload_accepts_ws_url_for_stt_endpoint() -> None:
    schema = DeepgramSTT.field_schema()
    errors = validate_payload(
        schema,
        {"api_key": "dg-test", "base_url": "wss://api.deepgram.com/v1/listen"},
    )
    assert errors == []


def test_split_values_routes_secrets_to_credentials() -> None:
    schema = OpenAILLM.field_schema()
    credentials, options = split_values(
        schema,
        {
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
            "temperature": "0.5",
            "base_url": "https://example.com/v1",
        },
    )
    assert credentials == {"api_key": "sk-test"}
    assert options["model"] == "gpt-4o-mini"
    assert options["temperature"] == 0.5
    assert options["base_url"] == "https://example.com/v1"


def test_split_values_drops_unknown_and_empty_fields() -> None:
    schema = OpenAILLM.field_schema()
    credentials, options = split_values(
        schema,
        {
            "api_key": "sk-test",
            "model": "",
            "made_up_key": "ignored",
            "temperature": None,
        },
    )
    assert credentials == {"api_key": "sk-test"}
    assert "model" not in options
    assert "made_up_key" not in options
    assert "temperature" not in options


def test_split_values_coerces_checkbox_values() -> None:
    schema = DeepgramSTT.field_schema()
    _, options = split_values(
        schema,
        {
            "api_key": "dg-test",
            "interim_results": "true",
            "punctuate": False,
            "smart_format": "yes",
        },
    )
    assert options["interim_results"] is True
    assert options["punctuate"] is False
    assert options["smart_format"] is True


def test_field_validation_error_serializes_for_fastapi() -> None:
    err = FieldValidationError(field="api_key", message="API key is required", error_type="missing")
    payload = err.to_dict()
    assert payload == {
        "loc": ["body", "api_key"],
        "msg": "API key is required",
        "type": "missing",
    }


def test_required_fields_are_grouped_with_auth_first() -> None:
    """Adapters should put required fields in the AUTH group so users see them first."""
    for adapter in ADAPTERS:
        schema = adapter.field_schema()
        for field in schema.fields:
            if field.required and field.secret:
                assert field.group is FieldGroup.AUTH, (
                    f"{adapter.__name__}.{field.name} is a required secret "
                    f"but lives in group {field.group}"
                )


LLM_ADAPTERS = [AnthropicLLM, GeminiLLM, OpenAILLM, OpenAICompatibleLLM]


@pytest.mark.parametrize("adapter", LLM_ADAPTERS)
def test_every_llm_adapter_exposes_disable_thinking_checkbox(adapter: type) -> None:
    """Every LLM provider must offer a uniform 'Disable thinking' checkbox."""
    schema = adapter.field_schema()
    field = schema.field("disable_thinking")
    assert field is not None, (
        f"{adapter.__name__} does not declare 'disable_thinking' — UI "
        f"requires this checkbox on every LLM provider"
    )
    assert field.type is FieldType.CHECKBOX
    assert field.group is FieldGroup.ADVANCED
    assert isinstance(field.default, bool)
    assert field.help_text, (
        f"{adapter.__name__}.disable_thinking has no help_text explaining "
        f"per-provider semantics"
    )


@pytest.mark.parametrize("adapter", LLM_ADAPTERS)
def test_every_llm_adapter_exposes_top_p_knob(adapter: type) -> None:
    """Nucleus sampling is a useful universal advanced knob; require it on every LLM."""
    schema = adapter.field_schema()
    field = schema.field("top_p")
    assert field is not None, f"{adapter.__name__} missing 'top_p' field"
    assert field.type is FieldType.NUMBER
    assert field.group is FieldGroup.ADVANCED


def test_disable_thinking_splits_into_options_bucket() -> None:
    """disable_thinking is non-secret and must land in the options dict, not credentials."""
    schema = OpenAILLM.field_schema()
    credentials, options = split_values(
        schema,
        {"api_key": "sk-test", "disable_thinking": True},
    )
    assert "disable_thinking" not in credentials
    assert options["disable_thinking"] is True


def test_disable_thinking_accepts_string_truthy_values() -> None:
    """HTML forms post checkbox state as 'true'/'false' strings — must coerce."""
    schema = OpenAICompatibleLLM.field_schema()
    _, options = split_values(
        schema,
        {
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434/v1",
            "disable_thinking": "true",
        },
    )
    assert options["disable_thinking"] is True


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_each_adapter_ships_tips(adapter: type) -> None:
    """Johnny-ckz.8 — operators must see latency/tuning know-how in-UI."""
    schema = adapter.field_schema()
    assert schema.tips, f"{adapter.__name__} ships no ProviderTip — operators have nothing to read"
    for tip in schema.tips:
        assert isinstance(tip, ProviderTip)
        assert tip.topic.strip(), f"{adapter.__name__} tip has an empty topic"
        assert tip.body.strip(), f"{adapter.__name__} tip '{tip.topic}' has an empty body"
        # Force the operator to actually write a sentence, not a fragment.
        assert len(tip.body) >= 30, (
            f"{adapter.__name__} tip '{tip.topic}' body is too terse to be useful"
        )


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_tips_serialize_in_to_dict(adapter: type) -> None:
    """The /providers/schemas endpoint must include tips so the frontend can render them."""
    schema = adapter.field_schema()
    payload = schema.to_dict()
    assert "tips" in payload
    assert isinstance(payload["tips"], list)
    assert len(payload["tips"]) == len(schema.tips)
    for raw_tip, expected in zip(payload["tips"], schema.tips, strict=True):
        assert raw_tip == {"topic": expected.topic, "body": expected.body}


def test_provider_schema_to_dict_omits_tips_only_when_explicitly_empty() -> None:
    """A schema with no tips still emits an empty list — never a missing key."""
    schema = ProviderSchema(
        kind=AnthropicLLM.field_schema().kind,
        provider_name="dummy",
        display_name="Dummy",
        summary="Test",
        fields=AnthropicLLM.field_schema().fields,
    )
    assert schema.tips == ()
    payload = schema.to_dict()
    assert payload["tips"] == []
