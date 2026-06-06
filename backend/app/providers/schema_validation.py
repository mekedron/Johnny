"""Validation of provider create/update payloads against field schemas.

The ``GET /providers/schemas`` endpoint surfaces the same FieldDef-based
description used here. ``validate_payload`` is the server-side gate that
turns a missing required key, an out-of-range number, or an unknown
``select`` option into a structured field-level error, ready to be
returned as HTTP 422 with ``{loc: [...], msg: "...", type: "..."}`` shape
matching what FastAPI / Pydantic emit elsewhere.

The validator is shape-agnostic: it accepts the flat ``values`` dict
the frontend sends and verifies each entry against the matching FieldDef.
Bucket-splitting (credentials vs options) is done by ``split_values``
after validation succeeds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.providers.schema import FieldDef, FieldType, ProviderSchema


@dataclass(frozen=True, slots=True)
class FieldValidationError:
    """One structured field-level validation failure."""

    field: str
    message: str
    error_type: str = "value_error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "loc": ["body", self.field],
            "msg": self.message,
            "type": self.error_type,
        }


def validate_payload(
    schema: ProviderSchema,
    values: Mapping[str, Any],
) -> list[FieldValidationError]:
    """Validate a flat ``values`` dict against ``schema``.

    Returns a list of structured errors. An empty list means the payload
    passes. Unknown keys are tolerated (the form may carry extra
    transient state) but logged-style errors do not surface — only
    declared fields are validated.
    """
    errors: list[FieldValidationError] = []
    declared = {f.name: f for f in schema.fields}

    for name, fdef in declared.items():
        if name not in values or _is_empty(values[name]):
            if fdef.required:
                errors.append(
                    FieldValidationError(
                        field=name,
                        message=f"{fdef.label} is required",
                        error_type="missing",
                    )
                )
            continue
        raw = values[name]
        type_error = _check_type(raw, fdef)
        if type_error is not None:
            errors.append(
                FieldValidationError(field=name, message=type_error, error_type="type_error")
            )
            continue
        option_error = _check_option(raw, fdef)
        if option_error is not None:
            errors.append(
                FieldValidationError(
                    field=name, message=option_error, error_type="value_error"
                )
            )

    return errors


def split_values(
    schema: ProviderSchema,
    values: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Partition a flat ``values`` dict into (credentials, options).

    Secret fields land in the encrypted credentials blob, all other
    declared fields land in the plain options dict. Empty / ``None`` /
    unknown values are dropped. Number fields are coerced to int/float
    so the adapter ``__init__`` doesn't need to parse strings.
    """
    credentials: dict[str, str] = {}
    options: dict[str, Any] = {}
    for fdef in schema.fields:
        if fdef.name not in values:
            continue
        raw = values[fdef.name]
        if _is_empty(raw):
            continue
        if fdef.secret:
            credentials[fdef.name] = str(raw)
        else:
            options[fdef.name] = _coerce(raw, fdef)
    return credentials, options


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _check_type(value: Any, fdef: FieldDef) -> str | None:
    if fdef.type is FieldType.NUMBER:
        try:
            float(value)
        except (TypeError, ValueError):
            return f"{fdef.label} must be a number"
    elif fdef.type is FieldType.URL:
        if not isinstance(value, str):
            return f"{fdef.label} must be a string"
        prefixes = ("http://", "https://", "ws://", "wss://")
        if not value.startswith(prefixes):
            return (
                f"{fdef.label} must start with http://, https://, "
                "ws:// or wss://"
            )
    elif fdef.type is FieldType.CHECKBOX:
        if not isinstance(value, bool | int | str):
            return f"{fdef.label} must be a boolean"
    elif fdef.type in (
        FieldType.TEXT,
        FieldType.PASSWORD,
        FieldType.SELECT,
        FieldType.TEXTAREA,
    ):
        if not isinstance(value, str | int | float | bool):
            return f"{fdef.label} must be text"
    return None


def _check_option(value: Any, fdef: FieldDef) -> str | None:
    if fdef.type is not FieldType.SELECT or not fdef.options:
        return None
    allowed = {opt.value for opt in fdef.options}
    if str(value) not in allowed:
        joined = ", ".join(sorted(allowed))
        return f"{fdef.label} must be one of: {joined}"
    return None


def _coerce(value: Any, fdef: FieldDef) -> Any:
    if fdef.type is FieldType.NUMBER:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return value
        return int(numeric) if numeric.is_integer() else numeric
    if fdef.type is FieldType.CHECKBOX:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"true", "1", "yes", "on"}
        return bool(value)
    return value


__all__ = [
    "FieldValidationError",
    "split_values",
    "validate_payload",
]
