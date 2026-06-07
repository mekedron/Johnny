"""Per-provider field schema for structured configuration UIs.

Each concrete provider adapter declares a :class:`ProviderSchema` via the
``field_schema()`` classmethod. The schema describes every configurable
field — label, type, whether it is a secret, where it should be stored
(credentials vs options), and inline guidance for the UI.

The same schema feeds:

* the ``GET /providers/schemas`` endpoint that drives the SvelteKit
  ``/providers`` UI (replacing the previous free-text textareas);
* the interactive setup wizard (``johnny.wizard``), so the CLI prompts
  for exactly the same field names the runtime adapter expects;
* the server-side validator that turns missing required keys into
  field-level HTTP 422 errors before any credential is encrypted.

The split between ``credentials`` and ``options`` is driven by the
:attr:`FieldDef.secret` flag — secrets land in the encrypted blob, the
rest in the plain ``config`` JSONB column. The frontend never needs to
hardcode that mapping; it just submits a flat ``values`` dict and the
backend splits according to the schema.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from app.providers.base import ProviderKind


class FieldType(enum.StrEnum):
    """How a field should be rendered and parsed.

    These map cleanly to HTML input types so the frontend can render
    each field without bespoke per-provider components.
    """

    TEXT = "text"
    PASSWORD = "password"
    URL = "url"
    NUMBER = "number"
    SELECT = "select"
    CHECKBOX = "checkbox"
    TEXTAREA = "textarea"


class FieldGroup(enum.StrEnum):
    """Logical grouping the UI uses to lay out related fields.

    ``AUTH`` fields (api_key, base_url) come first so the user sees the
    minimum required to authenticate. ``MODEL`` covers identifier/voice
    selection. ``ADVANCED`` collects tuning knobs the user usually wants
    to leave at their defaults.
    """

    AUTH = "auth"
    MODEL = "model"
    ADVANCED = "advanced"


@dataclass(frozen=True, slots=True)
class FieldOption:
    """One choice in a :attr:`FieldType.SELECT` field."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ProviderTip:
    """One actionable tip rendered in the provider settings modal (Johnny-ckz.8).

    A tip is the in-UI capture of know-how that the next operator tuning
    this provider would otherwise have to rediscover from scratch — voice
    tier vs latency, model size vs accuracy, CPU vs GPU rules of thumb.
    The frontend renders the full ``tips`` tuple as a dedicated "Latency
    & tuning tips" section in the modal so the knowledge lives next to
    the knobs an operator is about to twist.

    ``topic`` is the short headline (5-8 words). ``body`` is one or two
    sentences in plain language, ideally naming a concrete number
    measured on the local stack ("medium voices add ~50 ms vs low on
    CPU"). Keep marketing fluff out — these are read at 11pm before a
    demo, not on a landing page.
    """

    topic: str
    body: str

    def to_dict(self) -> dict[str, str]:
        return {"topic": self.topic, "body": self.body}


@dataclass(frozen=True, slots=True)
class FieldDef:
    """A single configurable field on a provider form.

    ``name`` is the dict key the adapter reads from credentials (when
    ``secret=True``) or options (otherwise). ``required`` and ``type``
    drive validation. ``placeholder`` / ``help_text`` / ``signup_url``
    feed the UI's inline guidance so users don't have to guess.
    """

    name: str
    label: str
    type: FieldType = FieldType.TEXT
    required: bool = False
    secret: bool = False
    placeholder: str | None = None
    help_text: str | None = None
    default: Any = None
    options: tuple[FieldOption, ...] = ()
    group: FieldGroup = FieldGroup.AUTH
    signup_url: str | None = None
    env_key: str | None = None  # `.env` var the wizard prefills from
    # When True, the frontend renders the unified voice picker (Johnny-1ge.8)
    # for this field instead of a plain SELECT: it fetches the provider's
    # `list_voices()` catalog (`GET /providers/{id}/voices` for a saved row,
    # `POST /providers/preview/voices` before save) and shows filterable rows
    # with per-voice Preview buttons. The `options` tuple stays the offline
    # fallback when the catalog can't be fetched (no creds yet, network down).
    voice_catalog: bool = False
    # When True, this SELECT's value is sourced from a live catalog fetched
    # from the provider's own API (e.g. the LLM model list, Johnny-9eq), so a
    # build-time `options` allow-list cannot be authoritative. The validator
    # SKIPS the `value ∈ options` membership check for such fields — any
    # non-empty string within a sane length cap is accepted, and a bad id
    # surfaces as a clean upstream error on first use rather than a hardcoded
    # schema rejection (Johnny-ckz.29). The `options` tuple stays the offline
    # fallback the dropdown shows when the live catalog can't be fetched.
    dynamic_options: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the ``GET /providers/schemas`` payload."""
        payload: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "type": self.type.value,
            "required": self.required,
            "secret": self.secret,
            "group": self.group.value,
        }
        if self.placeholder is not None:
            payload["placeholder"] = self.placeholder
        if self.help_text is not None:
            payload["help_text"] = self.help_text
        if self.default is not None:
            payload["default"] = self.default
        if self.options:
            payload["options"] = [
                {"value": o.value, "label": o.label} for o in self.options
            ]
        if self.signup_url is not None:
            payload["signup_url"] = self.signup_url
        if self.env_key is not None:
            payload["env_key"] = self.env_key
        if self.voice_catalog:
            payload["voice_catalog"] = True
        if self.dynamic_options:
            payload["dynamic_options"] = True
        return payload


@dataclass(frozen=True, slots=True)
class ProviderSchema:
    """Form definition for one provider adapter.

    ``kind`` + ``provider_name`` jointly identify the registry key (e.g.
    ``(LLM, "openai")``). ``display_name`` is the human-friendly label
    used in card headers; ``summary`` is the one-liner that appears
    under the name. ``signup_url`` is the global "Get started →" link
    rendered next to the title even when no individual field carries
    its own.
    """

    kind: ProviderKind
    provider_name: str
    display_name: str
    summary: str
    signup_url: str | None = None
    fields: tuple[FieldDef, ...] = field(default_factory=tuple)
    tips: tuple[ProviderTip, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the ``GET /providers/schemas`` payload."""
        return {
            "kind": self.kind.value,
            "provider_name": self.provider_name,
            "display_name": self.display_name,
            "summary": self.summary,
            "signup_url": self.signup_url,
            "fields": [f.to_dict() for f in self.fields],
            "tips": [t.to_dict() for t in self.tips],
        }

    def field(self, name: str) -> FieldDef | None:
        """Look up a field by its canonical name. Returns ``None`` if absent."""
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def credential_fields(self) -> tuple[FieldDef, ...]:
        """All fields stored in the encrypted credentials blob."""
        return tuple(f for f in self.fields if f.secret)

    def option_fields(self) -> tuple[FieldDef, ...]:
        """All fields stored in the plain options dict."""
        return tuple(f for f in self.fields if not f.secret)


__all__ = [
    "FieldDef",
    "FieldGroup",
    "FieldOption",
    "FieldType",
    "ProviderSchema",
    "ProviderTip",
]
