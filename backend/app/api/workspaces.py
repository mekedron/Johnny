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
from app.services.workspaces import count_attached_agents, derive_unique_slug

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


# --- Helpers ---------------------------------------------------------------


def _get_row_or_404(session: Session, workspace_id: int) -> Workspace:
    row = session.get(Workspace, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return row


def _workspace_read(session: Session, row: Workspace) -> WorkspaceRead:
    read = WorkspaceRead.model_validate(row)
    read.agent_count = count_attached_agents(session, row)
    return read


def _name_conflict(name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"a workspace named {name!r} already exists. Pick a different name.",
    )


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
    from app.services.docker_launcher import should_use_docker_launcher
    from app.services.workspace_containers import (
        WorkspaceContainerError,
        get_workspace_container_manager,
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


__all__ = ["WorkspaceCreate", "WorkspaceRead", "WorkspaceUpdate", "router"]
