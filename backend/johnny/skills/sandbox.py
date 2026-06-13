"""HTTP client for the skills-sandbox exec API (Johnny-trt.35 service).

The ``skills-sandbox`` compose service runs ``sandbox/execd.py`` — an
internal-only HTTP daemon (no published ports; reachable from api / worker /
agent-worker over the compose network exclusively). This client is the one
place Johnny talks to it:

* ``POST /exec`` — run one command *inside the sandbox container* with the
  daemon's timeout ceiling and per-stream output caps;
* ``GET /bins`` — resolve which binaries exist in the sandbox, used by the
  skill loader's eligibility gate (``requires.bins`` is checked INSIDE the
  sandbox, never against the api/worker image — the containers deliberately
  differ).

Error split: :class:`SandboxRequestError` means the daemon understood us and
said no (HTTP 4xx — oversized body, timeout over the cap, bad cwd);
:class:`SandboxUnavailableError` means we never got a verdict (connect /
read failure). Callers map both to honest speech, never a dead promise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_SANDBOX_URL = "http://skills-sandbox:8088"
DEFAULT_SKILLS_DIR = "/skills"

SANDBOX_URL_ENV = "JOHNNY_SKILLS_SANDBOX_URL"
SKILLS_DIR_ENV = "JOHNNY_SKILLS_DIR"

# Margin added on top of a command's own timeout so the daemon's SIGKILL —
# not the HTTP client — ends a runaway command and we still receive the
# structured timed_out reply.
_CLIENT_TIMEOUT_MARGIN_S = 10.0


def sandbox_url_from_env() -> str:
    """The exec API base URL (compose sets it for api/worker/agent-worker)."""
    return os.environ.get(SANDBOX_URL_ENV, "").strip().rstrip("/") or DEFAULT_SANDBOX_URL


# --- Per-workspace endpoints (Johnny-wks.1) ----------------------------------
# A NON-DEFAULT workspace's exec daemon lives in its own container of the
# same skills-sandbox image, reachable on the compose network under a
# canonical hostname derived from the workspace id. Those containers are
# LAZY: ``app.services.workspace_containers`` starts one with this exact
# name (and the johnny.workspace-id label) on first dispatch/claim and
# idle-stops it after a TTL (Johnny-wks.2). Between launches — or when a
# launch fails — the hostname resolves to nothing and every probe/exec
# degrades through SandboxUnavailableError — an empty capability snapshot
# for that key, never a crash (the documented containerless-workspace path).
WORKSPACE_CONTAINER_PREFIX = "johnny-workspace"
WORKSPACE_SANDBOX_PORT = 8088


def workspace_container_name(workspace_id: int) -> str:
    """Canonical container/hostname for a workspace's sandbox instance."""
    return f"{WORKSPACE_CONTAINER_PREFIX}-{workspace_id}"


def sandbox_url_for_workspace(workspace_id: int) -> str:
    """The exec API base URL for a workspace's own lazily-launched container.

    EVERY workspace with a real identity stamp routes here now — the default
    workspace (id 1) included (Johnny-etu.5: the default is lazy-launched like
    finance/ops, ``johnny-workspace-1``, instead of special-cased to the
    always-on ``skills-sandbox`` compose service). Only a TRULY stampless
    config (a pre-workspaces snapshot / claim with no ``workspace_id``) falls
    back to :func:`sandbox_url_from_env`; the resolver seams own that gate.
    """
    return f"http://{workspace_container_name(workspace_id)}:{WORKSPACE_SANDBOX_PORT}"


def skills_dir_from_env() -> str:
    """The skill-packages directory — the same ``/skills`` path in every
    container that mounts the volume (no per-container path translation)."""
    return os.environ.get(SKILLS_DIR_ENV, "").strip() or DEFAULT_SKILLS_DIR


# --- Per-workspace skill packages (Johnny-wks.3) ------------------------------
# A NON-DEFAULT workspace's skills live in its OWN host directory
# (~/.johnny/workspaces/<slug>/skills — the epic's storage convention),
# mounted read-only at /skills inside that workspace's container. The same
# package therefore works unchanged in any workspace: a SKILL.md argv like
# ``bash /skills/<name>/run.sh`` resolves against whichever skills dir the
# executing container mounts. api/worker (discovery + the install flow) see
# every workspace's dir through one parent mount, rooted at
# ``JOHNNY_WORKSPACES_DIR`` (/workspaces in compose). The DEFAULT workspace
# keeps :func:`skills_dir_from_env` — the pre-workspaces shared volume,
# byte-identical.
WORKSPACES_DIR_ENV = "JOHNNY_WORKSPACES_DIR"
DEFAULT_WORKSPACES_DIR = "/workspaces"
WORKSPACE_SKILLS_SUBDIR = "skills"


def workspaces_dir_from_env() -> str:
    """The container-side root under which per-workspace dirs live
    (``/workspaces/<slug>/...`` as seen by api / worker / agent-worker)."""
    return (
        os.environ.get(WORKSPACES_DIR_ENV, "").strip().rstrip("/")
        or DEFAULT_WORKSPACES_DIR
    )


def workspace_skills_dir(slug: str) -> str:
    """Where a NON-DEFAULT workspace's skill packages are discovered/installed.

    Keyed by the FROZEN slug (the wks.1 storage-identity decision): renames
    never re-key skills, and a workspace recreated with the same slug regains
    them — the same continuity-by-design the wks.2 state volume documents.
    Inside the workspace's own container this directory is ``/skills``.
    """
    return f"{workspaces_dir_from_env()}/{slug}/{WORKSPACE_SKILLS_SUBDIR}"


# --- Per-workspace gog state (Johnny-wks.4) ------------------------------------
# A NON-DEFAULT workspace's Google identity (gog's config, OAuth client
# credentials, and file keyring with the refresh tokens) lives in its OWN
# host directory — ``~/.johnny/workspaces/<slug>/gog`` — bind-mounted into
# that workspace's container and announced to every process there via
# ``GOG_HOME``. The host bind (not the wks.2 state volume) is the operator's
# storage convention: auth survives idle-TTL restarts, ``./stop.sh``
# (``down -v``), and clean installs, and cross-workspace credential-absence
# checks are plain host-path checks. The DEFAULT workspace keeps gog's XDG
# layout under the ``~/.johnny/sandbox-home`` bind, byte-identical.
WORKSPACE_GOG_SUBDIR = "gog"


def workspace_gog_dir(slug: str) -> str:
    """A NON-DEFAULT workspace's gog state dir, as seen by api / worker.

    Slug-keyed like :func:`workspace_skills_dir` and for the same reason:
    renames never re-key credentials, and recreating a deleted workspace
    with the same slug regains them unless the operator chose state removal
    at delete time.
    """
    return f"{workspaces_dir_from_env()}/{slug}/{WORKSPACE_GOG_SUBDIR}"


class SandboxError(Exception):
    """Base class for sandbox client failures."""


class SandboxUnavailableError(SandboxError):
    """The sandbox could not be reached (or returned a non-JSON 5xx)."""


class SandboxRequestError(SandboxError):
    """The sandbox rejected the request (HTTP 4xx with an ``error`` body)."""


@dataclass(frozen=True, slots=True)
class SandboxExecResult:
    """One ``POST /exec`` reply — the daemon's wire shape, typed."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_ms: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SandboxExecResult:
        return cls(
            exit_code=int(payload.get("exit_code", -1)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            truncated=bool(payload.get("truncated", False)),
            stdout_truncated=bool(payload.get("stdout_truncated", False)),
            stderr_truncated=bool(payload.get("stderr_truncated", False)),
            timed_out=bool(payload.get("timed_out", False)),
            duration_ms=int(payload.get("duration_ms", 0)),
        )


class SandboxClient:
    """Thin async client over the exec daemon; one lazily-built httpx client.

    Construct once per session / worker pass and :meth:`aclose` at teardown.
    ``http_client`` may be injected for tests (e.g. ``httpx.AsyncClient``
    with a ``MockTransport``)."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or sandbox_url_from_env()).rstrip("/")
        self._client = http_client

    @property
    def base_url(self) -> str:
        return self._base_url

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def check_bins(self, names: list[str]) -> dict[str, bool]:
        """Resolve binaries inside the sandbox (``GET /bins``), batched.

        One call covers every name — the loader coalesces all skills'
        requirements into a single probe (the openclaw ``system.which``
        batching pattern); never call this per skill or per turn.
        """
        if not names:
            return {}
        try:
            response = await self._http().get(
                "/bins", params={"names": ",".join(names)}, timeout=10.0
            )
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError(f"sandbox /bins unreachable: {exc}") from exc
        payload = self._payload_or_raise(response)
        bins = payload.get("bins")
        if not isinstance(bins, dict):
            raise SandboxUnavailableError("sandbox /bins returned no 'bins' object")
        return {str(name): bool(present) for name, present in bins.items()}

    async def check_env(self, names: list[str]) -> dict[str, bool]:
        """Resolve which env vars are set inside the sandbox, batched.

        The availability-predicate analogue of :meth:`check_bins`
        (Johnny-trt.55): one ``POST /exec`` covers every ``requires.env``
        name any skill declares — never per skill, never on the turn hot
        path. ``printenv -- "$v"`` keeps arbitrary names safe (no shell
        interpolation of the values); set-but-empty counts as set.
        """
        if not names:
            return {}
        script = (
            'for v in "$@"; do '
            'if printenv -- "$v" >/dev/null 2>&1; then echo "$v=1"; else echo "$v=0"; fi; '
            "done"
        )
        result = await self.exec(
            argv=["sh", "-c", script, "envprobe", *names], timeout_s=10.0
        )
        if result.exit_code != 0:
            raise SandboxUnavailableError(
                f"sandbox env probe exited {result.exit_code}: {result.stderr[:200]}"
            )
        present: dict[str, bool] = {name: False for name in names}
        for line in result.stdout.splitlines():
            name, _, flag = line.rpartition("=")
            if name in present:
                present[name] = flag.strip() == "1"
        return present

    async def exec(
        self,
        *,
        argv: list[str] | None = None,
        cmd: str | None = None,
        timeout_s: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxExecResult:
        """Run one command inside the sandbox (``POST /exec``).

        Exactly one of ``argv`` / ``cmd`` — the daemon enforces it too. The
        HTTP read timeout rides ``timeout_s`` plus a margin so the daemon's
        process-group SIGKILL is what ends a runaway command and the
        structured ``timed_out`` reply still arrives.
        """
        body: dict[str, Any] = {}
        if argv is not None:
            body["argv"] = list(argv)
        if cmd is not None:
            body["cmd"] = cmd
        if timeout_s is not None:
            body["timeout"] = timeout_s
        if cwd is not None:
            body["cwd"] = cwd
        if env:
            body["env"] = dict(env)

        # The daemon default is 30s; mirror it for the client-side margin.
        effective_timeout = timeout_s if timeout_s is not None else 30.0
        try:
            response = await self._http().post(
                "/exec", json=body, timeout=effective_timeout + _CLIENT_TIMEOUT_MARGIN_S
            )
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError(f"sandbox /exec unreachable: {exc}") from exc
        return SandboxExecResult.from_payload(self._payload_or_raise(response))

    def _payload_or_raise(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SandboxUnavailableError(
                f"sandbox returned non-JSON (HTTP {response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise SandboxUnavailableError("sandbox returned a non-object JSON body")
        if response.status_code >= 500:
            raise SandboxUnavailableError(
                f"sandbox error (HTTP {response.status_code}): {payload.get('error', '')}"
            )
        if response.status_code >= 400:
            raise SandboxRequestError(
                str(payload.get("error", f"HTTP {response.status_code}"))
            )
        return payload


__all__ = [
    "DEFAULT_SANDBOX_URL",
    "DEFAULT_SKILLS_DIR",
    "DEFAULT_WORKSPACES_DIR",
    "SANDBOX_URL_ENV",
    "SKILLS_DIR_ENV",
    "WORKSPACES_DIR_ENV",
    "WORKSPACE_CONTAINER_PREFIX",
    "WORKSPACE_GOG_SUBDIR",
    "WORKSPACE_SANDBOX_PORT",
    "WORKSPACE_SKILLS_SUBDIR",
    "SandboxClient",
    "SandboxError",
    "SandboxExecResult",
    "SandboxRequestError",
    "SandboxUnavailableError",
    "sandbox_url_for_workspace",
    "sandbox_url_from_env",
    "skills_dir_from_env",
    "workspace_container_name",
    "workspace_gog_dir",
    "workspace_skills_dir",
    "workspaces_dir_from_env",
]
