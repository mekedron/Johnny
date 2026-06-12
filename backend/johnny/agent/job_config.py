"""Agent job-payload contract (spike Johnny-y4j; consumed by Johnny-7we).

This module defines :class:`SessionJobConfig` — the single, serialisable
description of *one Meet session* that the orchestrator (the API / session
scheduler) hands to the LiveKit **agent worker** when it dispatches the agent
into that session's room. It is the LiveKit-era replacement for the bag of
``JOHNNY_*`` environment variables the Docker launcher
(:mod:`app.services.docker_launcher`) sets on each spawned meet-worker today,
and it mirrors that contract field-for-field so the two paths stay in lockstep
during the migration (epic Johnny-7g5).

Transport: the config is serialised to a JSON string via :meth:`to_metadata`
and delivered as the **agent dispatch metadata** (``CreateAgentDispatchRequest.
metadata`` → ``JobContext.job.metadata``); the agent reconstructs it with
:meth:`from_metadata`. See :mod:`johnny.agent.dispatch` for the dispatch call
and ``docs/livekit-room-auth-and-dispatch.md`` for the decision record.

Deliberately **stdlib-only** (no ``livekit`` import, no ``app.providers`` /
``sqlalchemy``): the contract is shared by the API (which builds it), the agent
worker (which parses it), and unit tests, so it must import cheaply everywhere —
the same import-safety discipline the top-level :mod:`johnny.agent` package
holds. The mode literals are duplicated here rather than imported from the
heavyweight engine modules; a drift guard in ``tests/agent/test_job_config.py``
asserts they still match the canonical definitions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

# --- Behaviour vocabularies -------------------------------------------------
# These mirror the canonical constants in
# ``johnny.voice_pipeline.reasoning`` (LISTEN_ONLY_MODE, …). They are
# re-declared here to keep this module dependency-free; the drift guard test
# fails if the canonical values ever diverge.
LISTEN_ONLY_MODE = "listen_only"
SUGGEST_ONLY_MODE = "suggest_only"
APPROVAL_REQUIRED_MODE = "approval_required"
LIMITED_AUTO_SPEAK_MODE = "limited_auto_speak"
# Free-form full-auto-speak: a first-class legacy SPEAKING_MODE and the sole
# FREE_FORM_MODE, which the agent answer path (johnny.agent.answer) already
# special-cases. It MUST be accepted here — if a dispatch for an autonomous-mode
# meeting hits from_metadata without it, the contract rejects the payload and the
# worker abandons the job, so the bot silently no-shows (cutover gap, Johnny-52b).
AUTONOMOUS_MODE = "autonomous"
SUPPORTED_MODES: frozenset[str] = frozenset(
    {
        LISTEN_ONLY_MODE,
        SUGGEST_ONLY_MODE,
        APPROVAL_REQUIRED_MODE,
        LIMITED_AUTO_SPEAK_MODE,
        AUTONOMOUS_MODE,
    }
)
DEFAULT_MODE = LISTEN_ONLY_MODE

# --- Room / identity naming -------------------------------------------------
# One LiveKit room per Meet session, named off the durable bot_session_id so
# the bridge, the agent, and the API all derive the same room without a side
# channel. Identity prefixes keep the two participants distinguishable in
# ``list_participants`` and in LiveKit's logs/observability.
ROOM_NAME_PREFIX = "johnny-session"
BRIDGE_IDENTITY_PREFIX = "meet-bridge"
AGENT_IDENTITY_PREFIX = "johnny-agent"


def room_name_for_session(bot_session_id: int | str) -> str:
    """Canonical LiveKit room name for a Meet session (one room per session)."""
    return f"{ROOM_NAME_PREFIX}-{bot_session_id}"


def bridge_identity_for_session(bot_session_id: int | str) -> str:
    """Participant identity for the meet-worker↔room bridge."""
    return f"{BRIDGE_IDENTITY_PREFIX}-{bot_session_id}"


def agent_identity_for_session(bot_session_id: int | str) -> str:
    """Participant identity for the agent worker (manual-mint / rtc path).

    In the LiveKit-Agents framework path the agent's participant token is
    issued by the server when a dispatched job is assigned, so this is only
    used when minting a token by hand (the spike proof, a non-framework
    participant, or a console harness).
    """
    return f"{AGENT_IDENTITY_PREFIX}-{bot_session_id}"


# --- Env var names (the legacy launcher contract this payload mirrors) ------
# Kept identical to app.services.docker_launcher._build_environment so
# SessionJobConfig.from_env() can rebuild the payload from the very same
# environment the meet-worker reads — single-sourcing the contract during the
# migration (Johnny-7we threads this into the dispatch call).
ENV_SESSION_ID = "JOHNNY_SESSION_ID"
ENV_MEETING_CONFIG_ID = "JOHNNY_MEETING_CONFIG_ID"
ENV_CALENDAR_EVENT_ID = "JOHNNY_CALENDAR_EVENT_ID"
ENV_ACCOUNT_ID = "JOHNNY_ACCOUNT_ID"
ENV_MEET_LINK = "JOHNNY_MEET_LINK"
ENV_MODE = "JOHNNY_MODE"
ENV_INSTRUCTIONS = "JOHNNY_INSTRUCTIONS"
ENV_PERSONALITY_PROMPT = "JOHNNY_PERSONALITY_PROMPT"
ENV_CONTEXT = "JOHNNY_CONTEXT"
ENV_CALENDAR_CONTEXT = "JOHNNY_CALENDAR_CONTEXT"
ENV_CALENDAR_ATTACHMENTS = "JOHNNY_CALENDAR_ATTACHMENTS"
ENV_PRIOR_SESSION_CONTEXT = "JOHNNY_PRIOR_SESSION_CONTEXT"
ENV_PROVIDER_CONFIG = "JOHNNY_PROVIDER_CONFIG"
ENV_REDIS_URL = "JOHNNY_REDIS_URL"
# Room name: reuse the existing LiveKitTransport env var so the bridge and the
# agent agree without a new variable (johnny.voice_pipeline.livekit_transport).
ENV_ROOM = "LIVEKIT_ROOM"


def _int_or_none(raw: str | None) -> int | None:
    """Parse an optional integer id from a launcher env string.

    The launcher stringifies ids with ``str(...)``, so a ``None`` id becomes
    the literal ``"None"``; treat any non-digit (incl. empty / ``"None"``) as
    absent rather than raising.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or not s.lstrip("-").isdigit():
        return None
    return int(s)


@dataclass(frozen=True, slots=True)
class SessionJobConfig:
    """Everything the agent worker needs to drive one Meet session.

    Field groups:

    * **correlation / routing** — ``bot_session_id`` (the durable session row,
      also the source of the room name), ``room_name``, ``meet_link`` and the
      optional ``meeting_config_id`` / ``calendar_event_id`` / ``account_id``;
    * **behaviour** — ``mode`` (one of :data:`SUPPORTED_MODES`);
    * **prompt assembly** — ``instructions`` / ``personality_prompt`` /
      ``context`` / ``calendar_context`` / ``calendar_attachments_text`` /
      ``prior_session_context``, the inputs to
      :class:`johnny.agent.session.AgentInstructionsConfig`;
    * **providers** — ``provider_config``, the exact dict shape produced by
      :func:`app.services.provider_payload.build_provider_payload`
      (``{kind: {provider_name, display_name, credentials, options}}``);
    * **infra** — ``redis_url`` for the event-bus / approval-gate wiring.

    All text fields default to ``""`` and ids/redis to ``None`` so an
    under-configured session degrades exactly as the legacy env contract does.
    """

    bot_session_id: int
    room_name: str
    meet_link: str = ""
    meeting_config_id: int | None = None
    calendar_event_id: int | None = None
    account_id: int | None = None
    mode: str = DEFAULT_MODE
    instructions: str = ""
    personality_prompt: str = ""
    context: str = ""
    calendar_context: str = ""
    calendar_attachments_text: str = ""
    prior_session_context: str = ""
    provider_config: Mapping[str, Any] = field(default_factory=dict)
    redis_url: str | None = None

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict (``provider_config`` copied to a plain dict)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "provider_config":
                value = dict(value)
            out[f.name] = value
        return out

    def to_metadata(self) -> str:
        """Serialise to the JSON string carried as dispatch/room metadata.

        ``sort_keys`` makes the output deterministic (stable across processes,
        easy to assert on). This is what
        :func:`johnny.agent.dispatch.dispatch_agent` puts on
        ``CreateAgentDispatchRequest.metadata``.
        """
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionJobConfig:
        """Build from a decoded mapping, validating the enumerated fields.

        Raises :class:`ValueError` on a missing/blank ``bot_session_id`` or
        ``room_name`` (the two fields with no safe default) or an unknown
        ``mode`` — the wire format is validated strictly so a malformed
        dispatch fails loud at the agent rather than silently mis-driving a
        meeting. Unknown keys (e.g. the retired ``pipeline_mode``,
        Johnny-trt.43) are ignored.
        """
        if "bot_session_id" not in data or data["bot_session_id"] in (None, ""):
            raise ValueError("SessionJobConfig requires bot_session_id")
        room_name = str(data.get("room_name") or "").strip()
        if not room_name:
            raise ValueError("SessionJobConfig requires a non-empty room_name")
        mode = str(data.get("mode") or DEFAULT_MODE)
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(SUPPORTED_MODES)}")
        provider_config = data.get("provider_config") or {}
        if not isinstance(provider_config, Mapping):
            raise ValueError("provider_config must be a JSON object")
        return cls(
            bot_session_id=int(data["bot_session_id"]),
            room_name=room_name,
            meet_link=str(data.get("meet_link") or ""),
            meeting_config_id=_coerce_optional_int(data.get("meeting_config_id")),
            calendar_event_id=_coerce_optional_int(data.get("calendar_event_id")),
            account_id=_coerce_optional_int(data.get("account_id")),
            mode=mode,
            instructions=str(data.get("instructions") or ""),
            personality_prompt=str(data.get("personality_prompt") or ""),
            context=str(data.get("context") or ""),
            calendar_context=str(data.get("calendar_context") or ""),
            calendar_attachments_text=str(data.get("calendar_attachments_text") or ""),
            prior_session_context=str(data.get("prior_session_context") or ""),
            provider_config=dict(provider_config),
            redis_url=(str(data["redis_url"]) if data.get("redis_url") else None),
        )

    @classmethod
    def from_metadata(cls, raw: str) -> SessionJobConfig:
        """Parse the dispatch/room metadata JSON string back into a config.

        Raises :class:`ValueError` on malformed JSON or a non-object payload,
        then defers field validation to :meth:`from_dict`.
        """
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid SessionJobConfig metadata JSON: {exc}") from exc
        if not isinstance(data, Mapping):
            raise ValueError("SessionJobConfig metadata must be a JSON object")
        return cls.from_dict(data)

    # -- legacy env bridge ------------------------------------------------

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> SessionJobConfig:
        """Rebuild the payload from the legacy ``JOHNNY_*`` launcher env.

        Single-sources the contract with
        :meth:`app.services.docker_launcher.DockerContainerLauncher.
        _build_environment` so the dispatch path (Johnny-7we) can construct a
        config from the same data the meet-worker already receives. Lenient
        like the launcher: a blank ``JOHNNY_MODE`` becomes ``listen_only``; the
        room name falls back to :func:`room_name_for_session` when
        ``LIVEKIT_ROOM`` is unset.
        """
        session_id = _int_or_none(environ.get(ENV_SESSION_ID))
        if session_id is None:
            raise ValueError(f"{ENV_SESSION_ID} must be a positive integer")
        room_name = (environ.get(ENV_ROOM) or "").strip() or room_name_for_session(session_id)
        provider_config = _parse_provider_config(environ.get(ENV_PROVIDER_CONFIG))
        mode = (environ.get(ENV_MODE) or "").strip() or DEFAULT_MODE
        return cls(
            bot_session_id=session_id,
            room_name=room_name,
            meet_link=environ.get(ENV_MEET_LINK, ""),
            meeting_config_id=_int_or_none(environ.get(ENV_MEETING_CONFIG_ID)),
            calendar_event_id=_int_or_none(environ.get(ENV_CALENDAR_EVENT_ID)),
            account_id=_int_or_none(environ.get(ENV_ACCOUNT_ID)),
            mode=mode,
            instructions=environ.get(ENV_INSTRUCTIONS, ""),
            personality_prompt=environ.get(ENV_PERSONALITY_PROMPT, ""),
            context=environ.get(ENV_CONTEXT, ""),
            calendar_context=environ.get(ENV_CALENDAR_CONTEXT, ""),
            calendar_attachments_text=environ.get(ENV_CALENDAR_ATTACHMENTS, ""),
            prior_session_context=environ.get(ENV_PRIOR_SESSION_CONTEXT, ""),
            provider_config=provider_config,
            redis_url=(environ.get(ENV_REDIS_URL) or None),
        )

    def to_env(self) -> dict[str, str]:
        """Render back to the ``JOHNNY_*`` env mapping (inverse of from_env).

        Useful for tests and for any path that still launches the meet-worker
        via env vars while the migration is in flight. Optional ids render as
        the empty string (the launcher's ``str(... or "")`` shape); ``room_name``
        maps to ``LIVEKIT_ROOM``.
        """
        env = {
            ENV_SESSION_ID: str(self.bot_session_id),
            ENV_ROOM: self.room_name,
            ENV_MEET_LINK: self.meet_link,
            ENV_MEETING_CONFIG_ID: _id_to_env(self.meeting_config_id),
            ENV_CALENDAR_EVENT_ID: _id_to_env(self.calendar_event_id),
            ENV_ACCOUNT_ID: _id_to_env(self.account_id),
            ENV_MODE: self.mode,
            ENV_INSTRUCTIONS: self.instructions,
            ENV_PERSONALITY_PROMPT: self.personality_prompt,
            ENV_CONTEXT: self.context,
            ENV_CALENDAR_CONTEXT: self.calendar_context,
            ENV_CALENDAR_ATTACHMENTS: self.calendar_attachments_text,
            ENV_PRIOR_SESSION_CONTEXT: self.prior_session_context,
            ENV_PROVIDER_CONFIG: json.dumps(dict(self.provider_config)),
        }
        if self.redis_url:
            env[ENV_REDIS_URL] = self.redis_url
        return env


def _coerce_optional_int(value: Any) -> int | None:
    """Coerce a decoded JSON value to an optional int (``None``/blank → None)."""
    if value is None or value == "":
        return None
    return int(value)


def _id_to_env(value: int | None) -> str:
    return "" if value is None else str(value)


def _parse_provider_config(raw: str | None) -> dict[str, Any]:
    """Decode ``JOHNNY_PROVIDER_CONFIG`` JSON; empty/invalid → ``{}``.

    Matches the meet-worker's tolerance: a missing or unparyable provider
    payload degrades to an empty config (listen-only) rather than refusing to
    start.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


__all__ = [
    "AGENT_IDENTITY_PREFIX",
    "APPROVAL_REQUIRED_MODE",
    "AUTONOMOUS_MODE",
    "BRIDGE_IDENTITY_PREFIX",
    "DEFAULT_MODE",
    "LIMITED_AUTO_SPEAK_MODE",
    "LISTEN_ONLY_MODE",
    "ROOM_NAME_PREFIX",
    "SUGGEST_ONLY_MODE",
    "SUPPORTED_MODES",
    "SessionJobConfig",
    "agent_identity_for_session",
    "bridge_identity_for_session",
    "room_name_for_session",
]
