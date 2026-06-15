"""Agent library HTTP endpoints (Johnny-trt.41).

Manages rows in ``agents``: list / create / clone / read / update / delete,
plus ``set-default`` to atomically promote one agent to the single default.
An agent bundles identity (name / avatar / description), the character
prompt, behavior (mode / allowed_replies / confidence_threshold) and the
split-pipeline provider role-slot pins. The session-start surfaces consume
agents via :mod:`app.services.agents`; this module only owns CRUD.

Validation parity with the retired templates/personalities rules:

* ``limited_auto_speak`` requires a non-empty ``allowed_replies`` list (the
  pipeline must always have a safe phrase to pick);
* ``autonomous`` requires a non-empty ``character_prompt`` (free-form
  generation's only governance is the prompt);
* provider FKs are kind-validated — the three LLM role slots must reference
  ``llm`` rows, ``tts_provider_id`` a ``tts`` row (422 otherwise);
* ``tts_voice_id`` requires ``tts_provider_id`` (voice ids are
  provider-specific, a dangling voice pin is meaningless);
* names are unique (409), exactly one default exists at any time (partial
  unique index + the deactivate-siblings-first promote).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_session
from app.db.models import (
    Agent,
    BotMode,
    GoogleAccount,
    MeetingAgent,
    ProviderCredential,
    Workspace,
)
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto

router = APIRouter(prefix="/agents", tags=["agents"])


# (payload field, expected provider kind) for every provider FK an agent
# carries. The three LLM role slots (Johnny-trt.41 note: router = triage,
# answer = conversational replies, reasoning = delegated executor tasks) all
# validate against the ``llm`` kind; resolution order/fallback is trt.42.
_PROVIDER_FK_KINDS: tuple[tuple[str, ProviderKind], ...] = (
    ("router_llm_provider_id", ProviderKind.LLM),
    ("answer_llm_provider_id", ProviderKind.LLM),
    ("reasoning_llm_provider_id", ProviderKind.LLM),
    ("tts_provider_id", ProviderKind.TTS),
)


# --- Pydantic schemas ------------------------------------------------------


class AgentCreate(BaseModel):
    """Payload for creating an agent. Always created non-default."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    avatar: str | None = Field(default=None, max_length=64)
    description: str | None = None
    character_prompt: str = ""
    mode: BotMode = BotMode.LISTEN_ONLY
    allowed_replies: list[str] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # Router-triage timeout + on-timeout fallback (Johnny-xql). ``<= 0`` on the
    # timeout disables the wall-clock bound; retries are clamped to a small
    # ceiling so a turn can never freeze for minutes.
    router_llm_timeout_s: float = Field(default=8.0, ge=0.0, le=120.0)
    router_timeout_retries: int = Field(default=0, ge=0, le=5)
    router_timeout_fallback_mode: Literal["disabled", "static", "llm"] = "static"
    router_timeout_fallback_text: str = Field(
        default="Sorry, I didn't catch that in time — could you say that again?",
        max_length=500,
    )
    # Native tool-loop depth (Johnny-3gx). 0 = unlimited tool calls per turn; a
    # positive value caps the answer agent's native tool loop.
    max_tool_steps: int = Field(default=0, ge=0, le=100)
    router_llm_provider_id: int | None = None
    answer_llm_provider_id: int | None = None
    reasoning_llm_provider_id: int | None = None
    tts_provider_id: int | None = None
    tts_voice_id: str | None = Field(default=None, max_length=128)
    tts_options: dict[str, Any] = Field(default_factory=dict)
    # Workspace attachment (Johnny-wks.1): which execution environment the
    # agent's delegated work runs in. None/omitted = the default workspace.
    workspace_id: int | None = None
    # Meeting-bot identity (Johnny-wks.7): the Google account this agent JOINS
    # meetings as. None/omitted = no agent-level identity (per-meeting
    # resolution unchanged). NOT the workspace and NOT the gog keyring.
    meeting_bot_account_id: int | None = None

    @field_validator("allowed_replies")
    @classmethod
    def _strip_blank_replies(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v and v.strip()]


class AgentUpdate(BaseModel):
    """Patch payload — only fields explicitly present are modified.

    Omit a field to leave it untouched; send ``null`` to clear a nullable
    one. ``is_default`` is intentionally not patchable — use
    ``POST /agents/{id}/set-default`` to promote.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    avatar: str | None = Field(default=None, max_length=64)
    description: str | None = None
    character_prompt: str | None = None
    mode: BotMode | None = None
    allowed_replies: list[str] | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    # Router-triage timeout + on-timeout fallback (Johnny-xql). Non-nullable
    # columns: an explicit ``null`` is popped in ``update_agent`` (parity with
    # confidence_threshold) so only a concrete value ever patches the row.
    router_llm_timeout_s: float | None = Field(default=None, ge=0.0, le=120.0)
    router_timeout_retries: int | None = Field(default=None, ge=0, le=5)
    router_timeout_fallback_mode: Literal["disabled", "static", "llm"] | None = None
    router_timeout_fallback_text: str | None = Field(default=None, max_length=500)
    # Native tool-loop depth (Johnny-3gx). NOT NULL column: an explicit null is
    # popped in update_agent (parity with the router-timeout knobs). 0 = unlimited.
    max_tool_steps: int | None = Field(default=None, ge=0, le=100)
    router_llm_provider_id: int | None = None
    answer_llm_provider_id: int | None = None
    reasoning_llm_provider_id: int | None = None
    tts_provider_id: int | None = None
    tts_voice_id: str | None = Field(default=None, max_length=128)
    tts_options: dict[str, Any] | None = None
    # Send ``null`` to reattach the agent to the default workspace.
    workspace_id: int | None = None
    # Send ``null`` to clear the agent-level meeting-bot identity (Johnny-wks.7)
    # — meetings then resolve the join identity per-assignment / per-meeting.
    meeting_bot_account_id: int | None = None

    @field_validator("allowed_replies")
    @classmethod
    def _strip_blank_replies(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [v.strip() for v in value if v and v.strip()]


class AgentRead(BaseModel):
    """Public view of an agent row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    avatar: str | None
    description: str | None
    character_prompt: str
    mode: BotMode
    allowed_replies: list[str]
    confidence_threshold: float
    router_llm_timeout_s: float
    router_timeout_retries: int
    router_timeout_fallback_mode: str
    router_timeout_fallback_text: str
    max_tool_steps: int
    is_default: bool
    router_llm_provider_id: int | None
    answer_llm_provider_id: int | None
    reasoning_llm_provider_id: int | None
    tts_provider_id: int | None
    tts_voice_id: str | None
    tts_options: dict[str, Any]
    # Workspace attachment (Johnny-wks.1). None = the default workspace
    # (the provider-pin NULL-inherits convention); the snapshot stamped at
    # dispatch always carries the resolved effective workspace.
    workspace_id: int | None
    # Meeting-bot identity (Johnny-wks.7): the Google account this agent joins
    # meetings as. None = no agent-level identity; the join falls back to the
    # per-assignment / per-meeting account (Johnny-trt.45/46).
    meeting_bot_account_id: int | None
    created_at: datetime
    updated_at: datetime
    # How many meetings currently assign this agent (``meeting_agents``
    # rows, enabled or not). The edit/list UI uses it to warn before a
    # delete — removing the agent cascades those assignments away
    # (Johnny-trt.44 acceptance).
    meeting_count: int = 0


# --- Helpers ---------------------------------------------------------------


SessionDep = Annotated[Session, Depends(get_session)]
CryptoDep = Annotated[CredentialCrypto, Depends(get_crypto)]


def _get_row_or_404(session: Session, agent_id: int) -> Agent:
    row = session.get(Agent, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return row


def _meeting_counts(session: Session, agent_ids: list[int]) -> dict[int, int]:
    """``{agent_id: meeting_agents row count}`` for the given agents."""
    if not agent_ids:
        return {}
    rows = session.execute(
        select(MeetingAgent.agent_id, func.count())
        .where(MeetingAgent.agent_id.in_(agent_ids))
        .group_by(MeetingAgent.agent_id)
    ).all()
    return {agent_id: count for agent_id, count in rows}


def _agent_read(session: Session, row: Agent) -> AgentRead:
    read = AgentRead.model_validate(row)
    read.meeting_count = _meeting_counts(session, [row.id]).get(row.id, 0)
    return read


def _validate_provider_fk(
    session: Session,
    provider_id: int,
    expected_kind: ProviderKind,
    *,
    field: str,
) -> None:
    """Reject a provider FK that doesn't exist or is the wrong kind (422).

    An agent may reference an *inactive* provider (the trt.42 resolver falls
    back at session start), so ``is_active`` is deliberately not checked —
    only existence and kind.
    """
    row = session.get(ProviderCredential, provider_id)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"{field}={provider_id} does not reference an existing provider",
        )
    if row.kind is not expected_kind:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field}={provider_id} is a {row.kind.value} provider, "
                f"expected {expected_kind.value}"
            ),
        )


def _validate_provider_fks(session: Session, data: dict[str, Any]) -> None:
    for field, kind in _PROVIDER_FK_KINDS:
        value = data.get(field)
        if value is not None:
            _validate_provider_fk(session, value, kind, field=field)


def _validate_workspace_fk(session: Session, data: dict[str, Any]) -> None:
    """Reject a ``workspace_id`` that names no existing workspace (422).

    ``None`` is always legal — it means the default workspace (Johnny-wks.1).
    """
    value = data.get("workspace_id")
    if value is None:
        return
    if session.get(Workspace, value) is None:
        raise HTTPException(
            status_code=422,
            detail=f"workspace_id={value} does not reference an existing workspace",
        )


def _validate_meeting_bot_account_fk(session: Session, data: dict[str, Any]) -> None:
    """Reject a ``meeting_bot_account_id`` that names no Google account (422).

    ``None`` is always legal — it means no agent-level meeting-bot identity, so
    the join falls back to the per-assignment / per-meeting account
    (Johnny-wks.7). The row's bot-session capability (a ``storage_state.json``
    on disk) is *not* required here: a row can be picked before its session is
    seeded, and a missing session degrades exactly as it always has (the
    meet-worker lands on Google's sign-in screen). ``google_accounts`` rows are
    global and dual-capability, so any existing row is a valid pick.
    """
    value = data.get("meeting_bot_account_id")
    if value is None:
        return
    if session.get(GoogleAccount, value) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"meeting_bot_account_id={value} does not reference an "
                "existing Google account"
            ),
        )


def _validate_behavior(
    *,
    mode: BotMode,
    allowed_replies: list[str],
    character_prompt: str,
    tts_provider_id: int | None,
    tts_voice_id: str | None,
) -> None:
    """Enforce the cross-field behavior invariants on the EFFECTIVE state.

    Called with the post-create / post-patch values so switching ``mode`` on
    an existing agent revalidates against its (possibly unchanged) replies
    and prompt.
    """
    if mode is BotMode.LIMITED_AUTO_SPEAK and not allowed_replies:
        raise HTTPException(
            status_code=422,
            detail=(
                "allowed_replies must be non-empty when mode is "
                "'limited_auto_speak' — the pipeline needs at least one safe "
                "phrase to choose from"
            ),
        )
    if mode is BotMode.AUTONOMOUS and not character_prompt.strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "character_prompt must be non-empty when mode is 'autonomous' "
                "— free-form generation has no allowlist or approval round, "
                "so the prompt is the only governance for what the agent says"
            ),
        )
    if tts_voice_id is not None and tts_voice_id.strip() and tts_provider_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "tts_voice_id requires tts_provider_id — voice ids are "
                "provider-specific"
            ),
        )


def _unique_clone_name(session: Session, original: str) -> str:
    """Return ``"<original> (copy)"``, disambiguated if that already exists."""
    base = f"{original} (copy)"
    candidate = base
    suffix = 2
    existing = set(session.scalars(select(Agent.name)).all())
    while candidate in existing:
        candidate = f"{original} (copy {suffix})"
        suffix += 1
    return candidate


def _name_conflict(name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"an agent named {name!r} already exists. Pick a different name.",
    )


# --- Endpoints -------------------------------------------------------------


@router.get("", response_model=list[AgentRead])
def list_agents(session: SessionDep) -> list[AgentRead]:
    """List every agent, the default first then alphabetical."""
    rows = session.scalars(
        select(Agent).order_by(Agent.is_default.desc(), Agent.name, Agent.id)
    ).all()
    counts = _meeting_counts(session, [row.id for row in rows])
    reads: list[AgentRead] = []
    for row in rows:
        read = AgentRead.model_validate(row)
        read.meeting_count = counts.get(row.id, 0)
        reads.append(read)
    return reads


@router.post("", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentCreate, session: SessionDep) -> AgentRead:
    """Create a new agent. Always created non-default."""
    data = payload.model_dump()
    _validate_provider_fks(session, data)
    _validate_workspace_fk(session, data)
    _validate_meeting_bot_account_fk(session, data)
    _validate_behavior(
        mode=payload.mode,
        allowed_replies=payload.allowed_replies,
        character_prompt=payload.character_prompt,
        tts_provider_id=payload.tts_provider_id,
        tts_voice_id=payload.tts_voice_id,
    )

    row = Agent(**data, is_default=False)
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _name_conflict(payload.name) from exc
    session.refresh(row)
    return _agent_read(session, row)


@router.post(
    "/{agent_id}/clone",
    response_model=AgentRead,
    status_code=status.HTTP_201_CREATED,
)
def clone_agent(agent_id: int, session: SessionDep) -> AgentRead:
    """Duplicate an agent as ``"<original> (copy)"``, non-default."""
    src = _get_row_or_404(session, agent_id)
    row = Agent(
        name=_unique_clone_name(session, src.name),
        avatar=src.avatar,
        description=src.description,
        character_prompt=src.character_prompt,
        mode=src.mode,
        allowed_replies=list(src.allowed_replies or []),
        confidence_threshold=src.confidence_threshold,
        router_llm_provider_id=src.router_llm_provider_id,
        answer_llm_provider_id=src.answer_llm_provider_id,
        reasoning_llm_provider_id=src.reasoning_llm_provider_id,
        tts_provider_id=src.tts_provider_id,
        tts_voice_id=src.tts_voice_id,
        tts_options=dict(src.tts_options or {}),
        workspace_id=src.workspace_id,
        meeting_bot_account_id=src.meeting_bot_account_id,
        is_default=False,
    )
    session.add(row)
    session.flush()
    session.refresh(row)
    return _agent_read(session, row)


@router.get("/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: int, session: SessionDep) -> AgentRead:
    """Read a single agent."""
    return _agent_read(session, _get_row_or_404(session, agent_id))


@router.patch("/{agent_id}", response_model=AgentRead)
def update_agent(
    agent_id: int,
    payload: AgentUpdate,
    session: SessionDep,
) -> AgentRead:
    """Patch an agent. Omitted fields are unchanged.

    Behavior invariants are validated against the EFFECTIVE post-patch
    state, so e.g. flipping ``mode`` to ``limited_auto_speak`` on an agent
    with no allowed replies is rejected even though the patch itself never
    mentions ``allowed_replies``.
    """
    row = _get_row_or_404(session, agent_id)
    data = payload.model_dump(exclude_unset=True)

    # Explicit ``null`` for the NOT NULL columns means "reset to empty" —
    # normalise before validation/assignment so it can't reach the DB as
    # NULL. A ``null`` mode is meaningless and treated as "unchanged".
    if data.get("allowed_replies") is None and "allowed_replies" in data:
        data["allowed_replies"] = []
    if data.get("character_prompt") is None and "character_prompt" in data:
        data["character_prompt"] = ""
    if data.get("tts_options") is None and "tts_options" in data:
        data["tts_options"] = {}
    if data.get("mode") is None:
        data.pop("mode", None)
    if data.get("confidence_threshold") is None:
        data.pop("confidence_threshold", None)
    # NOT NULL behavior columns (Johnny-xql router timeout + Johnny-3gx
    # max_tool_steps): an explicit null means "unchanged", same as
    # confidence_threshold / mode above.
    for _nonnull_key in (
        "router_llm_timeout_s",
        "router_timeout_retries",
        "router_timeout_fallback_mode",
        "router_timeout_fallback_text",
        "max_tool_steps",
    ):
        if data.get(_nonnull_key) is None:
            data.pop(_nonnull_key, None)
    if data.get("name") is None:
        data.pop("name", None)

    _validate_provider_fks(session, data)
    _validate_workspace_fk(session, data)
    _validate_meeting_bot_account_fk(session, data)

    effective: dict[str, Any] = {
        "mode": row.mode,
        "allowed_replies": list(row.allowed_replies or []),
        "character_prompt": row.character_prompt or "",
        "tts_provider_id": row.tts_provider_id,
        "tts_voice_id": row.tts_voice_id,
        **data,
    }
    _validate_behavior(
        mode=effective["mode"],
        allowed_replies=effective["allowed_replies"],
        character_prompt=effective["character_prompt"] or "",
        tts_provider_id=effective["tts_provider_id"],
        tts_voice_id=effective["tts_voice_id"],
    )

    for key, value in data.items():
        setattr(row, key, value)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _name_conflict(row.name) from exc
    session.refresh(row)
    return _agent_read(session, row)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: int, session: SessionDep) -> None:
    """Delete an agent. Refuses the default (promote another first)."""
    row = _get_row_or_404(session, agent_id)
    if row.is_default:
        raise HTTPException(
            status_code=409,
            detail=(
                "cannot delete the default agent; set another agent as "
                "default first"
            ),
        )
    session.delete(row)


@router.post(
    "/{agent_id}/test_voice",
    responses={
        200: {"content": {"audio/wav": {}}},
        404: {"description": "Agent not found"},
        409: {"description": "The agent's saved TTS provider/voice is unusable"},
        502: {"description": "Synthesis failed"},
    },
)
async def test_agent_voice(
    agent_id: int,
    session: SessionDep,
    crypto: CryptoDep,
) -> Response:
    """Synthesize a sample with the agent's EXACT saved provider + voice (Johnny-trt.42).

    The per-agent twin of ``POST /providers/{id}/play_sample``: resolves the
    agent's ``tts_provider_id`` pin (any existing TTS row — pins reference
    inactive rows by design, since only one row per kind can be globally
    active) and applies the agent's ``tts_voice_id`` / ``tts_options`` for
    this one synth call, returning a self-contained 16 kHz mono WAV.

    An agent with no TTS pin tests the global-active TTS row with its own
    saved voice — what an unpinned session would actually speak. Unlike
    session start (which falls back with a warning so a meeting always
    proceeds), an unusable PIN here is a 409: the edit page should see the
    broken state, not a sample synthesized with some other provider. For
    previewing an *unsaved* picker selection the edit page keeps using
    ``play_sample`` with its ``voice_id`` override.
    """
    # Lazy: the providers module imports every provider adapter; keep this
    # module import-light for the CRUD-only callers (and the sample helpers
    # are deliberately single-sourced there, not duplicated here).
    from app.api.providers import TTS_SAMPLE_PHRASE, _pcm_to_wav_bytes, _tts_sample_headers
    from app.providers.audio_assert import check_audible, measure_pcm16
    from app.providers.base import (
        ProviderConfig,
        TTSProvider,
        UnknownProviderError,
        get_registry,
    )
    from app.security.crypto import CryptoError, decrypt_json

    agent = _get_row_or_404(session, agent_id)

    pin_id = agent.tts_provider_id
    pinned = pin_id is not None
    if pin_id is not None:
        row = session.get(ProviderCredential, pin_id)
        if row is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"agent {agent.name!r} pins TTS provider "
                    f"id={pin_id}, which no longer exists — "
                    "sessions fall back to the global-active TTS; re-pin a "
                    "provider to test the exact voice"
                ),
            )
        if row.kind is not ProviderKind.TTS:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"agent {agent.name!r} pins provider id={row.id} of kind "
                    f"{row.kind.value}, not tts — re-pin a TTS provider"
                ),
            )
    else:
        row = session.scalar(
            select(ProviderCredential).where(
                ProviderCredential.kind == ProviderKind.TTS,
                ProviderCredential.is_active.is_(True),
            )
        )
        if row is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"agent {agent.name!r} pins no TTS provider and no global "
                    "TTS provider is active — nothing to synthesize with"
                ),
            )

    registry = get_registry()
    if not registry.has(row.kind, row.provider_name):
        raise HTTPException(
            status_code=502,
            detail=f"no factory registered for tts:{row.provider_name}",
        )
    try:
        creds = decrypt_json(crypto, row.credentials_encrypted)
    except (CryptoError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"failed to decrypt credentials: {exc}",
        ) from exc

    # The exact option merge the session resolver applies (Johnny-trt.42):
    # the agent's tts_options over the row config, the agent voice last.
    # Voice/options apply only on the agent's own pin — on the unpinned
    # global row they'd name another provider's voice (CRUD enforces
    # voice ⇒ pin, so there is nothing to apply anyway).
    options = dict(row.config or {})
    if pinned:
        options.update(dict(agent.tts_options or {}))
        if agent.tts_voice_id and agent.tts_voice_id.strip():
            options["voice_id"] = agent.tts_voice_id.strip()

    config = ProviderConfig(
        kind=row.kind,
        provider_name=row.provider_name,
        display_name=row.display_name,
        credentials=creds,
        options=options,
    )
    try:
        instance = registry.instantiate(config)
    except UnknownProviderError as exc:
        raise HTTPException(
            status_code=502, detail=f"provider factory missing: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — surface any factory error
        raise HTTPException(
            status_code=502, detail=f"provider construction failed: {exc}"
        ) from exc
    if not isinstance(instance, TTSProvider):
        raise HTTPException(
            status_code=502,
            detail=(
                f"tts:{row.provider_name} did not build a TTSProvider "
                "(registry misconfiguration)"
            ),
        )

    start = time.perf_counter()
    ttfa_ms = -1
    try:
        chunks: list[bytes] = []
        async for frame in instance.synthesize_stream(TTS_SAMPLE_PHRASE):
            if ttfa_ms < 0:
                ttfa_ms = int((time.perf_counter() - start) * 1000)
            chunks.append(frame)
        pcm = b"".join(chunks)
    except Exception as exc:  # noqa: BLE001 — surface any synth error
        raise HTTPException(status_code=502, detail=f"synthesis failed: {exc}") from exc
    finally:
        try:
            await instance.close()
        except Exception:  # noqa: BLE001, S110 — cleanup best-effort
            pass
    total_ms = int((time.perf_counter() - start) * 1000)

    if not pcm:
        raise HTTPException(status_code=502, detail="synthesis produced no audio")

    metrics = measure_pcm16(pcm)
    reasons = check_audible(metrics, TTS_SAMPLE_PHRASE)
    headers = _tts_sample_headers(
        instance, ttfa_ms, total_ms, f"agent-{agent.id}-voice.wav", metrics, reasons
    )
    headers["X-TTS-Provider"] = row.display_name
    headers["X-TTS-Voice"] = str(options.get("voice_id") or "")
    return Response(
        content=_pcm_to_wav_bytes(pcm),
        media_type="audio/wav",
        headers=headers,
    )


@router.post("/{agent_id}/set-default", response_model=AgentRead)
def set_default_agent(agent_id: int, session: SessionDep) -> AgentRead:
    """Promote this agent to the single default, atomically.

    Deactivates every other row first so the partial unique index on
    ``(is_default) WHERE is_default`` is never violated, then flips this
    row on — mirrors ``providers.activate_provider``.
    """
    row = _get_row_or_404(session, agent_id)
    session.execute(
        update(Agent).where(Agent.id != row.id).values(is_default=False)
    )
    row.is_default = True
    session.flush()
    session.refresh(row)
    return _agent_read(session, row)


__all__ = ["AgentCreate", "AgentRead", "AgentUpdate", "router"]
