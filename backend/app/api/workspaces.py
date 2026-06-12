"""Workspace CRUD HTTP endpoints (Johnny-wks.1).

Manages rows in ``workspaces``: list / create / read / rename / delete. A
workspace is a named execution environment (skills-sandbox container + host
state dir + connected accounts) that agents attach to via
``agents.workspace_id`` (the agents API owns that field; this module owns
only the workspace rows themselves).

Rules (the bead's CRUD matrix):

* names are unique (409) — they double as the operator's mental key;
* the ``slug`` (the frozen human-readable identity key) is derived from the
  name at creation and FROZEN — renames change the display name only, never
  the slug, which rides the workspace's container/volume labels
  (Johnny-wks.2) and must stay stable for state triage;
* the seeded default workspace is non-deletable (409);
* deleting any workspace is refused while agents are attached to it (409,
  with the count — detach or delete the agents first). The FK's RESTRICT
  is the belt-and-braces under this check.

Container lifecycle (Johnny-wks.2): creating a workspace creates the row
only — its sandbox container (``johnny-workspace-<id>``) launches LAZILY on
first need via :mod:`app.services.workspace_containers`. Deletion always
retires the container; the named state volume is removed only on the
explicit ``?remove_volume=true`` opt-in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import Agent, Workspace
from app.services.docker_launcher import should_use_docker_launcher
from app.services.workspace_containers import (
    WORKSPACE_STATE_RUNNING,
    WorkspaceContainerError,
    get_workspace_container_manager,
)
from app.services.workspaces import (
    count_attached_agents,
    derive_unique_slug,
    workspace_storage_dir_display,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

SessionDep = Annotated[Session, Depends(get_session)]


# --- Pydantic schemas ------------------------------------------------------


class WorkspaceCreate(BaseModel):
    """Payload for creating a workspace. Always created non-default."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class WorkspaceUpdate(BaseModel):
    """Patch payload — rename and/or re-describe.

    The slug is intentionally NOT patchable (frozen storage identity), and
    ``is_default`` is not patchable either — the default is the seeded row,
    not a promotable flag.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None


class WorkspaceRead(BaseModel):
    """Public view of a workspace row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str | None
    is_default: bool
    created_at: datetime
    updated_at: datetime
    # How many agents effectively run here (explicit attachments; the
    # default also counts NULL-attached agents). The UI warns before delete
    # and explains the 409.
    agent_count: int = 0
    # Operator-facing host path of the workspace's state dir (skills, gog
    # keyring) — the wks.3/wks.4 storage convention. None for the default
    # workspace (its state is the always-on sandbox's volume, not a dir).
    storage_dir: str | None = None


class WorkspaceContainerStatesRead(BaseModel):
    """Bulk container state for the workspaces list (Johnny-wks.5).

    ``available=False`` means state could not be determined at all (docker
    isn't driven here, or the daemon refused) — the UI says "unavailable"
    instead of guessing. ``states`` carries every NON-default workspace id →
    running / stopped / never-started; the default workspace is deliberately
    absent (its sandbox is the always-on compose service, ``managed`` —
    lifecycle belongs to ./run.sh, not this launcher).
    """

    available: bool
    reason: str = ""
    states: dict[int, str] = Field(default_factory=dict)


class WorkspaceContainerActionRead(BaseModel):
    """Outcome of a manual start/stop — the workspace's resulting state."""

    workspace_id: int
    state: str


# --- Helpers ---------------------------------------------------------------


def _get_row_or_404(session: Session, workspace_id: int) -> Workspace:
    row = session.get(Workspace, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return row


def _workspace_read(session: Session, row: Workspace) -> WorkspaceRead:
    read = WorkspaceRead.model_validate(row)
    read.agent_count = count_attached_agents(session, row)
    read.storage_dir = workspace_storage_dir_display(row)
    return read


_CONTAINERS_UNMANAGED_DETAIL = (
    "container management is unavailable: this deployment does not drive "
    "docker (JOHNNY_USE_DOCKER_LAUNCHER is off)"
)


def _container_target_or_409(session: Session, workspace_id: int) -> Workspace:
    """The workspace whose container a manual start/stop may touch.

    404 for unknown rows; 409 for the default workspace (its sandbox is the
    always-on compose service — ./run.sh owns that lifecycle, and stopping
    it would take every NULL-attached agent down with it) and for
    deployments that don't drive docker.
    """
    row = _get_row_or_404(session, workspace_id)
    if row.is_default:
        raise HTTPException(
            status_code=409,
            detail=(
                "the default workspace's sandbox is the always-on compose "
                "service — its lifecycle is managed by ./run.sh / ./stop.sh, "
                "not from here"
            ),
        )
    if not should_use_docker_launcher():
        raise HTTPException(status_code=409, detail=_CONTAINERS_UNMANAGED_DETAIL)
    return row


def _name_conflict(name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"a workspace named {name!r} already exists. Pick a different name.",
    )


def _remove_workspace_gog_dir(row: Workspace) -> None:
    """Honor the explicit state-removal choice for the workspace's Google
    credentials too (Johnny-wks.4).

    The gog state (file keyring with refresh tokens) lives OUTSIDE the
    docker volume — ``~/.johnny/workspaces/<slug>/gog``, by the operator's
    storage convention — so ``remove_volume=true`` must remove it as well or
    the most sensitive state would silently survive an explicit "remove the
    state" request (and a future same-slug workspace would inherit it). The
    skills dir is left alone (inert packages; the whole-dir choice is the
    workspaces UI's affordance). A removal failure aborts with 409 and
    preserves the row, the same contract as a container/volume teardown
    failure.
    """
    import shutil
    from pathlib import Path

    from johnny.skills.sandbox import workspace_gog_dir, workspaces_dir_from_env

    root = Path(workspaces_dir_from_env()).resolve()
    target = Path(workspace_gog_dir(row.slug)).resolve()
    if root not in target.parents or not row.slug:
        return  # malformed slug/root — nothing safe to remove
    if not target.exists():
        return
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace {row.name!r}: its Google credential dir could "
                f"not be removed ({exc}). The workspace was NOT deleted; "
                "resolve the file permissions and retry."
            ),
        ) from exc


# --- Endpoints -------------------------------------------------------------


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(session: SessionDep) -> list[WorkspaceRead]:
    """List every workspace, the default first then alphabetical."""
    rows = session.scalars(
        select(Workspace).order_by(Workspace.is_default.desc(), Workspace.name, Workspace.id)
    ).all()
    return [_workspace_read(session, row) for row in rows]


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(payload: WorkspaceCreate, session: SessionDep) -> WorkspaceRead:
    """Create a new workspace (row only — its container is Johnny-wks.2)."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name must be non-empty",
        )
    row = Workspace(
        name=name,
        slug=derive_unique_slug(session, name),
        description=payload.description,
        is_default=False,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _name_conflict(name) from exc
    session.refresh(row)
    return _workspace_read(session, row)


# NOTE: registered before the /{workspace_id} routes — Starlette matches in
# declaration order and "containers" would otherwise be parsed as an id.
@router.get("/containers", response_model=WorkspaceContainerStatesRead)
def workspace_container_states(session: SessionDep) -> WorkspaceContainerStatesRead:
    """Container state per NON-default workspace, in one daemon round-trip.

    Degrades to ``available=False`` (never an error response) when this
    deployment doesn't drive docker or the daemon can't answer — the list
    page renders "state unavailable" and everything else still works.
    """
    if not should_use_docker_launcher():
        return WorkspaceContainerStatesRead(
            available=False, reason=_CONTAINERS_UNMANAGED_DETAIL
        )
    ids = list(
        session.scalars(select(Workspace.id).where(Workspace.is_default.is_(False)))
    )
    try:
        states = get_workspace_container_manager().container_states(ids)
    except WorkspaceContainerError as exc:
        return WorkspaceContainerStatesRead(available=False, reason=str(exc))
    return WorkspaceContainerStatesRead(available=True, states=states)


@router.post(
    "/{workspace_id}/container/start", response_model=WorkspaceContainerActionRead
)
async def start_workspace_container(
    workspace_id: int, session: SessionDep
) -> WorkspaceContainerActionRead:
    """Start (or transparently restart) the workspace's sandbox container.

    The same ensure the dispatch surfaces run lazily — started containers
    still fall under the idle-TTL sweep, so an unused workspace stops again
    on its own. ``ensure_running`` never raises; a ``False`` outcome is a
    502 with a pointer at the api logs (daemon/image trouble).
    """
    row = _container_target_or_409(session, workspace_id)
    manager = get_workspace_container_manager()
    ok = await manager.ensure_running(workspace_id=row.id, slug=row.slug)
    if not ok:
        raise HTTPException(
            status_code=502,
            detail=(
                f"workspace {row.name!r}: its sandbox container failed to "
                "start — check the api logs (docker daemon reachability, "
                "image availability)"
            ),
        )
    return WorkspaceContainerActionRead(
        workspace_id=row.id, state=WORKSPACE_STATE_RUNNING
    )


@router.post(
    "/{workspace_id}/container/stop", response_model=WorkspaceContainerActionRead
)
def stop_workspace_container(
    workspace_id: int, session: SessionDep
) -> WorkspaceContainerActionRead:
    """Stop+remove the workspace's container now (state stays in the volume).

    Idle-sweep semantics on demand; the next dispatch — or the Start button
    — brings the workspace back exactly as it was. A survivor (verify-or-
    raise) is a 409 so the operator retries instead of trusting a stop that
    didn't happen.
    """
    row = _container_target_or_409(session, workspace_id)
    manager = get_workspace_container_manager()
    try:
        manager.stop_container(workspace_id=row.id)
        state = manager.container_states([row.id]).get(row.id, "stopped")
    except WorkspaceContainerError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"workspace {row.name!r}: container stop failed: {exc}",
        ) from exc
    return WorkspaceContainerActionRead(workspace_id=row.id, state=state)


@router.get("/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(workspace_id: int, session: SessionDep) -> WorkspaceRead:
    """Read a single workspace."""
    return _workspace_read(session, _get_row_or_404(session, workspace_id))


@router.patch("/{workspace_id}", response_model=WorkspaceRead)
def update_workspace(
    workspace_id: int,
    payload: WorkspaceUpdate,
    session: SessionDep,
) -> WorkspaceRead:
    """Rename / re-describe a workspace. The slug never changes."""
    row = _get_row_or_404(session, workspace_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="name must be non-empty",
            )
        row.name = name
    if "description" in data:
        row.description = data["description"]
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _name_conflict(row.name) from exc
    session.refresh(row)
    return _workspace_read(session, row)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(
    workspace_id: int,
    session: SessionDep,
    remove_volume: bool = False,
) -> None:
    """Delete a workspace. Refuses the default and any attached workspace.

    The workspace's sandbox container (Johnny-wks.2) is always stopped and
    removed — a deleted row must not leave a live executor behind. Its named
    state volume (``johnny-workspace-<id>-home``) is NEVER auto-deleted:
    only the explicit ``?remove_volume=true`` opt-in removes it; otherwise
    the state stays recoverable via ``docker volume ls`` (the volume's
    labels carry the workspace id and slug). A container/volume teardown
    failure aborts with 409 and PRESERVES the row, so the operator can retry
    instead of stranding an orphan.
    """
    row = _get_row_or_404(session, workspace_id)
    if row.is_default:
        raise HTTPException(
            status_code=409,
            detail="cannot delete the default workspace — it is the shared "
            "execution environment agents fall back to",
        )
    attached = int(
        session.scalar(
            select(func.count()).select_from(Agent).where(Agent.workspace_id == row.id)
        )
        or 0
    )
    if attached:
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot delete workspace {row.name!r} — {attached} agent(s) "
                "are attached to it; reattach them first"
            ),
        )
    if should_use_docker_launcher():
        try:
            get_workspace_container_manager().retire(
                workspace_id=row.id, remove_volume=remove_volume
            )
        except WorkspaceContainerError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"workspace {row.name!r} could not be torn down cleanly: "
                    f"{exc}. The workspace was NOT deleted; retry once the "
                    "container/volume issue is resolved."
                ),
            ) from exc
        if remove_volume:
            _remove_workspace_gog_dir(row)
    elif remove_volume:
        # Honor-or-refuse: this deployment doesn't drive docker, so the
        # explicit request can't be carried out — refusing beats silently
        # deleting the row and leaving the volume behind.
        raise HTTPException(
            status_code=409,
            detail=(
                "state-volume removal is unavailable: this deployment does "
                "not manage docker containers (JOHNNY_USE_DOCKER_LAUNCHER is "
                "off). Delete without remove_volume, or remove the volume "
                "manually."
            ),
        )
    session.delete(row)


__all__ = [
    "WorkspaceContainerActionRead",
    "WorkspaceContainerStatesRead",
    "WorkspaceCreate",
    "WorkspaceRead",
    "WorkspaceUpdate",
    "router",
]
