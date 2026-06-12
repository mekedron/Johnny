"""Workspace Google-account connect — gog OAuth into a workspace's keyring
(Johnny-wks.4).

Identity lives on the WORKSPACE: connecting a Google account stores its
credentials in that workspace's gog file keyring (the commit 72985666
pattern), so EVERY agent attached to the workspace can use them in its
delegated tasks — and no other workspace can. Storage:

* non-default workspaces — ``~/.johnny/workspaces/<slug>/gog`` on the host,
  bind-mounted at ``/home/sandbox/gog`` and announced via ``GOG_HOME``
  (:mod:`app.services.workspace_containers`). Host bind = auth survives
  idle-TTL restarts, ``./stop.sh`` factory resets, and clean installs;
  cross-workspace absence checks are plain host-path checks.
* the default workspace — gog's XDG layout under the always-on compose
  service's ``~/.johnny/sandbox-home`` bind, byte-identical to the
  pre-workspaces flow documented in sandbox/README.md.

CALLBACK-PORT STRATEGY (the bead's documented decision): there is NO OAuth
callback listener and NO published port at all. The flow uses gog's
remote/manual mode —

1. ``gog auth add --remote --step 1`` runs INSIDE the target workspace's
   container (lazy-started if needed) and prints the Google consent URL,
   with ``--redirect-uri`` pointed at this api's
   ``GET /workspaces/accounts/oauth/callback``;
2. the operator authorizes in their browser; Google redirects to the api
   (a loopback address — the api's published port — so this works exactly
   where the old fixed-port-8089 flow worked);
3. the callback endpoint relays the full redirect URL into the SAME
   container via ``gog auth add --remote --step 2 --auth-url …`` — gog
   exchanges the code itself (egress from the container) and stores the
   refresh token in the workspace's keyring. Nothing secret is ever
   rendered to the UI; the one-time code transits api→sandbox over the
   internal compose network.

Because the redirect targets the api, not a per-container port, concurrent
auths would only contend on the single Redis pending record — and the flow
SERIALIZES on it anyway (one connect at a time across all workspaces, the
bead's simplest-correct option) with a clear UI lock: the pending record is
a Redis key with a TTL, exposed by the accounts view so every workspace's
panel can show who holds the lock and offer cancel. The replace-after-
outcome path (a completed/failed record being overwritten by a new connect)
is last-writer-wins by design — a lost race leaves an orphaned gog state
file and an auth URL whose callback reports a state mismatch, never a
mis-routed credential.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from johnny.skills.sandbox import (
    SandboxClient,
    SandboxError,
    SandboxExecResult,
    SandboxUnavailableError,
    sandbox_url_for_workspace,
)

logger = logging.getLogger(__name__)

# One global pending record = the serialization lock. TTL bounds how long an
# abandoned consent tab can hold the lock; outcomes linger briefly so the
# panel's poll can report them before they age out.
PENDING_KEY = "johnny:workspace:gog-auth:pending"
PENDING_TTL_SECONDS = 600
OUTCOME_TTL_SECONDS = 180

# The api route the OAuth redirect lands on (registered by
# app.api.workspace_accounts; segment count keeps it clear of
# ``/workspaces/{workspace_id}``).
CALLBACK_PATH = "/workspaces/accounts/oauth/callback"

# gog's own default bundle ("user" = every standard user OAuth service).
DEFAULT_SERVICES = "user"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SERVICES_RE = re.compile(r"^[a-z0-9][a-z0-9,_-]*$")

# Step 2 exchanges the code with Google from inside the container.
_EXEC_TIMEOUT_FAST_S = 20.0
_EXEC_TIMEOUT_EXCHANGE_S = 60.0

_STATUS_AWAITING = "awaiting_callback"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"


class GogAuthError(Exception):
    """Base failure for the connect flow (router maps it to HTTP 502)."""


class GogSandboxUnreachableError(GogAuthError):
    """The workspace's sandbox container could not be reached (503)."""


class GogPreconditionError(GogAuthError):
    """A missing operator-fixable prerequisite, with the actionable fix (422)."""


class GogAuthBusyError(GogAuthError):
    """Another account connect is mid-flight (409); carries its summary."""

    def __init__(self, pending: PendingAuth) -> None:
        super().__init__(
            f"an account connect is already in progress for workspace "
            f"{pending.workspace_name!r} ({pending.email}) — one at a time; "
            "finish or cancel it first"
        )
        self.pending = pending


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    """The workspace identity the service needs (no ORM coupling)."""

    id: int
    name: str
    slug: str
    is_default: bool


@dataclass(slots=True)
class PendingAuth:
    """The serialized connect flow's state, as stored in Redis.

    Carries the workspace stamp fields the callback needs to re-ensure and
    address the right container without a DB round-trip. ``auth_url`` is the
    Google consent URL (safe to render — it contains the public client id
    and PKCE challenge, no secrets).
    """

    workspace_id: int
    workspace_name: str
    workspace_slug: str
    workspace_is_default: bool
    email: str
    services: str
    redirect_uri: str
    status: str = _STATUS_AWAITING
    state: str = ""
    auth_url: str = ""
    error: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> PendingAuth | None:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return cls(**{f: data[f] for f in cls.__dataclass_fields__ if f in data})
        except (ValueError, TypeError):
            return None


@dataclass(slots=True)
class GogAccount:
    """One connected account as ``gog auth list --json`` reports it."""

    email: str
    client: str = "default"
    services: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AccountsView:
    """The GET payload: list + sandbox readiness + the (global) lock state."""

    workspace_id: int
    workspace_name: str
    reachable: bool
    reason: str = ""
    keyring_backend: str = ""
    client_credentials: bool = False
    accounts: list[GogAccount] = field(default_factory=list)
    pending: PendingAuth | None = None  # this workspace's flow (any status)
    busy: PendingAuth | None = None  # another workspace's AWAITING flow


@dataclass(slots=True)
class CallbackOutcome:
    """What the callback page should tell the human."""

    status: str  # completed | failed | mismatch | expired
    message: str
    workspace_name: str = ""
    email: str = ""


def _tail(result: SandboxExecResult, limit: int = 400) -> str:
    """The honest error surface: last bytes of both streams, compact."""
    text = "; ".join(
        part.strip() for part in (result.stderr, result.stdout) if part.strip()
    )
    return text[-limit:] if text else f"exit code {result.exit_code}"


# Finds the operator's OAuth client JSON (the as-downloaded Google Cloud
# file) in the DEFAULT sandbox's gog config dir — the sandbox/README.md
# storage location. Prints it; exit 3 = none present.
_FIND_CLIENT_JSON_SH = (
    'for f in "$HOME/.config/gogcli/"*.json; do '
    '[ -f "$f" ] || continue; '
    "if jq -e '(.installed // .web) | (.client_id and .client_secret)' "
    '"$f" >/dev/null 2>&1; then cat "$f"; exit 0; fi; '
    "done; exit 3"
)

# Writes the client JSON (passed via the env overlay, never argv) into the
# target's GOG_HOME and imports it into gog's credential store; the temp
# file is removed either way.
_SEED_CLIENT_JSON_SH = (
    "set -e; "
    'umask 077; mkdir -p "$GOG_HOME"; tmp="$GOG_HOME/.oauth-client-seed.json"; '
    'printf "%s" "$GOG_SEED_JSON" > "$tmp"; '
    # No set -e past this point: the temp file (it holds the client secret)
    # must be removed whether the import succeeds or not.
    'set +e; gog auth credentials set "$tmp"; rc=$?; rm -f "$tmp"; exit $rc'
)


class WorkspaceGogAuthService:
    """The connect/list/disconnect flows, exec'd inside workspace sandboxes.

    Redis and sandbox clients are created PER CALL (the
    :class:`app.services.workspace_containers.WorkspaceContainerManager`
    loop-safety rule); the factories exist for tests.
    """

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        redis_client_factory: Callable[[], Any] | None = None,
        sandbox_client_factory: Callable[[str | None], Any] | None = None,
        ensure_container: Callable[..., Any] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._redis_url = (
            redis_url
            if redis_url is not None
            else os.environ.get("REDIS_URL", "").strip() or None
        )
        self._redis_client_factory = redis_client_factory
        self._sandbox_client_factory = sandbox_client_factory
        self._ensure_container = ensure_container
        self._now = now

    # --- client plumbing --------------------------------------------------

    def _redis_client(self) -> Any | None:
        if self._redis_client_factory is not None:
            return self._redis_client_factory()
        if not self._redis_url:
            return None
        from redis.asyncio import Redis

        return Redis.from_url(self._redis_url)

    def _sandbox(self, workspace: WorkspaceRef | None) -> Any:
        """An exec client for the workspace's sandbox (``None``/default →
        the always-on compose service)."""
        url = (
            None
            if workspace is None or workspace.is_default
            else sandbox_url_for_workspace(workspace.id)
        )
        if self._sandbox_client_factory is not None:
            return self._sandbox_client_factory(url)
        return SandboxClient(base_url=url)

    async def _ensure(self, workspace: WorkspaceRef) -> bool:
        """Lazy-start the workspace's container (never raises; default no-ops)."""
        if self._ensure_container is not None:
            return bool(await self._ensure_container(workspace))
        from app.services.workspace_containers import (
            ensure_workspace_container_for_stamp,
        )

        return await ensure_workspace_container_for_stamp(
            {
                "id": workspace.id,
                "is_default": workspace.is_default,
                "slug": workspace.slug,
            },
            context_label=f"account connect (workspace {workspace.id})",
        )

    # --- pending record (the lock) ------------------------------------------

    async def _read_pending(self) -> PendingAuth | None:
        client = self._redis_client()
        if client is None:
            return None
        try:
            try:
                raw = await client.get(PENDING_KEY)
            finally:
                await client.aclose()
        except Exception:  # noqa: BLE001 — the view degrades, never breaks
            logger.warning("gog-auth: pending read failed", exc_info=True)
            return None
        if raw is None:
            return None
        return PendingAuth.from_json(raw)

    async def _save_pending(self, record: PendingAuth, *, ttl_s: int) -> None:
        client = self._redis_client()
        if client is None:
            raise GogAuthError(
                "Redis is not configured — the account connect flow needs it "
                "for the one-at-a-time lock"
            )
        try:
            try:
                await client.set(PENDING_KEY, record.to_json(), ex=ttl_s)
            finally:
                await client.aclose()
        except GogAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced as a flow failure
            raise GogAuthError(f"could not persist the connect state: {exc}") from exc

    async def _save_outcome(self, record: PendingAuth) -> None:
        """Persist a terminal outcome for the panel's poll; the callback page
        already tells the human everything, so a Redis hiccup here only
        costs the in-app echo — log it, never raise."""
        try:
            await self._save_pending(record, ttl_s=OUTCOME_TTL_SECONDS)
        except GogAuthError:
            logger.warning("gog-auth: could not persist the flow outcome", exc_info=True)

    async def _delete_pending(self) -> None:
        client = self._redis_client()
        if client is None:
            return
        try:
            try:
                await client.delete(PENDING_KEY)
            finally:
                await client.aclose()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning("gog-auth: pending delete failed", exc_info=True)

    async def _acquire_lock(self, record: PendingAuth) -> None:
        """One connect at a time: SET NX, else replace only finished records."""
        client = self._redis_client()
        if client is None:
            raise GogAuthError(
                "Redis is not configured — the account connect flow needs it "
                "for the one-at-a-time lock"
            )
        try:
            try:
                acquired = await client.set(
                    PENDING_KEY, record.to_json(), nx=True, ex=PENDING_TTL_SECONDS
                )
                if acquired:
                    return
                raw = await client.get(PENDING_KEY)
                existing = PendingAuth.from_json(raw) if raw is not None else None
                if existing is not None and existing.status == _STATUS_AWAITING:
                    raise GogAuthBusyError(existing)
                # A finished (completed/failed) record only informs the UI —
                # replace it. Two replacers racing is last-writer-wins (see
                # the module docstring).
                await client.set(PENDING_KEY, record.to_json(), ex=PENDING_TTL_SECONDS)
            finally:
                await client.aclose()
        except GogAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 — lock layer must speak plainly
            raise GogAuthError(f"could not acquire the connect lock: {exc}") from exc

    # --- events ---------------------------------------------------------------

    async def _publish_auth_changed(self, workspace_id: int) -> None:
        """Credential state changed → nudge the per-workspace capability
        snapshot (same channel the container lifecycle uses); best-effort."""
        from app.services.workspace_containers import WORKSPACE_SANDBOX_EVENT_CHANNEL

        client = self._redis_client()
        if client is None:
            return
        try:
            try:
                await client.publish(
                    WORKSPACE_SANDBOX_EVENT_CHANNEL,
                    json.dumps({"workspace_id": workspace_id, "event": "auth-changed"}),
                )
            finally:
                await client.aclose()
        except Exception:  # noqa: BLE001 — freshness nudge only
            logger.warning(
                "gog-auth: auth-changed publish failed for workspace %s",
                workspace_id,
                exc_info=True,
            )

    # --- gog probes -------------------------------------------------------------

    async def _gog_status(self, client: Any) -> dict[str, str]:
        """``gog auth status`` parsed from its TSV (missing keys = absent)."""
        result = await client.exec(
            argv=["gog", "auth", "status"], timeout_s=_EXEC_TIMEOUT_FAST_S
        )
        if result.exit_code != 0:
            raise GogAuthError(f"gog auth status failed: {_tail(result)}")
        out: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("\t")
            if sep:
                out[key.strip()] = value.strip()
        return out

    async def _list_accounts(self, client: Any) -> list[GogAccount]:
        result = await client.exec(
            argv=["gog", "auth", "list", "--json"], timeout_s=_EXEC_TIMEOUT_FAST_S
        )
        if result.exit_code != 0:
            raise GogAuthError(f"gog auth list failed: {_tail(result)}")
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise GogAuthError("gog auth list returned non-JSON output") from exc
        accounts: list[GogAccount] = []
        for entry in payload.get("accounts") or []:
            if not isinstance(entry, dict) or not entry.get("email"):
                continue
            services = entry.get("services")
            accounts.append(
                GogAccount(
                    email=str(entry["email"]),
                    client=str(entry.get("client") or "default"),
                    services=[str(s) for s in services]
                    if isinstance(services, list)
                    else [],
                )
            )
        return accounts

    # --- the public flows ---------------------------------------------------

    async def accounts_view(self, workspace: WorkspaceRef) -> AccountsView:
        """The workspace's connected accounts + readiness + lock state.

        The GET is the refresh (the capabilities-API convention): a
        non-default workspace's container is lazily ensured first. An
        unreachable sandbox degrades to ``reachable=False`` with the reason —
        never an error response.
        """
        pending = await self._read_pending()
        view = AccountsView(
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            reachable=False,
        )
        if pending is not None:
            if pending.workspace_id == workspace.id:
                view.pending = pending
            elif pending.status == _STATUS_AWAITING:
                view.busy = pending
        await self._ensure(workspace)
        client = self._sandbox(workspace)
        try:
            status = await self._gog_status(client)
            view.accounts = await self._list_accounts(client)
        except (SandboxUnavailableError, GogAuthError) as exc:
            view.reason = str(exc)
            return view
        finally:
            await client.aclose()
        view.reachable = True
        view.keyring_backend = status.get("keyring_backend", "")
        view.client_credentials = status.get("credentials_exists") == "true"
        return view

    async def start_connect(
        self,
        workspace: WorkspaceRef,
        *,
        email: str,
        services: str = DEFAULT_SERVICES,
        redirect_uri: str,
    ) -> PendingAuth:
        """Acquire the lock, ready the workspace's gog home, and produce the
        Google consent URL (remote step 1). On any failure the lock is
        released so the operator can retry immediately."""
        email = email.strip().lower()
        if not _EMAIL_RE.match(email):
            raise GogPreconditionError(f"{email!r} does not look like an email address")
        services = (services or DEFAULT_SERVICES).strip().lower()
        if not _SERVICES_RE.match(services):
            raise GogPreconditionError(
                "services must be a comma-separated list of gog service names "
                "(see `gog auth services`)"
            )
        now = self._now()
        record = PendingAuth(
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_slug=workspace.slug,
            workspace_is_default=workspace.is_default,
            email=email,
            services=services,
            redirect_uri=redirect_uri,
            created_at=now,
            expires_at=now + PENDING_TTL_SECONDS,
        )
        await self._acquire_lock(record)
        try:
            # Best-effort lazy start; reachability truth is the exec itself
            # (ensure also returns False where docker isn't driven at all).
            await self._ensure(workspace)
            client = self._sandbox(workspace)
            try:
                await self._verify_gog_home(workspace, client)
                await self._bootstrap_gog(workspace, client)
                record.auth_url, record.state = await self._remote_step1(
                    client, record
                )
            finally:
                await client.aclose()
        except SandboxUnavailableError as exc:
            await self._delete_pending()
            raise GogSandboxUnreachableError(
                f"workspace sandbox unreachable: {exc}"
            ) from exc
        except Exception:
            await self._delete_pending()
            raise
        ttl = max(int(record.expires_at - self._now()), 60)
        await self._save_pending(record, ttl_s=ttl)
        return record

    async def _verify_gog_home(self, workspace: WorkspaceRef, client: Any) -> None:
        """Make sure the container actually routes gog state to the host bind.

        A workspace container launched before wks.4 has neither the gog
        mount nor ``GOG_HOME`` — auth done there would land in the state
        volume and silently miss the storage convention. Containers are
        disposable by design, so the fix is a one-shot recreate; if
        ``GOG_HOME`` still doesn't appear, refuse with the operator-level
        cause rather than authing into the wrong place.
        """
        from app.services.docker_launcher import should_use_docker_launcher
        from app.services.workspace_containers import (
            WorkspaceContainerError,
            get_workspace_container_manager,
        )

        if workspace.is_default or not should_use_docker_launcher():
            return
        present = await client.check_env(["GOG_HOME"])
        if present.get("GOG_HOME"):
            return
        logger.info(
            "workspace %s: container predates per-workspace gog state — "
            "recreating it to pick up the GOG_HOME mount",
            workspace.id,
        )
        manager = get_workspace_container_manager()
        try:
            await asyncio.to_thread(
                manager.retire, workspace_id=workspace.id, remove_volume=False
            )
        except WorkspaceContainerError as exc:
            raise GogAuthError(
                f"could not refresh the workspace container: {exc}"
            ) from exc
        if not await self._ensure(workspace):
            raise GogSandboxUnreachableError(
                "the workspace container did not come back after a refresh"
            )
        present = await client.check_env(["GOG_HOME"])
        if not present.get("GOG_HOME"):
            raise GogAuthError(
                "the workspace container does not expose GOG_HOME even after "
                "a restart — check JOHNNY_WORKSPACES_HOST_DIR in the api "
                "environment"
            )

    async def _bootstrap_gog(self, workspace: WorkspaceRef, client: Any) -> None:
        """Idempotent prerequisites: file keyring + password + client creds."""
        status = await self._gog_status(client)
        if status.get("keyring_backend") != "file":
            result = await client.exec(
                argv=["gog", "auth", "keyring", "file"],
                timeout_s=_EXEC_TIMEOUT_FAST_S,
            )
            if result.exit_code != 0:
                raise GogAuthError(
                    f"switching gog to the file keyring failed: {_tail(result)}"
                )
        password_set = await client.check_env(["GOG_KEYRING_PASSWORD"])
        if not password_set.get("GOG_KEYRING_PASSWORD"):
            raise GogPreconditionError(
                "GOG_KEYRING_PASSWORD is not set in the sandbox environment — "
                "set it in .env (any non-empty value, keep it stable) and "
                "rerun ./run-dev.sh or ./run.sh"
            )
        if status.get("credentials_exists") == "true":
            return
        await self._seed_client_credentials(workspace, client)

    async def _seed_client_credentials(
        self, workspace: WorkspaceRef, client: Any
    ) -> None:
        """Copy the operator's OAuth client JSON (app identity, one per
        deployment) from the default sandbox into this workspace's gog home.

        The client JSON identifies the Google Cloud OAuth APP, not a user —
        sharing it across workspaces is what makes "authorize once per app,
        connect accounts per workspace" work. Source of truth is the
        sandbox/README.md location (``~/.johnny/sandbox-home/.config/gogcli/``);
        when it's absent the flow refuses with the exact fix.
        """
        missing_msg = (
            "no OAuth client credentials found. Download the OAuth client "
            "JSON (Desktop app) from Google Cloud Console and place it in "
            "~/.johnny/sandbox-home/.config/gogcli/ — see sandbox/README.md, "
            "'gog auth' section — then retry"
        )
        if workspace.is_default:
            # The default IS the source; nothing to copy from.
            raise GogPreconditionError(missing_msg)
        source = self._sandbox(None)
        try:
            found = await source.exec(
                argv=["sh", "-c", _FIND_CLIENT_JSON_SH],
                timeout_s=_EXEC_TIMEOUT_FAST_S,
            )
        except SandboxUnavailableError as exc:
            raise GogAuthError(
                f"could not read the OAuth client JSON from the default "
                f"sandbox: {exc}"
            ) from exc
        finally:
            await source.aclose()
        client_json = found.stdout.strip()
        if found.exit_code != 0 or not client_json:
            raise GogPreconditionError(missing_msg)
        try:
            parsed = json.loads(client_json)
            block = parsed.get("installed") or parsed.get("web") or {}
            if not (block.get("client_id") and block.get("client_secret")):
                raise ValueError("not an OAuth client JSON")
        except (ValueError, AttributeError) as exc:
            raise GogPreconditionError(missing_msg) from exc
        seeded = await client.exec(
            argv=["sh", "-c", _SEED_CLIENT_JSON_SH],
            env={"GOG_SEED_JSON": client_json},
            timeout_s=_EXEC_TIMEOUT_FAST_S,
        )
        if seeded.exit_code != 0:
            raise GogAuthError(
                f"storing the OAuth client credentials in workspace "
                f"{workspace.name!r} failed: {_tail(seeded)}"
            )

    async def _remote_step1(
        self, client: Any, record: PendingAuth
    ) -> tuple[str, str]:
        result = await client.exec(
            argv=[
                "gog",
                "auth",
                "add",
                "--remote",
                "--step",
                "1",
                "--redirect-uri",
                record.redirect_uri,
                "--services",
                record.services,
                # Always re-prompt consent so a re-connect is guaranteed a
                # fresh refresh token (Google omits it on silent re-auth).
                "--force-consent",
                record.email,
            ],
            timeout_s=_EXEC_TIMEOUT_FAST_S,
        )
        if result.exit_code != 0:
            raise GogAuthError(f"gog auth add (step 1) failed: {_tail(result)}")
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("\t")
            if sep and key.strip() == "auth_url":
                url = value.strip()
                state = (parse_qs(urlsplit(url).query).get("state") or [""])[0]
                if url and state:
                    # gog's URL carries no login_hint, so a multi-session
                    # browser lands on whatever account is authuser=0 —
                    # not the one the operator typed. Appending the
                    # standard OAuth login_hint preselects the requested
                    # account; gog's step 2 only consumes the redirect
                    # query, so it never sees (or needs) this param.
                    return (
                        f"{url}&login_hint={quote(record.email, safe='')}",
                        state,
                    )
        raise GogAuthError(
            "gog auth add (step 1) printed no auth_url — "
            f"output was: {_tail(result)}"
        )

    async def complete_callback(self, raw_query: str) -> CallbackOutcome:
        """Handle the browser's OAuth redirect (remote step 2).

        Never raises: whatever happens becomes a human-readable outcome for
        the callback page, and terminal outcomes replace the pending record
        (with a short TTL) so the panel's poll can report them.
        """
        params = {
            key: values[0]
            for key, values in parse_qs(raw_query, keep_blank_values=True).items()
        }
        pending = await self._read_pending()
        if pending is None or pending.status != _STATUS_AWAITING:
            return CallbackOutcome(
                status="expired",
                message=(
                    "There is no account connect waiting for this sign-in — "
                    "it may have expired or been cancelled. Return to Johnny "
                    "and start again."
                ),
            )
        state = params.get("state", "")
        if not state or state != pending.state:
            # A stray or replayed redirect must not kill a live flow.
            return CallbackOutcome(
                status="mismatch",
                message=(
                    "This sign-in does not match the connect currently in "
                    "progress. The original flow is still waiting — use its "
                    "Google tab, or cancel it in Johnny and start fresh."
                ),
            )
        if params.get("error"):
            pending.status = _STATUS_FAILED
            pending.error = f"Google returned: {params['error']}"
            await self._save_outcome(pending)
            return CallbackOutcome(
                status="failed",
                message=f"Google did not authorize the account ({params['error']}).",
                workspace_name=pending.workspace_name,
                email=pending.email,
            )
        workspace = WorkspaceRef(
            id=pending.workspace_id,
            name=pending.workspace_name,
            slug=pending.workspace_slug,
            is_default=pending.workspace_is_default,
        )
        await self._ensure(workspace)  # it may have idle-stopped mid-consent
        client = self._sandbox(workspace)
        try:
            result = await client.exec(
                argv=[
                    "gog",
                    "auth",
                    "add",
                    "--remote",
                    "--step",
                    "2",
                    "--auth-url",
                    f"{pending.redirect_uri}?{raw_query}",
                    "--redirect-uri",
                    pending.redirect_uri,
                    "--services",
                    pending.services,
                    "--force-consent",
                    pending.email,
                ],
                timeout_s=_EXEC_TIMEOUT_EXCHANGE_S,
            )
            error = "" if result.exit_code == 0 else _tail(result)
        except SandboxError as exc:
            error = f"workspace sandbox unreachable during the token exchange: {exc}"
        finally:
            await client.aclose()
        pending.status = _STATUS_COMPLETED if not error else _STATUS_FAILED
        pending.error = error
        await self._save_outcome(pending)
        if not error:
            await self._publish_auth_changed(pending.workspace_id)
            return CallbackOutcome(
                status="completed",
                message=(
                    f"{pending.email} is now connected to the "
                    f"{pending.workspace_name!r} workspace. Every agent "
                    "attached to it can use the account."
                ),
                workspace_name=pending.workspace_name,
                email=pending.email,
            )
        logger.warning(
            "gog-auth: token exchange failed for workspace %s: %s",
            pending.workspace_id,
            error,
        )
        return CallbackOutcome(
            status="failed",
            message=f"Connecting the account failed: {error}",
            workspace_name=pending.workspace_name,
            email=pending.email,
        )

    async def cancel_pending(self, workspace_id: int) -> None:
        """Clear this workspace's pending/outcome record (the UI lock's
        cancel + dismiss affordance). Clearing another workspace's live flow
        is refused — its own panel owns it."""
        pending = await self._read_pending()
        if pending is None:
            return
        if pending.workspace_id != workspace_id:
            if pending.status == _STATUS_AWAITING:
                raise GogAuthBusyError(pending)
            return
        await self._delete_pending()

    async def disconnect(self, workspace: WorkspaceRef, email: str) -> None:
        """Remove a stored account from the workspace's keyring."""
        email = email.strip().lower()
        if not _EMAIL_RE.match(email):
            raise GogPreconditionError(f"{email!r} does not look like an email address")
        await self._ensure(workspace)
        client = self._sandbox(workspace)
        try:
            result = await client.exec(
                argv=["gog", "auth", "remove", email, "--force"],
                timeout_s=_EXEC_TIMEOUT_FAST_S,
            )
        except SandboxUnavailableError as exc:
            raise GogSandboxUnreachableError(
                f"workspace sandbox unreachable: {exc}"
            ) from exc
        finally:
            await client.aclose()
        if result.exit_code != 0:
            raise GogAuthError(f"gog auth remove failed: {_tail(result)}")
        await self._publish_auth_changed(workspace.id)


# --- Process-wide singleton (the manager pattern) -----------------------------

_service: WorkspaceGogAuthService | None = None


def get_workspace_gog_auth_service() -> WorkspaceGogAuthService:
    global _service
    if _service is None:
        _service = WorkspaceGogAuthService()
    return _service


def set_workspace_gog_auth_service(service: WorkspaceGogAuthService | None) -> None:
    """Test seam: inject a fake (or ``None`` to reset to the lazy default)."""
    global _service
    _service = service


__all__ = [
    "CALLBACK_PATH",
    "DEFAULT_SERVICES",
    "OUTCOME_TTL_SECONDS",
    "PENDING_KEY",
    "PENDING_TTL_SECONDS",
    "AccountsView",
    "CallbackOutcome",
    "GogAccount",
    "GogAuthBusyError",
    "GogAuthError",
    "GogPreconditionError",
    "GogSandboxUnreachableError",
    "PendingAuth",
    "WorkspaceGogAuthService",
    "WorkspaceRef",
    "get_workspace_gog_auth_service",
    "set_workspace_gog_auth_service",
]
