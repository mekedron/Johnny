"""Tests for the multi-agent playground group endpoints (Johnny-trt.48).

API-contract coverage with the pipeline spawn stubbed (the
``test_browser_sessions`` discipline): group start creates one browser row
per agent with the shared floor scope + group overrides fragment, validation
(cap / duplicates / unknown agent / live-session gate) rejects cleanly, the
member-vs-group socket ownership is enforced, and the group lifecycle
(per-member end → group survives; last member → teardown) works against real
asyncio member tasks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import browser_session_groups as groups_module
from app.api import browser_sessions as browser_sessions_module
from app.api.deps import get_session
from app.db import Base
from app.db.models import (
    Agent,
    AgentDecision,
    AgentTask,
    AgentUtterance,
    BotMode,
    BotSession,
    BotSessionSource,
    BotSessionStatus,
    CalendarEvent,
    GoogleAccount,
    MeetingAgent,
    MeetingConfig,
    ProviderCredential,
    TranscriptChunk,
)
from app.main import app
from johnny.voice_pipeline.browser_transport import BrowserAudioTransport

# Captured at import time, before the autouse fixture patches the module
# attribute — the lifecycle test drives the REAL monitor directly.
_REAL_MONITOR_GROUP = groups_module._monitor_group


@pytest.fixture(autouse=True)
def _no_real_pipeline() -> Iterator[mock.MagicMock]:
    """Stub the runner spawn for BOTH modules (each holds its own binding)."""

    def _fake_runner(*, bot_session_id: int, spec: Any) -> mock.Mock:
        runner = mock.Mock()
        runner.bot_session_id = bot_session_id
        runner.spec = spec
        runner.transport = BrowserAudioTransport()
        runner.event_bus = None
        runner.pipeline = None
        runner.stop_event = mock.Mock()
        return runner

    with (
        mock.patch.object(
            groups_module, "_spawn_runner", side_effect=_fake_runner
        ) as spawn,
        mock.patch.object(
            browser_sessions_module, "_spawn_runner", side_effect=_fake_runner
        ),
        mock.patch.object(groups_module, "_monitor_group", new=_noop_monitor),
    ):
        yield spawn


async def _noop_monitor(group: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _clean_group_registry() -> Iterator[None]:
    groups_module._session_groups.clear()
    yield
    for group in list(groups_module._session_groups.values()):
        group.audio.close()
        if group.monitor_task is not None and not group.monitor_task.done():
            group.monitor_task.cancel()
    groups_module._session_groups.clear()


@pytest.fixture
def engine() -> sa.Engine:
    eng = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(
        bind=eng,
        tables=[
            GoogleAccount.__table__,  # type: ignore[list-item]
            CalendarEvent.__table__,  # type: ignore[list-item]
            ProviderCredential.__table__,  # type: ignore[list-item]
            Agent.__table__,  # type: ignore[list-item]
            MeetingConfig.__table__,  # type: ignore[list-item]
            MeetingAgent.__table__,  # type: ignore[list-item]
            BotSession.__table__,  # type: ignore[list-item]
            TranscriptChunk.__table__,  # type: ignore[list-item]
            AgentDecision.__table__,  # type: ignore[list-item]
            AgentUtterance.__table__,  # type: ignore[list-item]
            AgentTask.__table__,  # type: ignore[list-item]
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


def _seed_agents(db_session: Session, *names: str) -> list[Agent]:
    agents = [
        Agent(name=name, mode=BotMode.AUTONOMOUS, is_default=(index == 0))
        for index, name in enumerate(names)
    ]
    db_session.add_all(agents)
    db_session.commit()
    for agent in agents:
        db_session.refresh(agent)
    return agents


def _start_group(client: TestClient, agents: list[Agent], **extra: Any) -> Any:
    payload: dict[str, Any] = {
        "agents": [{"agent_id": a.id} for a in agents],
        **extra,
    }
    return client.post("/sessions/browser/groups/start", json=payload)


# --- start ---------------------------------------------------------------------


def test_start_group_creates_member_rows_with_shared_floor_scope(
    client: TestClient, db_session: Session, _no_real_pipeline: mock.MagicMock
) -> None:
    agents = _seed_agents(db_session, "Alex", "Echo")
    res = _start_group(client, agents, context="Sprint sync rehearsal")
    assert res.status_code == 201, res.text
    body = res.json()

    assert len(body["members"]) == 2
    member_ids = [m["session"]["id"] for m in body["members"]]
    assert body["group_id"] == member_ids[0]
    assert body["audio_ws_path"] == f"/ws/sessions/groups/{body['group_id']}/audio"
    assert [m["agent_name"] for m in body["members"]] == ["Alex", "Echo"]

    # Every member's spec carries the same browser-group floor scope.
    specs = [call.kwargs["spec"] for call in _no_real_pipeline.call_args_list]
    scopes = {spec.floor_scope for spec in specs}
    assert scopes == {f"browser-group-{body['group_id']}"}

    # Peer roster (Johnny-trt.47): each member's snapshot names the OTHER
    # members — the router's peer-selectivity prompt input — and the same
    # roster is frozen onto the row the session reads back.
    assert specs[0].agent_snapshot["peer_names"] == ["Echo"]
    assert specs[1].agent_snapshot["peer_names"] == ["Alex"]
    for member_id, peers in zip(member_ids, (["Echo"], ["Alex"]), strict=True):
        row = db_session.get(BotSession, member_id)
        assert row is not None and row.agent_snapshot is not None
        assert row.agent_snapshot["peer_names"] == peers

    # Rows: browser source, joined, agent frozen, group fragment persisted.
    for member_id, agent in zip(member_ids, agents, strict=True):
        row = db_session.get(BotSession, member_id)
        assert row is not None
        assert row.source == BotSessionSource.BROWSER
        assert row.status == BotSessionStatus.JOINED
        assert row.agent_id == agent.id
        assert row.bot_name == agent.name
        overrides = row.playground_overrides or {}
        assert overrides["playground"] is True
        assert overrides["group"]["id"] == body["group_id"]
        assert overrides["group"]["member_ids"] == member_ids
        assert overrides["context"] == "Sprint sync rehearsal"

    group = groups_module.get_session_group(body["group_id"])
    assert group is not None
    assert group.member_ids == member_ids
    assert groups_module.group_id_for_member(member_ids[1]) == body["group_id"]


def test_member_context_overrides_group_context(
    client: TestClient, db_session: Session, _no_real_pipeline: mock.MagicMock
) -> None:
    agents = _seed_agents(db_session, "Alex", "Echo")
    res = client.post(
        "/sessions/browser/groups/start",
        json={
            "agents": [
                {"agent_id": agents[0].id, "context": "You lead."},
                {"agent_id": agents[1].id},
            ],
            "context": "Shared brief",
        },
    )
    assert res.status_code == 201, res.text
    specs = [call.kwargs["spec"] for call in _no_real_pipeline.call_args_list]
    assert specs[0].agent_snapshot["assignment_context"] == "You lead."
    assert specs[1].agent_snapshot["assignment_context"] == "Shared brief"


def test_start_group_requires_two_agents(
    client: TestClient, db_session: Session
) -> None:
    (alex,) = _seed_agents(db_session, "Alex")
    res = _start_group(client, [alex])
    assert res.status_code == 422


def test_start_group_rejects_duplicates_cap_and_unknown_agents(
    client: TestClient, db_session: Session
) -> None:
    agents = _seed_agents(db_session, "A", "B", "C", "D", "E")
    dup = client.post(
        "/sessions/browser/groups/start",
        json={"agents": [{"agent_id": agents[0].id}, {"agent_id": agents[0].id}]},
    )
    assert dup.status_code == 422
    assert "once" in dup.json()["detail"]

    over_cap = _start_group(client, agents)  # 5 > MAX_AGENTS_PER_MEETING (4)
    assert over_cap.status_code == 422
    assert "cap" in over_cap.json()["detail"]

    unknown = client.post(
        "/sessions/browser/groups/start",
        json={"agents": [{"agent_id": agents[0].id}, {"agent_id": 99_999}]},
    )
    assert unknown.status_code == 404
    # The failed start leaves nothing live.
    assert groups_module._session_groups == {}


def test_group_start_blocked_while_single_session_live(
    client: TestClient, db_session: Session
) -> None:
    agents = _seed_agents(db_session, "Alex", "Echo")
    res1 = client.post("/sessions/browser/start", json={})
    assert res1.status_code == 201
    with mock.patch.object(
        browser_sessions_module, "get_session_runner", return_value=mock.Mock()
    ):
        res2 = _start_group(client, agents)
    assert res2.status_code == 409
    assert res2.json()["detail"]["active_session_id"] == res1.json()["id"]


def test_single_start_blocked_while_group_live_with_group_id_in_detail(
    client: TestClient, db_session: Session
) -> None:
    agents = _seed_agents(db_session, "Alex", "Echo")
    res = _start_group(client, agents)
    assert res.status_code == 201, res.text
    gid = res.json()["group_id"]
    with mock.patch.object(
        browser_sessions_module, "get_session_runner", return_value=mock.Mock()
    ):
        res2 = client.post("/sessions/browser/start", json={})
    assert res2.status_code == 409
    detail = res2.json()["detail"]
    assert detail["active_group_id"] == gid
    assert "group" in detail["message"].lower()


# --- stop / text -----------------------------------------------------------------


def test_stop_group_signals_every_member(
    client: TestClient, db_session: Session, _no_real_pipeline: mock.MagicMock
) -> None:
    agents = _seed_agents(db_session, "Alex", "Echo")
    res = _start_group(client, agents)
    gid = res.json()["group_id"]
    spawned = [
        call.kwargs["bot_session_id"] for call in _no_real_pipeline.call_args_list
    ]
    assert set(spawned) == {m["session"]["id"] for m in res.json()["members"]}

    fake_runners = {
        sid: mock.Mock(stop_event=mock.Mock(), pipeline=None, transport=mock.Mock())
        for sid in spawned
    }
    with mock.patch.object(
        groups_module, "get_session_runner", side_effect=lambda sid: fake_runners.get(sid)
    ):
        stop = client.post(f"/sessions/browser/groups/{gid}/stop")
    assert stop.status_code == 200, stop.text
    for runner in fake_runners.values():
        runner.stop_event.set.assert_called_once()


def test_stop_unknown_group_404s(client: TestClient) -> None:
    res = client.post("/sessions/browser/groups/424242/stop")
    assert res.status_code == 404


def test_group_text_fans_out_and_persists_fallback_chunks(
    client: TestClient, db_session: Session
) -> None:
    agents = _seed_agents(db_session, "Alex", "Echo")
    res = _start_group(client, agents)
    gid = res.json()["group_id"]
    member_ids = [m["session"]["id"] for m in res.json()["members"]]

    accepting = mock.Mock()
    accepting.pipeline = mock.AsyncMock()
    accepting.pipeline.feed_text = mock.AsyncMock(return_value=True)
    silent = mock.Mock(pipeline=None)
    runners = {member_ids[0]: accepting, member_ids[1]: silent}
    with mock.patch.object(
        groups_module, "get_session_runner", side_effect=lambda sid: runners.get(sid)
    ):
        res2 = client.post(
            f"/sessions/browser/groups/{gid}/text", json={"text": "Alex, hello"}
        )
    assert res2.status_code == 202, res2.text
    body = res2.json()
    assert body["drove_pipeline"][str(member_ids[0])] is True
    assert body["drove_pipeline"][str(member_ids[1])] is False
    accepting.pipeline.feed_text.assert_awaited_once_with("Alex, hello")
    # The silent member got the chunk persisted so its transcript still
    # records what was said.
    chunks = list(
        db_session.scalars(
            sa.select(TranscriptChunk).where(
                TranscriptChunk.bot_session_id == member_ids[1]
            )
        )
    )
    assert [c.text for c in chunks] == ["Alex, hello"]


# --- member socket ownership -------------------------------------------------------


def test_member_audio_socket_redirects_to_group(
    client: TestClient, db_session: Session
) -> None:
    agents = _seed_agents(db_session, "Alex", "Echo")
    res = _start_group(client, agents)
    gid = res.json()["group_id"]
    member_id = res.json()["members"][1]["session"]["id"]

    with client.websocket_connect(f"/ws/sessions/{member_id}/audio") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ended"
        assert f"group #{gid}" in msg["reason"]


def test_group_audio_socket_serves_ready_and_refuses_second_tab(
    client: TestClient, db_session: Session
) -> None:
    agents = _seed_agents(db_session, "Alex", "Echo")
    res = _start_group(client, agents)
    gid = res.json()["group_id"]

    with client.websocket_connect(f"/ws/sessions/groups/{gid}/audio") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["group_id"] == gid
        assert ready["session_id"] == gid
        assert len(ready["member_ids"]) == 2
        with client.websocket_connect(f"/ws/sessions/groups/{gid}/audio") as ws2:
            refused = ws2.receive_json()
            assert refused["type"] == "ended"
            assert "another tab" in refused["reason"]


def test_group_audio_socket_for_unknown_group_ends_cleanly(
    client: TestClient,
) -> None:
    with client.websocket_connect("/ws/sessions/groups/31337/audio") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ended"
        assert "not active" in msg["reason"]


# --- lifecycle monitor ----------------------------------------------------------------


async def test_monitor_detaches_ended_members_then_tears_down() -> None:
    """End one member → group survives; end the last → group deregisters."""
    from johnny.voice_pipeline.group_audio import GroupAudioRouter

    audio = GroupAudioRouter(mix_tick_s=0.002)
    t1, t2 = BrowserAudioTransport(), BrowserAudioTransport()
    audio.add_member(101, t1)
    audio.add_member(102, t2)
    group = groups_module.BrowserSessionGroup(
        group_id=101,
        member_ids=[101, 102],
        member_names={101: "Alex", 102: "Echo"},
        audio=audio,
    )
    groups_module._session_groups[101] = group

    gate1, gate2 = asyncio.Event(), asyncio.Event()

    async def _member(gate: asyncio.Event) -> None:
        await gate.wait()

    task1 = asyncio.create_task(_member(gate1))
    task2 = asyncio.create_task(_member(gate2))
    runners = {
        101: mock.Mock(task=task1),
        102: mock.Mock(task=task2),
    }
    with mock.patch.object(
        groups_module, "get_session_runner", side_effect=lambda sid: runners.get(sid)
    ):
        monitor = asyncio.create_task(_REAL_MONITOR_GROUP(group))
        gate1.set()
        await asyncio.sleep(0.05)
        # One member ended: detached from the audio router, group still live.
        assert audio.member_ids == [102]
        assert groups_module.get_session_group(101) is group
        assert group.member_ids == [102]

        gate2.set()
        await asyncio.wait_for(monitor, 2.0)
    # Last member ended: group gone, audio closed.
    assert groups_module.get_session_group(101) is None
    assert audio.is_closed
