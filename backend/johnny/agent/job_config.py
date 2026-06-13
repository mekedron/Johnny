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
from dataclasses import dataclass, field, fields, replace
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
# Mirrors johnny.voice_pipeline.reasoning.DEFAULT_CONFIDENCE_THRESHOLD (the
# router gate's own default) — re-declared to keep this module
# dependency-free; the drift guard test pins the two together.
DEFAULT_CONFIDENCE_THRESHOLD = 0.7

# --- Provider-config role keys (Johnny-trt.42) -------------------------------
# ``provider_config`` is keyed by provider kind (``stt`` / ``llm`` / ``tts``,
# the shape :func:`app.services.provider_payload.build_provider_payload`
# produces). Per-agent role resolution adds two OPTIONAL keys on top:
#
# * ``router_llm`` — the triage-stage LLM entry (same instantiable shape as
#   ``llm``). Present only when the resolved router provider differs from the
#   answer entry under ``llm``; absent → the session reuses the ``llm`` entry
#   (and the same live instance) for both stages, exactly the pre-trt.42
#   behavior.
# * ``reasoning_llm`` — a **credential-less descriptor**
#   (``{provider_id, provider_name, display_name, model}``) naming the LLM
#   delegated tasks should reason with. It is NOT instantiable (no
#   ``credentials``); its only consumer is the ``agent_tasks`` row stamp at
#   delegation time (the worker executor resolves the real provider from the
#   DB when multi-step kinds land). Never build a ``ProviderConfig`` from it.
PROVIDER_CONFIG_ROUTER_LLM_KEY = "router_llm"
PROVIDER_CONFIG_REASONING_LLM_KEY = "reasoning_llm"


def reasoning_llm_from_provider_config(
    provider_config: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The sanitized reasoning-LLM descriptor from a job payload, or ``None``.

    Reads the optional :data:`PROVIDER_CONFIG_REASONING_LLM_KEY` entry and
    re-sanitizes it defensively: only the identity fields survive
    (``provider_id`` / ``provider_name`` / ``display_name`` / ``model``), so a
    malformed payload that smuggled credentials under this key can never leak
    them into the ``agent_tasks`` row stamp. ``None`` when the key is absent,
    not a mapping, or names no provider.
    """
    entry = provider_config.get(PROVIDER_CONFIG_REASONING_LLM_KEY)
    if not isinstance(entry, Mapping):
        return None
    provider_name = str(entry.get("provider_name") or "").strip()
    if not provider_name:
        return None
    descriptor: dict[str, Any] = {"provider_name": provider_name}
    provider_id = entry.get("provider_id")
    if isinstance(provider_id, int):
        descriptor["provider_id"] = provider_id
    display_name = str(entry.get("display_name") or "").strip()
    if display_name:
        descriptor["display_name"] = display_name
    model = entry.get("model")
    if isinstance(model, str) and model:
        descriptor["model"] = model
    return descriptor


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
#
# Johnny-trt.45: the six per-field behavior overrides (JOHNNY_MODE /
# JOHNNY_INSTRUCTIONS / JOHNNY_CHARACTER_PROMPT / JOHNNY_CONTEXT /
# JOHNNY_ALLOWED_REPLIES / JOHNNY_CONFIDENCE_THRESHOLD) were retired —
# behavior now rides the frozen agent snapshot as one JSON env var
# (JOHNNY_AGENT_SNAPSHOT, mirroring ``bot_sessions.agent_snapshot``).
# Calendar/cross-session context stays per-field: it is per-MEETING, not
# per-agent, so it does not belong inside the agent snapshot.
ENV_SESSION_ID = "JOHNNY_SESSION_ID"
ENV_MEETING_CONFIG_ID = "JOHNNY_MEETING_CONFIG_ID"
ENV_CALENDAR_EVENT_ID = "JOHNNY_CALENDAR_EVENT_ID"
ENV_ACCOUNT_ID = "JOHNNY_ACCOUNT_ID"
ENV_MEET_LINK = "JOHNNY_MEET_LINK"
ENV_AGENT_ID = "JOHNNY_AGENT_ID"
ENV_AGENT_SNAPSHOT = "JOHNNY_AGENT_SNAPSHOT"
ENV_CALENDAR_CONTEXT = "JOHNNY_CALENDAR_CONTEXT"
ENV_CALENDAR_ATTACHMENTS = "JOHNNY_CALENDAR_ATTACHMENTS"
ENV_PRIOR_SESSION_CONTEXT = "JOHNNY_PRIOR_SESSION_CONTEXT"
ENV_PROVIDER_CONFIG = "JOHNNY_PROVIDER_CONFIG"
ENV_REDIS_URL = "JOHNNY_REDIS_URL"
# Room name: reuse the existing LiveKitTransport env var so the bridge and the
# agent agree without a new variable (johnny.voice_pipeline.livekit_transport).
ENV_ROOM = "LIVEKIT_ROOM"

# --- Agent-snapshot keys (Johnny-trt.45) -------------------------------------
# The behavior keys this contract reads from the frozen agent snapshot
# (:func:`app.services.agents.build_agent_snapshot` is the producer). The
# snapshot may carry more (identity, provider pins) — the extra keys ride
# along untouched for downstream consumers (e.g. history rendering).
SNAPSHOT_MODE_KEY = "mode"
SNAPSHOT_CHARACTER_PROMPT_KEY = "character_prompt"
SNAPSHOT_ALLOWED_REPLIES_KEY = "allowed_replies"
SNAPSHOT_CONFIDENCE_THRESHOLD_KEY = "confidence_threshold"
SNAPSHOT_ASSIGNMENT_CONTEXT_KEY = "assignment_context"
SNAPSHOT_PEER_NAMES_KEY = "peer_names"
SNAPSHOT_CAPABILITY_POLICY_KEY = "capability_policy"
# Workspace attachment (Johnny-wks.1): the agent's effective execution
# environment, resolved + stamped at dispatch. ``workspace_id`` is the key
# the sandbox resolver seams consume (trt.63 / trt.24) — a present id routes
# to that workspace's OWN container endpoint, the default (id 1) included
# (Johnny-etu.5: lazy-launched like finance/ops); only a stampless snapshot
# (no ``workspace_id``) falls back to the global skills-sandbox. ``workspace``
# is the identity object ({id, name, slug, is_default}); ``is_default`` still
# keys the policy/MCP DB-row resolution, not the sandbox routing.
SNAPSHOT_WORKSPACE_ID_KEY = "workspace_id"
SNAPSHOT_WORKSPACE_KEY = "workspace"


def workspace_from_agent_snapshot(
    agent_snapshot: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The sanitized workspace stamp from a frozen agent snapshot, or ``None``.

    Defensive re-sanitization like
    :func:`reasoning_llm_from_provider_config`: only the known identity
    fields survive (``id`` / ``name`` / ``slug`` / ``is_default``). ``None``
    when the snapshot carries no usable workspace info (legacy snapshots,
    fixtures) — consumers then degrade to the default-workspace behavior.
    The task sink stamps this into each queued ``agent_tasks`` row so the
    worker executor resolves the SAME workspace the session promised.
    """
    raw_id: Any = agent_snapshot.get(SNAPSHOT_WORKSPACE_ID_KEY)
    entry = agent_snapshot.get(SNAPSHOT_WORKSPACE_KEY)
    if not isinstance(entry, Mapping):
        entry = {}
    if raw_id is None:
        raw_id = entry.get("id")
    try:
        workspace_id = int(raw_id) if raw_id is not None and raw_id != "" else None
    except (TypeError, ValueError):
        workspace_id = None
    if workspace_id is None:
        return None
    payload: dict[str, Any] = {
        "id": workspace_id,
        "is_default": bool(entry.get("is_default")),
    }
    name = str(entry.get("name") or "").strip()
    if name:
        payload["name"] = name
    slug = str(entry.get("slug") or "").strip()
    if slug:
        payload["slug"] = slug
    return payload


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
    * **agent** (Johnny-trt.45) — ``agent_id`` plus ``agent_snapshot``, the
      frozen behavior blob (:func:`app.services.agents.build_agent_snapshot`)
      persisted on ``bot_sessions.agent_snapshot`` at dispatch. The behavior
      knobs the runtime reads (:attr:`mode`, :attr:`character_prompt`,
      :attr:`context`, :attr:`allowed_replies`, :attr:`confidence_threshold`)
      are derived **from the snapshot** — there is no separate per-field
      override channel anymore, so a session can never run a mode its
      snapshot doesn't carry;
    * **meeting context** — ``calendar_context`` /
      ``calendar_attachments_text`` / ``prior_session_context``: per-meeting
      (not per-agent) prompt inputs, kept as plain fields;
    * **providers** — ``provider_config``, the exact dict shape produced by
      :func:`app.services.provider_payload.build_provider_payload`
      (``{kind: {provider_name, display_name, credentials, options}}``),
      with the agent's pins already resolved in (Johnny-trt.42);
    * **infra** — ``redis_url`` for the event-bus / approval-gate wiring.

    An empty ``agent_snapshot`` degrades to the contract defaults
    (``listen_only``, no character, threshold 0.7) — exactly the agent-less
    degrade the selection chain documents (Johnny-trt.41).
    """

    bot_session_id: int
    room_name: str
    meet_link: str = ""
    meeting_config_id: int | None = None
    calendar_event_id: int | None = None
    account_id: int | None = None
    agent_id: int | None = None
    agent_snapshot: Mapping[str, Any] = field(default_factory=dict)
    calendar_context: str = ""
    calendar_attachments_text: str = ""
    prior_session_context: str = ""
    provider_config: Mapping[str, Any] = field(default_factory=dict)
    redis_url: str | None = None

    # -- snapshot-derived behavior (Johnny-trt.45) --------------------------

    @property
    def mode(self) -> str:
        """The session mode from the agent snapshot.

        Lenient at read time: blank / missing / unknown values degrade to
        :data:`DEFAULT_MODE` (``listen_only``) so a hand-built or corrupt
        snapshot mutes the bot rather than crashing the session. The wire
        parser (:meth:`from_dict`) is strict instead — a dispatched payload
        with an unknown snapshot mode fails loud at the worker.
        """
        raw = str(self.agent_snapshot.get(SNAPSHOT_MODE_KEY) or "").strip()
        return raw if raw in SUPPORTED_MODES else DEFAULT_MODE

    @property
    def character_prompt(self) -> str:
        """The agent's character prompt (empty when no agent resolved)."""
        return str(self.agent_snapshot.get(SNAPSHOT_CHARACTER_PROMPT_KEY) or "")

    @property
    def context(self) -> str:
        """The per-assignment brief (``assignment_context`` in the snapshot).

        One free-text slot per assignment (Johnny-trt.45): the meeting's
        :class:`MeetingAgent.context` for scheduled sessions, the per-start
        context field for playground sessions.
        """
        return str(self.agent_snapshot.get(SNAPSHOT_ASSIGNMENT_CONTEXT_KEY) or "")

    @property
    def allowed_replies(self) -> tuple[str, ...]:
        """The limited-auto-speak allowlist from the snapshot (lenient)."""
        return _coerce_replies(self.agent_snapshot.get(SNAPSHOT_ALLOWED_REPLIES_KEY))

    @property
    def confidence_threshold(self) -> float:
        """The router speak floor from the snapshot (lenient, clamped)."""
        return _coerce_threshold(
            self.agent_snapshot.get(SNAPSHOT_CONFIDENCE_THRESHOLD_KEY)
        )

    @property
    def peer_names(self) -> tuple[str, ...]:
        """Co-agent display names serving the same meeting/group (Johnny-trt.47).

        Stamped into the snapshot at launch by the surfaces that know the
        roster (the per-assignment scheduler, the playground group start);
        empty everywhere else — single-agent sessions render no peer block.
        Lenient like the other snapshot reads: non-list / non-string shapes
        degrade to absent.
        """
        return _coerce_replies(self.agent_snapshot.get(SNAPSHOT_PEER_NAMES_KEY))

    @property
    def workspace_id(self) -> int | None:
        """The agent's workspace attachment from the snapshot (Johnny-wks.1).

        The key the sandbox resolver seams consume. Lenient like the other
        snapshot reads: absent / blank / unparseable degrades to ``None`` —
        a legacy snapshot, which downstream treats as the default workspace
        (the global skills-sandbox, byte-identical pre-workspaces behavior).
        """
        raw = self.agent_snapshot.get(SNAPSHOT_WORKSPACE_ID_KEY)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @property
    def workspace_is_default(self) -> bool:
        """Whether the stamped workspace is the seeded default (Johnny-wks.1).

        ``True`` for snapshots with no workspace info at all (legacy — they
        predate workspaces and ran the shared sandbox). With a stamped id,
        the verdict comes from the ``workspace`` identity object; a stamped
        id with no object is honoured as a non-default key (the producer
        always writes both, so this shape only arises hand-built).
        """
        if self.workspace_id is None:
            return True
        entry = self.agent_snapshot.get(SNAPSHOT_WORKSPACE_KEY)
        if isinstance(entry, Mapping):
            return bool(entry.get("is_default"))
        return False

    @property
    def workspace_slug(self) -> str | None:
        """The stamped workspace's frozen slug (Johnny-wks.3), or ``None``.

        The key the per-workspace SKILLS-DIR resolver consumes (the
        discovery twin of the sandbox-URL seam): a non-default workspace's
        skill packages live under ``~/.johnny/workspaces/<slug>/skills``.
        Lenient like the other snapshot reads — absent / blank / non-string
        degrades to ``None`` (the resolver then promises no workspace-local
        skills rather than guessing a directory).
        """
        entry = self.agent_snapshot.get(SNAPSHOT_WORKSPACE_KEY)
        if not isinstance(entry, Mapping):
            return None
        slug = str(entry.get("slug") or "").strip()
        return slug or None

    def capability_policy(self) -> Any:
        """The resolved capability policy stamped at dispatch (Johnny-trt.38).

        Returns a
        :class:`johnny.skills.capability_policy.ResolvedCapabilityPolicy` —
        rebuilt from the snapshot payload, NEVER from the policy tables (the
        trt.41 no-turn-time-DB-reads rule). Lenient like the other snapshot
        reads: a missing/malformed payload degrades to the unrestricted
        policy (legacy snapshots keep their pre-trt.38 behavior). A method
        rather than a property so the (import-cheap, but not free) policy
        module loads only on delegation-capable assemblies.
        """
        from johnny.skills.capability_policy import ResolvedCapabilityPolicy

        return ResolvedCapabilityPolicy.from_payload(
            self.agent_snapshot.get(SNAPSHOT_CAPABILITY_POLICY_KEY)
        )

    def with_mode(self, mode: str) -> SessionJobConfig:
        """A copy whose snapshot carries ``mode`` — the runtime degrade seam.

        Replaces the pre-trt.45 ``dataclasses.replace(config, mode=...)`` the
        no-TTS degrade used: mode lives inside the snapshot now, so the
        degrade rewrites the snapshot copy (the durable row snapshot is
        untouched — this is the in-process effective mode only).
        """
        snapshot = {**dict(self.agent_snapshot), SNAPSHOT_MODE_KEY: mode}
        return replace(self, agent_snapshot=snapshot)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict (mapping fields copied to plain dicts)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in ("provider_config", "agent_snapshot"):
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
        ``room_name`` (the two fields with no safe default), a non-object
        ``agent_snapshot``, or a snapshot carrying an unknown ``mode`` — the
        wire format is validated strictly so a malformed dispatch fails loud
        at the agent rather than silently mis-driving a meeting. Unknown
        keys — including the retired per-field behavior overrides (``mode``
        / ``instructions`` / ``character_prompt`` / ``context`` /
        ``allowed_replies`` / ``confidence_threshold``, Johnny-trt.45) and
        ``pipeline_mode`` (Johnny-trt.43) — are ignored, so an old in-flight
        dispatch payload still parses (degrading to the snapshot defaults).
        """
        if "bot_session_id" not in data or data["bot_session_id"] in (None, ""):
            raise ValueError("SessionJobConfig requires bot_session_id")
        room_name = str(data.get("room_name") or "").strip()
        if not room_name:
            raise ValueError("SessionJobConfig requires a non-empty room_name")
        agent_snapshot = data.get("agent_snapshot") or {}
        if not isinstance(agent_snapshot, Mapping):
            raise ValueError("agent_snapshot must be a JSON object")
        snapshot_mode = str(agent_snapshot.get(SNAPSHOT_MODE_KEY) or "").strip()
        if snapshot_mode and snapshot_mode not in SUPPORTED_MODES:
            raise ValueError(
                f"unknown agent_snapshot mode {snapshot_mode!r}; "
                f"expected one of {sorted(SUPPORTED_MODES)}"
            )
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
            agent_id=_coerce_optional_int(data.get("agent_id")),
            agent_snapshot=dict(agent_snapshot),
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
        like the launcher: a malformed ``JOHNNY_AGENT_SNAPSHOT`` degrades to
        an empty snapshot (listen-only contract defaults); the room name
        falls back to :func:`room_name_for_session` when ``LIVEKIT_ROOM`` is
        unset.
        """
        session_id = _int_or_none(environ.get(ENV_SESSION_ID))
        if session_id is None:
            raise ValueError(f"{ENV_SESSION_ID} must be a positive integer")
        room_name = (environ.get(ENV_ROOM) or "").strip() or room_name_for_session(session_id)
        provider_config = _parse_provider_config(environ.get(ENV_PROVIDER_CONFIG))
        return cls(
            bot_session_id=session_id,
            room_name=room_name,
            meet_link=environ.get(ENV_MEET_LINK, ""),
            meeting_config_id=_int_or_none(environ.get(ENV_MEETING_CONFIG_ID)),
            calendar_event_id=_int_or_none(environ.get(ENV_CALENDAR_EVENT_ID)),
            account_id=_int_or_none(environ.get(ENV_ACCOUNT_ID)),
            agent_id=_int_or_none(environ.get(ENV_AGENT_ID)),
            agent_snapshot=_parse_json_object(environ.get(ENV_AGENT_SNAPSHOT)),
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
            ENV_AGENT_ID: _id_to_env(self.agent_id),
            ENV_AGENT_SNAPSHOT: json.dumps(dict(self.agent_snapshot)),
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


def _coerce_replies(value: Any) -> tuple[str, ...]:
    """Coerce a decoded JSON value to the allowed-replies tuple.

    Lenient like the rest of the optional fields: ``None`` / non-list →
    empty (the no-allowlist default); items are stringified and blanks
    dropped so a sloppy payload degrades instead of failing the dispatch.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _coerce_threshold(value: Any) -> float:
    """Coerce a decoded JSON / env value to the confidence threshold.

    ``None`` / blank / unparseable degrade to
    :data:`DEFAULT_CONFIDENCE_THRESHOLD`; parsed values are clamped into
    ``[0.0, 1.0]`` (defensive against a corrupt snapshot)."""
    if value is None or value == "":
        return DEFAULT_CONFIDENCE_THRESHOLD
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE_THRESHOLD
    return max(0.0, min(1.0, threshold))


def _id_to_env(value: int | None) -> str:
    return "" if value is None else str(value)


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    """Decode a JSON-object env value; empty/invalid → ``{}``.

    Matches the meet-worker's tolerance: a missing or unparseable
    ``JOHNNY_PROVIDER_CONFIG`` / ``JOHNNY_AGENT_SNAPSHOT`` degrades to an
    empty mapping (listen-only contract defaults) rather than refusing to
    start.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


# Backwards-compatible alias — provider parsing predates the snapshot var.
_parse_provider_config = _parse_json_object


__all__ = [
    "AGENT_IDENTITY_PREFIX",
    "APPROVAL_REQUIRED_MODE",
    "AUTONOMOUS_MODE",
    "BRIDGE_IDENTITY_PREFIX",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODE",
    "LIMITED_AUTO_SPEAK_MODE",
    "LISTEN_ONLY_MODE",
    "PROVIDER_CONFIG_REASONING_LLM_KEY",
    "PROVIDER_CONFIG_ROUTER_LLM_KEY",
    "ROOM_NAME_PREFIX",
    "SNAPSHOT_ALLOWED_REPLIES_KEY",
    "SNAPSHOT_ASSIGNMENT_CONTEXT_KEY",
    "SNAPSHOT_CHARACTER_PROMPT_KEY",
    "SNAPSHOT_CONFIDENCE_THRESHOLD_KEY",
    "SNAPSHOT_MODE_KEY",
    "SNAPSHOT_PEER_NAMES_KEY",
    "SNAPSHOT_WORKSPACE_ID_KEY",
    "SNAPSHOT_WORKSPACE_KEY",
    "SUGGEST_ONLY_MODE",
    "SUPPORTED_MODES",
    "SessionJobConfig",
    "agent_identity_for_session",
    "bridge_identity_for_session",
    "reasoning_llm_from_provider_config",
    "room_name_for_session",
    "workspace_from_agent_snapshot",
]
