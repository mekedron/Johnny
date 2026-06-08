"""End-to-end personality matrix (Johnny-oly.7, part A).

The personality library only delivers value if the whole chain holds:

    operator POSTs a personality  →  attaches / selects it  →  a session starts
    →  the resolved provider payload + the snapshot the UI reads reflect it.

The per-sub-task suites prove each layer in isolation
(``tests/api/test_personalities.py`` = CRUD, ``tests/services/
test_personality_resolver.py`` = the resolver, ``tests/api/
test_browser_sessions.py`` = the start endpoint). *This* file wires them
together: it drives the **real** ``/personalities`` operator API and the
**real** ``/sessions/browser/start`` endpoint against one shared DB, with only
the asyncio pipeline runner stubbed (so we never instantiate a live LLM/TTS
adapter or pay model cost — the "faked provider adapters" the bead asks for).
Every assertion is on what actually flows into ``BrowserPipelineSpec`` or the
``playground_overrides`` snapshot the operator's browser renders.

Two premises in the bead text predate the Johnny-oly.1 PRD and were corrected
by the shipped Johnny-oly.3 resolver; this file is faithful to the *code*, not
the stale prose, and documents each correction inline:

* **The personality ``description`` IS injected as the LLM system prompt
  (Johnny-oly.8).** The original .oly.1 PRD deferred persona text to a v2
  field, and the first cut of this suite asserted the description was *absent*
  from the prompt. Johnny-oly.8 reversed that operator-side: the single
  freeform ``description`` is now THE personality's character text, assembled
  by ``build_personality_system_prompt`` into ``[personality: <name>]\n<desc>``
  and carried to the pipeline on ``spec.personality_prompt`` (a distinct
  IDENTITY layer — deliberately NOT folded into ``spec.instructions``, which
  stays the JOB layer). So we assert the description rides on
  ``spec.personality_prompt``; ``tests/voice_pipeline/test_pipeline.py`` proves
  that field reaches the router + answer system messages verbatim.
* **One-active-provider-per-kind** (the ``uq_provider_credentials_active_per_kind``
  partial unique index) means a *valid* personality override can only point at
  the single active row for its kind — i.e. it resolves byte-for-byte to the
  global-active payload. So two distinct providers of one kind cannot be active
  at once; we demonstrate "personality A loads A's providers" by making A's
  providers the active ones, and prove the override *mechanism* via the
  fallback paths (where the pinned row deliberately differs from global active).
  The pure overwrite contract is unit-tested in ``test_personality_resolver``.
* **A deleted provider ``SET NULL``s the FK and is then a SILENT inherit**, not
  a loud fallback. The bead's delete-TTS step says "fallback fires same as
  above"; the resolver logs ``personality.fallback:`` only for a *set-but-
  unusable* FK (missing / deactivated / undecryptable). A ``NULL`` FK is the
  designed inherit path (bootstrap Johnny carries NULL FKs) and stays silent so
  it doesn't drown the real alerts. ``test_deleted_tts_set_null_*`` asserts the
  silent path; ``test_deactivated_llm_*`` covers the loud path.

Resolver fallback-branch coverage (acceptance: "every fallback branch"):

    deactivated   → test_deactivated_llm_falls_back_loudly
    undecryptable → test_undecryptable_llm_falls_back_loudly
    NULL (silent) → test_deleted_tts_set_null_personality_survives_silently
    missing       → unreachable through the operator API (FK enforcement + the
                    CRUD 422 guard + ON DELETE SET NULL forbid a dangling FK);
                    covered deterministically by test_personality_resolver's
                    ``test_apply_missing_fk_falls_back`` (FK-off engine).

Regression matrix (acceptance part D — each closed sub-task gets a cell):

    .oly.2 CRUD / single-default / ON DELETE SET NULL / refuse-delete-default
        → test_create_two_personalities_with_distinct_providers,
          test_clone_then_patch_personality,
          test_default_personality_used_when_no_id (set-default),
          test_deleted_tts_set_null_personality_survives_silently,
          test_delete_default_personality_refused_409
    .oly.3 select precedence + apply + loud/silent fallback
        → test_personality_{a,b}_session_loads_pinned_providers,
          test_default_personality_used_when_no_id (level 3),
          test_meeting_personality_used_without_request (level 2),
          test_deactivated_llm_*, test_undecryptable_llm_*, test_deleted_tts_*
    .oly.4 page CRUD (list/create/clone/edit/delete) — API underpinning
        → test_create_* (create+list), test_clone_then_patch_* (clone+patch),
          test_delete_default_* (delete). The rendered page is part B (browser).
    .oly.6 picker wiring + ``bot_name`` snapshot + meeting personality + badge
        → test_personality_{a,b}_* + test_default_* assert ``bot_name`` and the
          ``personality_{id,name}`` snapshot the badge/history read;
          test_meeting_personality_* covers ``meeting_configs.personality_id``.

(.oly.1 is the design PRD — no runtime surface; it is "tested" by the whole
chain conforming to its §4a precedence + §4b/§4c fallback/mode rules.)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import browser_sessions as browser_sessions_module
from app.api.deps import get_session
from app.db import Base
from app.db.models import (
    AgentDecision,
    AgentUtterance,
    BotMode,
    BotSession,
    CalendarEvent,
    GoogleAccount,
    MeetingConfig,
    Personality,
    PipelineSettings,
    ProfileTemplate,
    ProviderCredential,
    TranscriptChunk,
)
from app.main import app
from app.providers.base import ProviderKind
from app.security.crypto import CredentialCrypto, encrypt_json

# A known key shared by the seeded provider rows and the patched ``get_crypto``
# so credentials decrypt inside both ``build_provider_payload`` and the
# resolver. The spec builders import ``get_crypto`` lazily, so patching the
# source attribute (see ``_patch_crypto``) reaches both.
_CRYPTO = CredentialCrypto(Fernet.generate_key())


# --- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_pipeline() -> Iterator[mock.Mock]:
    """Stub ``_spawn_runner`` so a start never touches the asyncio loop / the
    live provider registry. Yields the mock so a test can read the captured
    ``spec`` off ``spawn.call_args.kwargs['spec']``."""
    with mock.patch.object(browser_sessions_module, "_spawn_runner") as spawn:
        spawn.return_value = mock.Mock(bot_session_id=0)
        yield spawn


@pytest.fixture(autouse=True)
def _patch_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.security.crypto.get_crypto", lambda: _CRYPTO)


@pytest.fixture
def engine() -> sa.Engine:
    """In-memory SQLite with FK enforcement ON.

    FK enforcement is required so ``DELETE provider`` actually fires
    ``ON DELETE SET NULL`` on a personality's FK (SQLite leaves it off by
    default). It also makes the schema reject the pathological dangling-FK that
    only the FK-off resolver unit test can craft — which is exactly why the
    "missing" branch is out of scope here (see module docstring).
    """
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )

    @sa.event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn: object, _record: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(
        bind=eng,
        tables=[
            GoogleAccount.__table__,  # type: ignore[list-item]
            CalendarEvent.__table__,  # type: ignore[list-item]
            ProfileTemplate.__table__,  # type: ignore[list-item]
            ProviderCredential.__table__,  # type: ignore[list-item]
            Personality.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            TranscriptChunk.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
            PipelineSettings.__table__,  # type: ignore[list-item]
        ],
    )
    return eng


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    sess = Session(engine)
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """TestClient on the full ``app.main.app`` so one client drives BOTH the
    ``/personalities`` operator API and ``/sessions/browser/start`` over the
    same shared session — the whole point of an e2e."""

    def _override_session() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- helpers ---------------------------------------------------------------


def _provider(
    db_session: Session,
    *,
    kind: ProviderKind,
    name: str,
    display: str | None = None,
    is_active: bool = False,
    options: dict[str, Any] | None = None,
    credentials: dict[str, Any] | None = None,
    bad_cipher: bool = False,
) -> ProviderCredential:
    """Seed a ``provider_credentials`` row. ``bad_cipher`` writes a token that
    will not decrypt under ``_CRYPTO`` (simulates a rotated FERNET_KEY)."""
    blob = (
        "not-a-valid-fernet-token"
        if bad_cipher
        else encrypt_json(_CRYPTO, credentials or {"api_key": f"sk-{name}"})
    )
    row = ProviderCredential(
        kind=kind,
        provider_name=name,
        display_name=display or name.title(),
        credentials_encrypted=blob,
        config=options or {},
        is_active=is_active,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _create_personality(client: TestClient, **body: Any) -> dict[str, Any]:
    """POST /personalities through the real operator API; assert 201."""
    body.setdefault("display_name", "P")
    res = client.post("/personalities", json=body)
    assert res.status_code == 201, res.text
    data: dict[str, Any] = res.json()
    return data


def _start(client: TestClient, **body: Any) -> Any:
    """POST /sessions/browser/start through the real endpoint."""
    return client.post("/sessions/browser/start", json=body)


def _spec(spawn: mock.Mock) -> Any:
    """The ``BrowserPipelineSpec`` the start handler handed to the (stubbed)
    runner — carries the resolved ``provider_payload``, ``mode``, and
    ``instructions``."""
    return spawn.call_args.kwargs["spec"]


def _seed_meeting(
    db_session: Session, *, mode: BotMode = BotMode.LISTEN_ONLY
) -> tuple[CalendarEvent, MeetingConfig]:
    """Minimal account → event → template → meeting-config chain (FK-ordered so
    it is valid under FK enforcement)."""
    now = datetime.now(UTC).replace(microsecond=0)
    account = GoogleAccount(email="u@example.com", refresh_token_encrypted="x")
    db_session.add(account)
    db_session.flush()
    event = CalendarEvent(
        account_id=account.id,
        external_id="evt-e2e",
        summary="Quarterly planning",
        description="Discuss roadmap",
        start_time=now + timedelta(minutes=5),
        end_time=now + timedelta(minutes=30),
        meet_link="https://meet.google.com/abc-defg-hij",
    )
    db_session.add(event)
    db_session.flush()
    template = ProfileTemplate(
        name="tpl",
        mode=mode,
        base_instructions="Be helpful.",
        base_context="ctx",
        allowed_replies=[],
        confidence_threshold=0.7,
    )
    db_session.add(template)
    db_session.flush()
    cfg = MeetingConfig(
        calendar_event_id=event.id,
        profile_template_id=template.id,
        identity_account_id=account.id,
        mode=mode,
        instructions="Meeting brief.",
        context="ctx",
        enabled=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.refresh(event)
    db_session.refresh(cfg)
    return event, cfg


# --- A. create two personalities with distinct providers -------------------


def test_create_two_personalities_with_distinct_providers(
    client: TestClient, db_session: Session
) -> None:
    """Bead A.1: two personalities, each pinning its own LLM + TTS rows. Drives
    create + list and proves the FKs persist (one active per kind — the
    inactive twins satisfy the active-per-kind unique index)."""
    llm_a = _provider(db_session, kind=ProviderKind.LLM, name="brain-a", is_active=True)
    tts_a = _provider(db_session, kind=ProviderKind.TTS, name="voice-a", is_active=True)
    llm_b = _provider(db_session, kind=ProviderKind.LLM, name="brain-b", is_active=False)
    tts_b = _provider(db_session, kind=ProviderKind.TTS, name="voice-b", is_active=False)

    a = _create_personality(
        client,
        display_name="Alice",
        description="Alice the analyst — precise and terse.",
        llm_provider_id=llm_a.id,
        tts_provider_id=tts_a.id,
    )
    b = _create_personality(
        client,
        display_name="Bob",
        description="Bob the brainstormer — playful and divergent.",
        llm_provider_id=llm_b.id,
        tts_provider_id=tts_b.id,
    )

    assert a["llm_provider_id"] == llm_a.id and a["tts_provider_id"] == tts_a.id
    assert b["llm_provider_id"] == llm_b.id and b["tts_provider_id"] == tts_b.id
    assert a["is_default"] is False and b["is_default"] is False
    assert a["id"] != b["id"]

    listed = client.get("/personalities")
    assert listed.status_code == 200
    names = {row["display_name"] for row in listed.json()}
    assert {"Alice", "Bob"} <= names


# --- A. personality A / B drive the session that loads their providers ------


def test_personality_a_session_loads_pinned_providers_and_snapshot(
    client: TestClient, db_session: Session, _no_real_pipeline: mock.Mock
) -> None:
    """Bead A.2: start a session passing personality A → the resolved
    ``provider_payload`` carries A's (active) LLM + TTS, the snapshot names A,
    ``bot_name`` is A's name, and — per Johnny-oly.8 — A's *description* rides
    to the pipeline as the IDENTITY-layer ``personality_prompt`` (NOT folded
    into the JOB-layer ``instructions``)."""
    llm_a = _provider(db_session, kind=ProviderKind.LLM, name="brain-a", is_active=True)
    tts_a = _provider(db_session, kind=ProviderKind.TTS, name="voice-a", is_active=True)
    a = _create_personality(
        client,
        display_name="Alice",
        description="ALICE-SECRET-PERSONA-TEXT",
        llm_provider_id=llm_a.id,
        tts_provider_id=tts_a.id,
    )

    res = _start(client, personality_id=a["id"])
    assert res.status_code == 201, res.text

    spec = _spec(_no_real_pipeline)
    assert spec.provider_payload["llm"]["provider_name"] == "brain-a"
    assert spec.provider_payload["tts"]["provider_name"] == "voice-a"
    # Johnny-oly.8: the description becomes the IDENTITY-layer persona prompt,
    # wrapped with the preamble, and is kept OUT of the JOB-layer instructions.
    assert spec.personality_prompt == "[personality: Alice]\nALICE-SECRET-PERSONA-TEXT"
    assert "ALICE-SECRET-PERSONA-TEXT" not in (spec.instructions or "")

    ov = res.json()["playground_overrides"]
    assert ov["personality_id"] == a["id"]
    assert ov["personality_name"] == "Alice"
    assert "personality_fallbacks" not in ov  # both FKs usable → no fallback

    # bot_name snapshot (Johnny-oly.6) drives history + the active-session badge.
    row = db_session.get(BotSession, res.json()["id"])
    assert row is not None and row.bot_name == "Alice"


def test_personality_b_session_loads_pinned_providers_and_snapshot(
    client: TestClient, db_session: Session, _no_real_pipeline: mock.Mock
) -> None:
    """Bead A.2 (mirror): personality B, with B's providers active, loads B's
    LLM + TTS and names B in the snapshot."""
    llm_b = _provider(db_session, kind=ProviderKind.LLM, name="brain-b", is_active=True)
    tts_b = _provider(db_session, kind=ProviderKind.TTS, name="voice-b", is_active=True)
    b = _create_personality(
        client,
        display_name="Bob",
        llm_provider_id=llm_b.id,
        tts_provider_id=tts_b.id,
    )

    res = _start(client, personality_id=b["id"])
    assert res.status_code == 201, res.text

    spec = _spec(_no_real_pipeline)
    assert spec.provider_payload["llm"]["provider_name"] == "brain-b"
    assert spec.provider_payload["tts"]["provider_name"] == "voice-b"

    ov = res.json()["playground_overrides"]
    assert ov["personality_id"] == b["id"]
    assert ov["personality_name"] == "Bob"
    assert "personality_fallbacks" not in ov
    assert db_session.get(BotSession, res.json()["id"]).bot_name == "Bob"  # type: ignore[union-attr]


# --- A. default personality selected when no id is passed ------------------


def test_default_personality_used_when_no_id(
    client: TestClient, db_session: Session, _no_real_pipeline: mock.Mock
) -> None:
    """Bead A.3: set B default (POST set-default), start a session with NO
    personality_id → precedence level 3 picks B; payload + snapshot + bot_name
    all reflect B."""
    llm_b = _provider(db_session, kind=ProviderKind.LLM, name="brain-b", is_active=True)
    tts_b = _provider(db_session, kind=ProviderKind.TTS, name="voice-b", is_active=True)
    # An A also exists (non-default) so "default", not "only row", is the reason.
    _create_personality(client, display_name="Alice")
    b = _create_personality(
        client, display_name="Bob", llm_provider_id=llm_b.id, tts_provider_id=tts_b.id
    )

    promoted = client.post(f"/personalities/{b['id']}/set-default")
    assert promoted.status_code == 200 and promoted.json()["is_default"] is True

    res = _start(client)  # no personality_id
    assert res.status_code == 201, res.text
    ov = res.json()["playground_overrides"]
    assert ov["personality_id"] == b["id"]
    assert ov["personality_name"] == "Bob"
    assert _spec(_no_real_pipeline).provider_payload["llm"]["provider_name"] == "brain-b"
    assert db_session.get(BotSession, res.json()["id"]).bot_name == "Bob"  # type: ignore[union-attr]


def test_meeting_personality_used_without_request(
    client: TestClient, db_session: Session, _no_real_pipeline: mock.Mock
) -> None:
    """Precedence level 2 (PRD §4a): a meeting's attached personality is honored
    over the default when the start passes no personality_id. Covers
    ``meeting_configs.personality_id`` (Johnny-oly.3 schema / .6 wiring)."""
    _provider(db_session, kind=ProviderKind.LLM, name="ga", is_active=True)
    event, cfg = _seed_meeting(db_session)
    _create_personality(client, display_name="Default")
    default_row = db_session.scalar(
        sa.select(Personality).where(Personality.display_name == "Default")
    )
    client.post(f"/personalities/{default_row.id}/set-default")  # type: ignore[union-attr]
    meeting_p = _create_personality(client, display_name="MeetingPreset")

    cfg.personality_id = meeting_p["id"]
    db_session.commit()

    res = _start(client, event_id=event.id)  # no personality_id
    assert res.status_code == 201, res.text
    ov = res.json()["playground_overrides"]
    assert ov["personality_id"] == meeting_p["id"]  # the meeting's, not default
    assert ov["personality_name"] == "MeetingPreset"
    # §4c: the meeting's NOT-NULL mode wins; the personality never reseeds it.
    assert _spec(_no_real_pipeline).mode == "listen_only"


# --- A. loud fallback: deactivated provider -------------------------------


def test_deactivated_llm_falls_back_loudly(
    client: TestClient,
    db_session: Session,
    _no_real_pipeline: mock.Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bead A.4: a personality pinned at a DEACTIVATED LLM still starts the
    session, falls back to the global-active LLM, records the fallback in the
    snapshot the UI reads, AND emits the filterable ``personality.fallback:``
    log line."""
    ga = _provider(db_session, kind=ProviderKind.LLM, name="ga", is_active=True)
    dormant = _provider(
        db_session, kind=ProviderKind.LLM, name="dormant", is_active=False
    )
    b = _create_personality(
        client, display_name="Bob", llm_provider_id=dormant.id
    )

    with caplog.at_level(logging.WARNING):
        res = _start(client, personality_id=b["id"])
    assert res.status_code == 201, res.text

    # global default used (UI/runtime), not the dormant pin.
    assert _spec(_no_real_pipeline).provider_payload["llm"]["provider_name"] == "ga"
    assert ga.id != dormant.id
    # the session UI gets the fallback-warning payload.
    ov = res.json()["playground_overrides"]
    assert ov["personality_fallbacks"] == [{"kind": "llm", "reason": "deactivated"}]
    # the structured log fires (so docker logs can be filtered for it).
    assert "personality.fallback:" in caplog.text
    assert "reason=deactivated" in caplog.text


def test_undecryptable_llm_falls_back_loudly(
    client: TestClient,
    db_session: Session,
    _no_real_pipeline: mock.Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Resolver branch ``undecryptable`` (rotated FERNET_KEY): the pinned LLM is
    active but its ciphertext won't decrypt → loud fallback. With no decryptable
    LLM anywhere, the llm channel is simply absent (build_provider_payload skips
    it too) — the session still starts on its TTS."""
    _provider(db_session, kind=ProviderKind.TTS, name="ga-tts", is_active=True)
    rotted = _provider(
        db_session,
        kind=ProviderKind.LLM,
        name="rotated",
        is_active=True,
        bad_cipher=True,
    )
    b = _create_personality(client, display_name="Bob", llm_provider_id=rotted.id)

    with caplog.at_level(logging.WARNING):
        res = _start(client, personality_id=b["id"])
    assert res.status_code == 201, res.text

    spec = _spec(_no_real_pipeline)
    assert "llm" not in spec.provider_payload  # nothing usable to inherit
    assert spec.provider_payload["tts"]["provider_name"] == "ga-tts"  # unaffected
    ov = res.json()["playground_overrides"]
    assert ov["personality_fallbacks"] == [{"kind": "llm", "reason": "undecryptable"}]
    assert "reason=undecryptable" in caplog.text


# --- A. deleted provider: SET NULL → silent inherit -----------------------


def test_deleted_tts_set_null_personality_survives_silently(
    client: TestClient,
    db_session: Session,
    _no_real_pipeline: mock.Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bead A.5 (corrected): deleting a personality's pinned TTS ``SET NULL``s
    the FK; the personality survives and the session inherits the global-active
    TTS. Per the resolver, a NULL FK is a SILENT inherit — NOT a loud fallback
    (the bead's "fallback fires same as above" is the pre-PRD premise; the loud
    path is *deactivation*, see test_deactivated_llm_falls_back_loudly). This
    contrast — delete = silent, deactivate = loud — is the whole point."""
    global_tts = _provider(
        db_session, kind=ProviderKind.TTS, name="ga-tts", is_active=True
    )
    pinned = _provider(db_session, kind=ProviderKind.TTS, name="bs-tts", is_active=False)
    b = _create_personality(client, display_name="Bob", tts_provider_id=pinned.id)
    assert b["tts_provider_id"] == pinned.id

    # Operator deletes the pinned TTS provider → ON DELETE SET NULL fires.
    db_session.delete(pinned)
    db_session.commit()

    # Personality survives; its FK is now NULL.
    survived = client.get(f"/personalities/{b['id']}")
    assert survived.status_code == 200
    assert survived.json()["tts_provider_id"] is None

    with caplog.at_level(logging.WARNING):
        res = _start(client, personality_id=b["id"])
    assert res.status_code == 201, res.text

    # TTS inherits global active; NO fallback recorded, NO log line.
    assert _spec(_no_real_pipeline).provider_payload["tts"]["provider_name"] == "ga-tts"
    ov = res.json()["playground_overrides"]
    assert ov["personality_id"] == b["id"]
    assert "personality_fallbacks" not in ov
    assert "personality.fallback" not in caplog.text
    assert global_tts.id != pinned.id


# --- A. delete the default is refused -------------------------------------


def test_delete_default_personality_refused_409_but_non_default_deletes(
    client: TestClient, db_session: Session
) -> None:
    """Bead A.6 + B's "delete the non-default": deleting the *default* is refused
    with 409 (promote another first) and the row survives, while a *non-default*
    deletes cleanly (204) and is then gone — the 409 is specific to the
    single-default invariant, not a blanket block."""
    a = _create_personality(client, display_name="Alice")
    b = _create_personality(client, display_name="Bob")
    promoted = client.post(f"/personalities/{a['id']}/set-default")
    assert promoted.status_code == 200 and promoted.json()["is_default"] is True

    refused = client.delete(f"/personalities/{a['id']}")
    assert refused.status_code == 409, refused.text
    assert "default" in refused.json()["detail"].lower()
    assert client.get(f"/personalities/{a['id']}").status_code == 200  # still there

    # the non-default deletes cleanly and is gone.
    deleted = client.delete(f"/personalities/{b['id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/personalities/{b['id']}").status_code == 404


# --- A. clone + edit (CRUD endpoints used by the .oly.4 page) --------------


def test_clone_then_patch_personality(
    client: TestClient, db_session: Session
) -> None:
    """Underpins the .oly.4 browser flow "clone → edit description → save":
    POST /clone duplicates as ``"<name> (copy)"``; PATCH edits the clone in
    isolation from its source."""
    llm = _provider(db_session, kind=ProviderKind.LLM, name="brain", is_active=True)
    src = _create_personality(
        client,
        display_name="Alice",
        description="original",
        llm_provider_id=llm.id,
    )

    cloned = client.post(f"/personalities/{src['id']}/clone")
    assert cloned.status_code == 201, cloned.text
    clone = cloned.json()
    assert clone["display_name"] == "Alice (copy)"
    assert clone["id"] != src["id"]
    assert clone["llm_provider_id"] == llm.id  # carried over
    assert clone["is_default"] is False

    patched = client.patch(
        f"/personalities/{clone['id']}", json={"description": "edited clone"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["description"] == "edited clone"
    # source untouched.
    assert client.get(f"/personalities/{src['id']}").json()["description"] == "original"
