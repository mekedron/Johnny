"""Workspace entity services — seeding, attachment resolution, snapshot stamp (Johnny-wks.1).

A WORKSPACE is a named execution environment (container instance of the
skills-sandbox image + host state dir + connected accounts) that agents
attach to via ``agents.workspace_id``. This module owns the pieces the
dispatch surfaces and the CRUD API share:

* :func:`seed_default_workspace` — insert the canonical non-deletable
  "Default" workspace when none exists (boot-time belt-and-braces over the
  0032 migration seed, mirroring :func:`app.services.agents.seed_default_agent`).
* :func:`resolve_agent_workspace` — the effective attachment for an agent:
  its ``workspace_id`` row, or the default workspace when ``NULL`` (the
  provider-pin NULL-inherits convention).
* :func:`workspace_snapshot_payload` — the identity blob stamped into the
  frozen agent snapshot at dispatch (``build_agent_snapshot``), so turn-time
  code and the worker resolver key the sandbox by WORKSPACE ID without ever
  re-reading these tables (the trt.41 no-turn-time-DB-reads rule).
* :func:`slugify` / :func:`derive_unique_slug` — the frozen human-readable
  identity key. Slugs are FROZEN at creation: they label the workspace's
  container and named state volume (Johnny-wks.2 — the volume itself is
  keyed by the never-reused id, ``johnny-workspace-<id>-home``), and a
  rename must never re-key state.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models import Agent, Workspace

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_NAME = "Default"
DEFAULT_WORKSPACE_SLUG = "default"
DEFAULT_WORKSPACE_DESCRIPTION = (
    "The shared execution environment every agent starts on — today's "
    "skills-sandbox container and its connected accounts. Non-deletable."
)

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 64


def slugify(name: str) -> str:
    """Lowercase-kebab the display name into a label-safe identity key.

    ``"Finance Team"`` → ``finance-team``. Non-alphanumeric runs collapse to
    one hyphen; an all-symbols name degrades to ``"workspace"`` rather than
    an empty slug (container/volume labels must always have a value).
    """
    slug = _SLUG_STRIP_RE.sub("-", name.lower()).strip("-")
    return slug[:_SLUG_MAX_LEN].rstrip("-") or "workspace"


def derive_unique_slug(session: Session, name: str) -> str:
    """The slug for a NEW workspace, disambiguated against existing rows.

    The slug labels the workspace's container + state volume, so collisions
    are forbidden even when the display names differ only in symbols
    (``"Team A"`` vs ``"Team-A"``). First taken candidate gets a numeric
    suffix (``finance-2``), the agents clone-name pattern.
    """
    base = slugify(name)
    existing = set(session.scalars(select(Workspace.slug)).all())
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def select_default_workspace(session: Session) -> Workspace | None:
    """The single ``is_default`` workspace, or ``None`` on an unseeded schema."""
    return session.scalar(select(Workspace).where(Workspace.is_default.is_(True)))


def seed_default_workspace(session: Session) -> Workspace | None:
    """Insert the canonical "Default" workspace when no default exists.

    Boot-time insurance over the 0032 migration seed: a stripped test schema
    still gets the default so attachment resolution always lands somewhere.
    Returns the created row, or ``None`` when a default already exists
    (existing rows — including renames — are never touched). Commits on
    insert so the row is durable outside a request lifecycle (the
    default-agent seeder's contract).
    """
    if select_default_workspace(session) is not None:
        return None
    row = Workspace(
        name=DEFAULT_WORKSPACE_NAME,
        slug=DEFAULT_WORKSPACE_SLUG,
        description=DEFAULT_WORKSPACE_DESCRIPTION,
        is_default=True,
    )
    session.add(row)
    session.commit()
    logger.info("seeded default workspace %r", row.name)
    return row


def resolve_agent_workspace(session: Session, agent: Agent | None) -> Workspace | None:
    """The workspace this agent's work executes in (Johnny-wks.1).

    ``agent.workspace_id`` when set; the default workspace otherwise
    (``NULL`` = attached-to-default, so pre-workspaces agents behave
    byte-identically). ``None`` only on an unseeded schema with no default —
    callers then omit the snapshot stamp and downstream resolvers degrade to
    the global sandbox (the legacy-snapshot path).

    A dangling ``workspace_id`` (deleted row — the RESTRICT FK should make
    this impossible) logs and falls back to the default rather than failing
    a dispatch.
    """
    if agent is not None and agent.workspace_id is not None:
        row = session.get(Workspace, agent.workspace_id)
        if row is not None:
            return row
        logger.warning(
            "agent id=%s references workspace_id=%s which no longer exists; "
            "falling back to the default workspace",
            agent.id,
            agent.workspace_id,
        )
    return select_default_workspace(session)


def workspace_snapshot_payload(workspace: Workspace) -> dict[str, Any]:
    """The identity blob ``build_agent_snapshot`` stamps at dispatch.

    Plain JSON-able types only. ``is_default`` is what the resolver seams
    key on (default → the global skills-sandbox URL, byte-identical to
    pre-workspaces dispatches); ``id`` is the per-workspace endpoint key;
    ``name``/``slug`` ride for rendering and diagnostics.
    """
    return {
        "id": int(workspace.id),
        "name": workspace.name,
        "slug": workspace.slug,
        "is_default": bool(workspace.is_default),
    }


def count_attached_agents(session: Session, workspace: Workspace) -> int:
    """How many agents EFFECTIVELY run in this workspace.

    Explicit attachments (``workspace_id = id``) for every workspace; the
    default additionally counts the ``NULL``-attached agents (they run there
    by convention). The delete endpoint blocks on explicit attachments only
    — the default is non-deletable regardless, so the broader count is
    display truth for the UI, not the delete rule.
    """
    from sqlalchemy import func, or_

    condition = Agent.workspace_id == workspace.id
    if workspace.is_default:
        condition = or_(condition, Agent.workspace_id.is_(None))
    return int(
        session.scalar(select(func.count()).select_from(Agent).where(condition)) or 0
    )


__all__ = [
    "DEFAULT_WORKSPACE_DESCRIPTION",
    "DEFAULT_WORKSPACE_NAME",
    "DEFAULT_WORKSPACE_SLUG",
    "count_attached_agents",
    "derive_unique_slug",
    "resolve_agent_workspace",
    "seed_default_workspace",
    "select_default_workspace",
    "slugify",
    "workspace_snapshot_payload",
]
