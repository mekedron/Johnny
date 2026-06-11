"""SKILL.md frontmatter parsing — openclaw / AgentSkills wire-compatible.

A skill package is a directory holding a ``SKILL.md`` whose YAML frontmatter
carries the machine-readable contract; the Markdown body is the *instruction
sheet* (consumed by the execution engine's prompt, Johnny-trt.22/24, and by
humans). The format follows openclaw so ClawHub-style skills drop into the
skills volume unchanged:

* ``name`` and ``description`` are required strings.
* ``metadata`` is optional and carries per-consumer manifest namespaces. It
  may be a YAML mapping (openclaw's shipped skills use a multi-line flow
  mapping, which YAML parses natively) **or** a string holding one line of
  JSON — both shapes land in the same dict.
* The openclaw manifest lives under ``metadata.openclaw`` (legacy key
  ``clawdbot`` still read, mirroring openclaw's own compat shim):
  ``requires.bins`` / ``requires.anyBins`` / ``requires.env`` /
  ``requires.config``, an ``os`` platform list, plus fields this parser
  carries but does not interpret (``emoji``, ``install`` specs, …).
* Johnny's own additive namespace lives under ``metadata.johnny`` and is
  invisible to openclaw consumers:

  - ``run`` — the v1 deterministic runner spec (Johnny-trt.23):
    ``{"argv": [...], "timeout_s": 60}``. The argv runs inside the
    skills-sandbox via ``sandbox.exec``; exit 0 settles the task ``done``
    with stdout as the speech-ready result, any other exit settles
    ``failed`` (stdout, when present, is the skill-authored spoken failure
    copy). Skills without a ``run`` spec are still discovered, gated, and
    cataloged — they become runnable when the LLM execution engine lands
    (Johnny-trt.22 decides it, Johnny-trt.24 wires it).
  - ``availability`` — the Johnny-trt.55 capability probe:
    ``{"check": {"argv": [...], "timeout_s": 10}, "unavailable_reason": "…"}``.
    The check runs in-sandbox at catalog assembly and again at claim time;
    exit 0 means the capability is usable now, any other exit marks the
    catalog entry unavailable with stdout (or ``unavailable_reason``) as the
    spoken-form actionable reason the router declines with.
  - ``keywords`` — English trigger words for the heuristic scorer's
    delegate-prior dimension (Johnny-trt.50). Never rendered into prompts.

Parsing never raises on document content: every defect is collected into
:attr:`SkillDocument.problems` so the loader can list a broken skill *with
its reason* instead of silently dropping it (Johnny-trt.23 acceptance).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import yaml

MANIFEST_KEYS: tuple[str, ...] = ("openclaw", "clawdbot")
"""Metadata namespaces probed for the openclaw manifest block, current name
first (mirrors openclaw's ``MANIFEST_KEY`` + ``LEGACY_MANIFEST_KEYS``)."""

JOHNNY_METADATA_KEY = "johnny"
"""Johnny's additive metadata namespace — ignored by openclaw consumers."""


@dataclass(frozen=True, slots=True)
class SkillRequirements:
    """Runtime requirements advertised by a skill's openclaw manifest.

    ``bins`` must *all* resolve inside the sandbox; ``any_bins`` is satisfied
    by any one member. ``env`` / ``config`` are carried for the Phase-4
    availability predicate (Johnny-trt.55) — v1 eligibility does not evaluate
    them.
    """

    bins: tuple[str, ...] = ()
    any_bins: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    config: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillRunSpec:
    """The v1 deterministic runner (``metadata.johnny.run``).

    ``argv`` is executed verbatim inside the skills-sandbox (never a shell
    string — composition belongs in a script the skill ships). ``timeout_s``
    is clamped by the sandbox's own ceiling; ``None`` means the sandbox
    default.
    """

    argv: tuple[str, ...]
    timeout_s: float | None = None


@dataclass(frozen=True, slots=True)
class SkillAvailabilitySpec:
    """The skill-declared availability probe (``metadata.johnny.availability``).

    The credential/integration dimension of Johnny-trt.55's availability
    predicate, owned by the skill exactly like the run contract: ``check``
    runs in-sandbox at catalog assembly (and again at claim time) — exit 0
    means the capability is usable now; any other exit means unavailable,
    with stdout as the skill-authored spoken-form reason (actionable: name
    what is missing and the fix). ``unavailable_reason`` is the fallback
    spoken copy when a failing check prints nothing.
    """

    check: SkillRunSpec | None = None
    unavailable_reason: str = ""


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """One parsed SKILL.md — fields plus every problem found on the way.

    ``manifest`` / ``johnny`` keep the raw namespace dicts so consumers can
    read fields this parser does not interpret (``install`` specs, ``emoji``,
    future keys) without a reparse. A document with a non-empty
    :attr:`problems` is *loadable but defective*: the loader lists it as
    ineligible with these reasons.
    """

    name: str = ""
    description: str = ""
    homepage: str = ""
    os: tuple[str, ...] = ()
    requires: SkillRequirements = field(default_factory=SkillRequirements)
    run: SkillRunSpec | None = None
    availability: SkillAvailabilitySpec | None = None
    keywords: tuple[str, ...] = ()
    body: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    johnny: dict[str, Any] = field(default_factory=dict)
    problems: tuple[str, ...] = ()


def _string_tuple(value: Any, *, label: str, problems: list[str]) -> tuple[str, ...]:
    """Normalize a manifest list-of-strings field, recording defects."""
    if value is None:
        return ()
    if isinstance(value, str):
        # openclaw tolerates comma-separated loose strings; mirror that.
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                items.append(entry.strip())
            else:
                problems.append(f"{label} entries must be non-empty strings (got {entry!r})")
        return tuple(items)
    problems.append(f"{label} must be a list of strings (got {type(value).__name__})")
    return ()


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return ``(frontmatter_yaml, body)``; frontmatter ``None`` when absent.

    The block must open the file: a ``---`` first line and a closing ``---``
    line (trailing whitespace tolerated, BOM stripped) — the openclaw shape.
    """
    stripped = text.lstrip("﻿")
    lines = stripped.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None, text


def _parse_metadata(raw: Any, problems: list[str]) -> dict[str, Any]:
    """Coerce the ``metadata`` field into a dict.

    A YAML mapping passes through; a string must hold JSON (the
    "single-line metadata JSON" shape from the acceptance criteria).
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(f"metadata string is not valid JSON: {exc}")
            return {}
        if isinstance(parsed, dict):
            return parsed
        problems.append("metadata JSON must be an object")
        return {}
    problems.append(f"metadata must be a mapping or a JSON string (got {type(raw).__name__})")
    return {}


def _parse_requires(manifest: dict[str, Any], problems: list[str]) -> SkillRequirements:
    raw = manifest.get("requires")
    if raw is None:
        return SkillRequirements()
    if not isinstance(raw, dict):
        problems.append(f"requires must be a mapping (got {type(raw).__name__})")
        return SkillRequirements()
    return SkillRequirements(
        bins=_string_tuple(raw.get("bins"), label="requires.bins", problems=problems),
        any_bins=_string_tuple(
            # openclaw spells it anyBins; accept snake_case for hand-written files.
            raw.get("anyBins", raw.get("any_bins")),
            label="requires.anyBins",
            problems=problems,
        ),
        env=_string_tuple(raw.get("env"), label="requires.env", problems=problems),
        config=_string_tuple(raw.get("config"), label="requires.config", problems=problems),
    )


def _parse_run_spec(raw: Any, *, label: str, problems: list[str]) -> SkillRunSpec | None:
    """Parse one ``{"argv": [...], "timeout_s": N}`` command spec (run / check)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append(f"{label} must be a mapping (got {type(raw).__name__})")
        return None
    argv_raw = raw.get("argv")
    if (
        not isinstance(argv_raw, (list, tuple))
        or not argv_raw
        or not all(isinstance(a, str) and a.strip() for a in argv_raw)
    ):
        problems.append(f"{label}.argv must be a non-empty list of strings")
        return None
    timeout_raw = raw.get("timeout_s")
    timeout: float | None = None
    if timeout_raw is not None:
        valid_number = isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool)
        if valid_number and timeout_raw > 0:
            timeout = float(timeout_raw)
        else:
            problems.append(f"{label}.timeout_s must be a positive number")
    return SkillRunSpec(argv=tuple(argv_raw), timeout_s=timeout)


def _parse_run(johnny: dict[str, Any], problems: list[str]) -> SkillRunSpec | None:
    return _parse_run_spec(johnny.get("run"), label="johnny.run", problems=problems)


def _parse_availability(
    johnny: dict[str, Any], problems: list[str]
) -> SkillAvailabilitySpec | None:
    raw = johnny.get("availability")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append(f"johnny.availability must be a mapping (got {type(raw).__name__})")
        return None
    check = _parse_run_spec(
        raw.get("check"), label="johnny.availability.check", problems=problems
    )
    reason_raw = raw.get("unavailable_reason", raw.get("unavailableReason"))
    reason = reason_raw.strip() if isinstance(reason_raw, str) else ""
    if reason_raw is not None and not isinstance(reason_raw, str):
        problems.append("johnny.availability.unavailable_reason must be a string")
    return SkillAvailabilitySpec(check=check, unavailable_reason=reason)


def parse_skill_markdown(text: str) -> SkillDocument:
    """Parse one SKILL.md's text into a :class:`SkillDocument`.

    Never raises on content: structural defects (no frontmatter, invalid
    YAML, missing name/description, malformed metadata) are recorded in
    :attr:`SkillDocument.problems` and whatever *did* parse is kept, so the
    loader can show a broken skill with its reason.
    """
    problems: list[str] = []
    frontmatter_text, body = _split_frontmatter(text)
    if frontmatter_text is None:
        return SkillDocument(
            body=text,
            problems=("SKILL.md has no frontmatter block (expected a leading '---' section)",),
        )

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        return SkillDocument(
            body=body,
            problems=(f"frontmatter is not valid YAML: {exc}",),
        )
    if not isinstance(frontmatter, dict):
        return SkillDocument(body=body, problems=("frontmatter must be a YAML mapping",))

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("frontmatter is missing the required 'name' field")
        name = ""
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("frontmatter is missing the required 'description' field")
        description = ""
    homepage = frontmatter.get("homepage")
    homepage = homepage.strip() if isinstance(homepage, str) else ""

    metadata = _parse_metadata(frontmatter.get("metadata"), problems)
    manifest: dict[str, Any] = {}
    for key in MANIFEST_KEYS:
        candidate = metadata.get(key)
        if isinstance(candidate, dict):
            manifest = candidate
            break
    johnny_raw = metadata.get(JOHNNY_METADATA_KEY)
    johnny = johnny_raw if isinstance(johnny_raw, dict) else {}
    if johnny_raw is not None and not isinstance(johnny_raw, dict):
        problems.append(f"metadata.johnny must be a mapping (got {type(johnny_raw).__name__})")

    return SkillDocument(
        name=name.strip(),
        description=description.strip(),
        homepage=homepage,
        os=tuple(
            entry.lower()
            for entry in _string_tuple(manifest.get("os"), label="os", problems=problems)
        ),
        requires=_parse_requires(manifest, problems),
        run=_parse_run(johnny, problems),
        availability=_parse_availability(johnny, problems),
        keywords=_string_tuple(johnny.get("keywords"), label="johnny.keywords", problems=problems),
        body=body,
        manifest=manifest,
        johnny=johnny,
        problems=tuple(problems),
    )


__all__ = [
    "JOHNNY_METADATA_KEY",
    "MANIFEST_KEYS",
    "SkillAvailabilitySpec",
    "SkillDocument",
    "SkillRequirements",
    "SkillRunSpec",
    "parse_skill_markdown",
]
