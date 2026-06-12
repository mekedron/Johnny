"""Tests for app.services.workspace_accounts (Johnny-wks.4).

The serialized gog OAuth connect flow: lock semantics (one connect at a
time across all workspaces, finished records replaceable), the bootstrap
chain (file keyring, keyring password, client-credential seeding from the
default sandbox), remote step 1 parsing, the callback's outcome matrix
(expired / state mismatch / Google error / exchange success / exchange
failure), the accounts view's honest unreachable degrade, cancel ownership,
and disconnect. Sandbox execs and Redis are faked at the service's
injection seams — no docker, no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.services.workspace_accounts import (
    OUTCOME_TTL_SECONDS,
    PENDING_KEY,
    PENDING_TTL_SECONDS,
    GogAuthBusyError,
    GogAuthError,
    GogPreconditionError,
    GogSandboxUnreachableError,
    PendingAuth,
    WorkspaceGogAuthService,
    WorkspaceRef,
)
from johnny.skills.sandbox import SandboxExecResult, SandboxUnavailableError


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-container pytest inherits JOHNNY_USE_DOCKER_LAUNCHER=true — without
    this, _verify_gog_home would drive the REAL docker daemon."""
    monkeypatch.delenv("JOHNNY_USE_DOCKER_LAUNCHER", raising=False)


DEFAULT_WS = WorkspaceRef(id=1, name="Default", slug="default", is_default=True)
FINANCE_WS = WorkspaceRef(id=2, name="Finance", slug="finance", is_default=False)
OPS_WS = WorkspaceRef(id=3, name="Ops", slug="ops", is_default=False)

REDIRECT = "http://127.0.0.1:8000/workspaces/accounts/oauth/callback"
AUTH_URL = (
    "https://accounts.google.com/o/oauth2/auth?client_id=x"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fworkspaces%2Faccounts"
    "%2Foauth%2Fcallback&state=STATE123"
)
CLIENT_JSON = json.dumps(
    {"installed": {"client_id": "cid", "client_secret": "csec"}}
)


def _ok(stdout: str = "") -> SandboxExecResult:
    return SandboxExecResult(
        exit_code=0,
        stdout=stdout,
        stderr="",
        truncated=False,
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        duration_ms=1,
    )


def _fail(stderr: str = "boom", exit_code: int = 1) -> SandboxExecResult:
    return SandboxExecResult(
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        truncated=False,
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        duration_ms=1,
    )


_READY_STATUS = _ok("keyring_backend\tfile\ncredentials_exists\ttrue\n")
_FRESH_STATUS = _ok("config_exists\tfalse\nkeyring_backend\tauto\n")
_STEP1_OUT = _ok(f"auth_url\t{AUTH_URL}\nstate_reused\tfalse\n")


@dataclass
class _FakeSandbox:
    """Scripted exec client: responses matched by argv/cmd signature."""

    script: dict[str, list[Any]]  # signature -> FIFO of results/exceptions
    calls: list[dict[str, Any]] = field(default_factory=list)
    env_present: dict[str, bool] = field(default_factory=dict)
    unreachable: bool = False
    closed: int = 0

    @staticmethod
    def _signature(argv: list[str] | None) -> str:
        if not argv:
            return "?"
        if argv[0] == "sh":
            text = argv[2] if len(argv) > 2 else ""
            if "credentials set" in text:
                return "seed"
            if "gogcli" in text:
                return "find-client"
            return "sh"
        return " ".join(
            part for part in argv[1:] if not part.startswith("-") or part == "--step"
        )[:40]

    async def exec(
        self,
        *,
        argv: list[str] | None = None,
        cmd: str | None = None,
        timeout_s: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxExecResult:
        if self.unreachable:
            raise SandboxUnavailableError("sandbox /exec unreachable: down")
        self.calls.append({"argv": argv, "env": env})
        signature = self._signature(argv)
        for key, queue in self.script.items():
            if signature.startswith(key) and queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                assert isinstance(item, SandboxExecResult)
                return item
        raise AssertionError(f"unscripted exec: {argv} (signature {signature!r})")

    async def check_env(self, names: list[str]) -> dict[str, bool]:
        if self.unreachable:
            raise SandboxUnavailableError("sandbox /exec unreachable: down")
        return {name: self.env_present.get(name, False) for name in names}

    async def aclose(self) -> None:
        self.closed += 1


class _FakeRedis:
    """dict-backed asyncio-Redis shim shared across per-call instances."""

    def __init__(self, store: dict[str, str], events: list[tuple[str, str]]) -> None:
        self.store = store
        self.events = events
        self.ttls: dict[str, int] = {}

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def publish(self, channel: str, payload: str) -> int:
        self.events.append((channel, payload))
        return 1

    async def aclose(self) -> None:
        pass


@dataclass
class _Harness:
    service: WorkspaceGogAuthService
    redis_store: dict[str, str]
    redis: _FakeRedis
    events: list[tuple[str, str]]
    workspace_sandbox: _FakeSandbox
    default_sandbox: _FakeSandbox
    ensured: list[int]


def _harness(
    *,
    workspace_script: dict[str, list[Any]] | None = None,
    default_script: dict[str, list[Any]] | None = None,
    workspace_env: dict[str, bool] | None = None,
    redis_store: dict[str, str] | None = None,
) -> _Harness:
    store = redis_store if redis_store is not None else {}
    events: list[tuple[str, str]] = []
    redis = _FakeRedis(store, events)
    workspace_sandbox = _FakeSandbox(
        script=workspace_script or {},
        env_present=workspace_env if workspace_env is not None else {
            "GOG_HOME": True,
            "GOG_KEYRING_PASSWORD": True,
        },
    )
    default_sandbox = _FakeSandbox(
        script=default_script or {},
        env_present={"GOG_KEYRING_PASSWORD": True},
    )
    ensured: list[int] = []

    def _sandbox_factory(url: str | None) -> _FakeSandbox:
        return default_sandbox if url is None else workspace_sandbox

    async def _ensure(workspace: WorkspaceRef) -> bool:
        ensured.append(workspace.id)
        return True

    service = WorkspaceGogAuthService(
        redis_client_factory=lambda: redis,
        sandbox_client_factory=_sandbox_factory,
        ensure_container=_ensure,
        now=lambda: 1_000.0,
    )
    return _Harness(
        service=service,
        redis_store=store,
        redis=redis,
        events=events,
        workspace_sandbox=workspace_sandbox,
        default_sandbox=default_sandbox,
        ensured=ensured,
    )


def _pending_in(store: dict[str, str]) -> PendingAuth:
    record = PendingAuth.from_json(store[PENDING_KEY])
    assert record is not None
    return record


def _awaiting_record(workspace: WorkspaceRef = FINANCE_WS) -> PendingAuth:
    return PendingAuth(
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        workspace_slug=workspace.slug,
        workspace_is_default=workspace.is_default,
        email="user@example.com",
        services="user",
        redirect_uri=REDIRECT,
        state="STATE123",
        auth_url=AUTH_URL,
        created_at=1_000.0,
        expires_at=1_600.0,
    )


# --- start_connect ------------------------------------------------------------


async def test_start_connect_happy_path_ready_workspace() -> None:
    """Already-bootstrapped workspace: status probe → step 1 → pending saved."""
    h = _harness(
        workspace_script={"auth status": [_READY_STATUS], "auth add": [_STEP1_OUT]}
    )
    record = await h.service.start_connect(
        FINANCE_WS, email="User@Example.com", redirect_uri=REDIRECT
    )
    # login_hint is appended so a multi-session browser preselects the
    # requested account (gog's own URL carries none).
    assert record.auth_url == f"{AUTH_URL}&login_hint=user%40example.com"
    assert record.state == "STATE123"
    assert record.email == "user@example.com"  # normalized
    assert record.status == "awaiting_callback"
    stored = _pending_in(h.redis_store)
    assert stored.state == "STATE123"
    assert h.redis.ttls[PENDING_KEY] <= PENDING_TTL_SECONDS
    assert h.ensured == [FINANCE_WS.id]
    # step 1 argv carried the redirect, services, and forced consent
    step1 = h.workspace_sandbox.calls[-1]["argv"]
    assert step1[:6] == ["gog", "auth", "add", "--remote", "--step", "1"]
    assert REDIRECT in step1
    assert "--force-consent" in step1
    assert step1[-1] == "user@example.com"


async def test_start_connect_bootstraps_fresh_workspace_with_seeded_client() -> None:
    """Fresh gog home: keyring set to file + client JSON copied from the
    default sandbox (env overlay, never argv) before step 1."""
    h = _harness(
        workspace_script={
            "auth status": [_FRESH_STATUS],
            "auth keyring file": [_ok()],
            "seed": [_ok()],
            "auth add": [_STEP1_OUT],
        },
        default_script={"find-client": [_ok(CLIENT_JSON)]},
    )
    record = await h.service.start_connect(
        FINANCE_WS, email="user@example.com", redirect_uri=REDIRECT
    )
    assert record.auth_url.startswith(AUTH_URL)
    seed_call = next(
        call
        for call in h.workspace_sandbox.calls
        if call["argv"][0] == "sh" and "credentials set" in call["argv"][2]
    )
    assert seed_call["env"] == {"GOG_SEED_JSON": CLIENT_JSON}
    # the finder ran in the DEFAULT sandbox, not the workspace's
    assert any(
        "gogcli" in (call["argv"][2] if call["argv"][0] == "sh" else "")
        for call in h.default_sandbox.calls
    )


async def test_start_connect_no_client_json_anywhere_is_actionable_422() -> None:
    h = _harness(
        workspace_script={"auth status": [_FRESH_STATUS], "auth keyring file": [_ok()]},
        default_script={"find-client": [_fail("", exit_code=3)]},
    )
    with pytest.raises(GogPreconditionError, match="sandbox/README.md"):
        await h.service.start_connect(
            FINANCE_WS, email="user@example.com", redirect_uri=REDIRECT
        )
    assert PENDING_KEY not in h.redis_store  # lock released


async def test_start_connect_default_workspace_missing_client_is_422() -> None:
    """The default IS the seed source — nothing to copy from, so the flow
    points at the README instead of execing a self-copy."""
    h = _harness(
        workspace_script={},
        default_script={"auth status": [_FRESH_STATUS], "auth keyring file": [_ok()]},
    )
    h.default_sandbox.env_present["GOG_KEYRING_PASSWORD"] = True
    with pytest.raises(GogPreconditionError, match="sandbox/README.md"):
        await h.service.start_connect(
            DEFAULT_WS, email="user@example.com", redirect_uri=REDIRECT
        )
    assert PENDING_KEY not in h.redis_store


async def test_start_connect_missing_keyring_password_is_actionable_422() -> None:
    h = _harness(
        workspace_script={"auth status": [_READY_STATUS]},
        workspace_env={"GOG_HOME": True, "GOG_KEYRING_PASSWORD": False},
    )
    with pytest.raises(GogPreconditionError, match="GOG_KEYRING_PASSWORD"):
        await h.service.start_connect(
            FINANCE_WS, email="user@example.com", redirect_uri=REDIRECT
        )
    assert PENDING_KEY not in h.redis_store


async def test_start_connect_busy_lock_names_the_holder() -> None:
    h = _harness()
    h.redis_store[PENDING_KEY] = _awaiting_record(FINANCE_WS).to_json()
    with pytest.raises(GogAuthBusyError) as excinfo:
        await h.service.start_connect(
            OPS_WS, email="other@example.com", redirect_uri=REDIRECT
        )
    assert "Finance" in str(excinfo.value)
    # the live record was NOT clobbered
    assert _pending_in(h.redis_store).workspace_id == FINANCE_WS.id


async def test_start_connect_replaces_finished_records() -> None:
    stale = _awaiting_record(FINANCE_WS)
    stale.status = "failed"
    stale.error = "old failure"
    h = _harness(
        workspace_script={"auth status": [_READY_STATUS], "auth add": [_STEP1_OUT]},
        redis_store={PENDING_KEY: stale.to_json()},
    )
    record = await h.service.start_connect(
        FINANCE_WS, email="user@example.com", redirect_uri=REDIRECT
    )
    assert record.status == "awaiting_callback"
    assert _pending_in(h.redis_store).status == "awaiting_callback"


async def test_start_connect_sandbox_unreachable_is_503_and_releases_lock() -> None:
    h = _harness()
    h.workspace_sandbox.unreachable = True
    with pytest.raises(GogSandboxUnreachableError):
        await h.service.start_connect(
            FINANCE_WS, email="user@example.com", redirect_uri=REDIRECT
        )
    assert PENDING_KEY not in h.redis_store


async def test_start_connect_step1_failure_surfaces_tail_and_releases_lock() -> None:
    h = _harness(
        workspace_script={
            "auth status": [_READY_STATUS],
            "auth add": [_fail("OAuth client credentials missing", exit_code=10)],
        }
    )
    with pytest.raises(GogAuthError, match="OAuth client credentials missing"):
        await h.service.start_connect(
            FINANCE_WS, email="user@example.com", redirect_uri=REDIRECT
        )
    assert PENDING_KEY not in h.redis_store


async def test_start_connect_rejects_bad_email_and_services() -> None:
    h = _harness()
    with pytest.raises(GogPreconditionError, match="email"):
        await h.service.start_connect(
            FINANCE_WS, email="not-an-email", redirect_uri=REDIRECT
        )
    with pytest.raises(GogPreconditionError, match="services"):
        await h.service.start_connect(
            FINANCE_WS,
            email="user@example.com",
            services="calendar; rm -rf /",
            redirect_uri=REDIRECT,
        )
    assert PENDING_KEY not in h.redis_store


# --- complete_callback ----------------------------------------------------------


async def test_callback_without_pending_reports_expired() -> None:
    h = _harness()
    outcome = await h.service.complete_callback("state=STATE123&code=abc")
    assert outcome.status == "expired"


async def test_callback_state_mismatch_keeps_the_live_flow() -> None:
    h = _harness(redis_store={PENDING_KEY: _awaiting_record().to_json()})
    outcome = await h.service.complete_callback("state=WRONG&code=abc")
    assert outcome.status == "mismatch"
    assert _pending_in(h.redis_store).status == "awaiting_callback"


async def test_callback_google_error_marks_failed() -> None:
    h = _harness(redis_store={PENDING_KEY: _awaiting_record().to_json()})
    outcome = await h.service.complete_callback("state=STATE123&error=access_denied")
    assert outcome.status == "failed"
    stored = _pending_in(h.redis_store)
    assert stored.status == "failed"
    assert "access_denied" in stored.error
    assert h.redis.ttls[PENDING_KEY] == OUTCOME_TTL_SECONDS


async def test_callback_success_exchanges_in_the_pending_workspace() -> None:
    h = _harness(
        workspace_script={"auth add": [_ok("stored refresh token")]},
        redis_store={PENDING_KEY: _awaiting_record(FINANCE_WS).to_json()},
    )
    outcome = await h.service.complete_callback("state=STATE123&code=abc&scope=email")
    assert outcome.status == "completed"
    assert outcome.email == "user@example.com"
    stored = _pending_in(h.redis_store)
    assert stored.status == "completed"
    # the container was re-ensured (it may have idled out mid-consent)
    assert h.ensured == [FINANCE_WS.id]
    step2 = h.workspace_sandbox.calls[-1]["argv"]
    assert "--step" in step2 and "2" in step2
    auth_url_arg = step2[step2.index("--auth-url") + 1]
    assert auth_url_arg == f"{REDIRECT}?state=STATE123&code=abc&scope=email"
    # capability snapshots for this workspace were nudged
    assert any(
        json.loads(payload) == {"workspace_id": FINANCE_WS.id, "event": "auth-changed"}
        for _, payload in h.events
    )


async def test_callback_exchange_failure_marks_failed_with_tail() -> None:
    h = _harness(
        workspace_script={"auth add": [_fail("token exchange failed: invalid_grant")]},
        redis_store={PENDING_KEY: _awaiting_record(FINANCE_WS).to_json()},
    )
    outcome = await h.service.complete_callback("state=STATE123&code=abc")
    assert outcome.status == "failed"
    assert "invalid_grant" in outcome.message
    assert _pending_in(h.redis_store).status == "failed"
    assert h.events == []  # no auth-changed nudge on failure


async def test_callback_sandbox_down_during_exchange_marks_failed() -> None:
    h = _harness(redis_store={PENDING_KEY: _awaiting_record(FINANCE_WS).to_json()})
    h.workspace_sandbox.unreachable = True
    outcome = await h.service.complete_callback("state=STATE123&code=abc")
    assert outcome.status == "failed"
    assert "unreachable" in outcome.message


# --- accounts_view ----------------------------------------------------------------


async def test_accounts_view_lists_accounts_and_readiness() -> None:
    listing = json.dumps(
        {
            "accounts": [
                {"email": "a@x.com", "client": "default", "services": ["calendar"]},
            ]
        }
    )
    h = _harness(
        workspace_script={"auth status": [_READY_STATUS], "auth list": [_ok(listing)]}
    )
    view = await h.service.accounts_view(FINANCE_WS)
    assert view.reachable is True
    assert view.keyring_backend == "file"
    assert view.client_credentials is True
    assert [account.email for account in view.accounts] == ["a@x.com"]
    assert view.accounts[0].services == ["calendar"]
    assert h.ensured == [FINANCE_WS.id]  # the GET is the refresh


async def test_accounts_view_unreachable_degrades_honestly() -> None:
    h = _harness()
    h.workspace_sandbox.unreachable = True
    view = await h.service.accounts_view(FINANCE_WS)
    assert view.reachable is False
    assert "unreachable" in view.reason
    assert view.accounts == []


async def test_accounts_view_exposes_own_pending_and_foreign_busy() -> None:
    h = _harness(
        workspace_script={
            "auth status": [_READY_STATUS, _READY_STATUS],
            "auth list": [_ok('{"accounts": []}'), _ok('{"accounts": []}')],
        },
        redis_store={PENDING_KEY: _awaiting_record(FINANCE_WS).to_json()},
    )
    own = await h.service.accounts_view(FINANCE_WS)
    assert own.pending is not None and own.pending.email == "user@example.com"
    assert own.busy is None
    foreign = await h.service.accounts_view(OPS_WS)
    assert foreign.pending is None
    assert foreign.busy is not None and foreign.busy.workspace_name == "Finance"


async def test_accounts_view_finished_foreign_record_is_not_busy() -> None:
    done = _awaiting_record(FINANCE_WS)
    done.status = "completed"
    h = _harness(
        workspace_script={
            "auth status": [_READY_STATUS],
            "auth list": [_ok('{"accounts": []}')],
        },
        redis_store={PENDING_KEY: done.to_json()},
    )
    view = await h.service.accounts_view(OPS_WS)
    assert view.busy is None


# --- cancel / disconnect -------------------------------------------------------------


async def test_cancel_clears_own_record_and_is_idempotent() -> None:
    h = _harness(redis_store={PENDING_KEY: _awaiting_record(FINANCE_WS).to_json()})
    await h.service.cancel_pending(FINANCE_WS.id)
    assert PENDING_KEY not in h.redis_store
    await h.service.cancel_pending(FINANCE_WS.id)  # no record → no error


async def test_cancel_refuses_anothers_live_flow() -> None:
    h = _harness(redis_store={PENDING_KEY: _awaiting_record(FINANCE_WS).to_json()})
    with pytest.raises(GogAuthBusyError):
        await h.service.cancel_pending(OPS_WS.id)
    assert PENDING_KEY in h.redis_store


async def test_disconnect_removes_and_publishes() -> None:
    h = _harness(workspace_script={"auth remove": [_ok("removed")]})
    await h.service.disconnect(FINANCE_WS, "a@x.com")
    argv = h.workspace_sandbox.calls[-1]["argv"]
    assert argv == ["gog", "auth", "remove", "a@x.com", "--force"]
    assert any(
        json.loads(payload)["event"] == "auth-changed" for _, payload in h.events
    )


async def test_disconnect_failure_surfaces_tail() -> None:
    h = _harness(workspace_script={"auth remove": [_fail("no such account")]})
    with pytest.raises(GogAuthError, match="no such account"):
        await h.service.disconnect(FINANCE_WS, "a@x.com")
