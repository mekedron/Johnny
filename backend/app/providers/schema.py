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

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the ``GET /providers/schemas`` payload."""
        return {
            "kind": self.kind.value,
            "provider_name": self.provider_name,
            "display_name": self.display_name,
            "summary": self.summary,
            "signup_url": self.signup_url,
            "fields": [f.to_dict() for f in self.fields],
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
]
