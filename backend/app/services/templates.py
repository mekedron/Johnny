"""Profile template seeding (US-010).

Two example templates are inserted on first run so the user has something
to apply immediately:

* **Listen-only standup** — pure transcription, no router, no speaking.
* **Approval-required client call** — router runs and asks for approval
  before Johnny speaks.

Seeding is idempotent: each row is inserted only if a template with the
same ``name`` does not already exist. Existing names are left untouched —
users may edit the seeded rows and re-running the seeder must not undo
those edits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.db.models import BotMode, ProfileTemplate

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TemplateSeed:
    name: str
    mode: BotMode
    base_instructions: str
    base_context: str
    allowed_replies: tuple[str, ...]
    confidence_threshold: float = 0.7


_SEEDS: tuple[_TemplateSeed, ...] = (
    _TemplateSeed(
        name="Listen-only standup",
        mode=BotMode.LISTEN_ONLY,
        base_instructions=(
            "Transcribe the meeting silently. Do not speak. Capture the standup "
            "updates so the user can review them later."
        ),
        base_context=(
            "Daily team standup format: each person shares yesterday's progress, "
            "today's plan, and any blockers."
        ),
        allowed_replies=(),
        confidence_threshold=0.7,
    ),
    _TemplateSeed(
        name="Approval-required client call",
        mode=BotMode.APPROVAL_REQUIRED,
        base_instructions=(
            "Listen carefully. When a clear question is asked or a clarification "
            "is needed, propose a concise reply and ask the user to approve "
            "before speaking. Never speak without explicit approval."
        ),
        base_context=(
            "External client meeting — tone is professional. Errors are costly, "
            "so default to caution and ask for approval before contributing."
        ),
        allowed_replies=(),
        confidence_threshold=0.75,
    ),
)


def seed_initial_templates(session: Session) -> list[ProfileTemplate]:
    """Insert the canonical example templates if they don't already exist.

    Returns the list of newly inserted rows (empty list when everything was
    already present). Commits the session so the rows are durable even when
    called outside a request lifecycle.
    """
    from sqlalchemy import select

    existing_names = set(
        session.scalars(select(ProfileTemplate.name)).all()
    )
    created: list[ProfileTemplate] = []
    for seed in _SEEDS:
        if seed.name in existing_names:
            continue
        row = ProfileTemplate(
            name=seed.name,
            mode=seed.mode,
            base_instructions=seed.base_instructions,
            base_context=seed.base_context,
            allowed_replies=list(seed.allowed_replies),
            confidence_threshold=seed.confidence_threshold,
        )
        session.add(row)
        created.append(row)
    if created:
        session.commit()
        logger.info("seeded %d profile template(s)", len(created))
    return created


__all__ = ["seed_initial_templates"]
