"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.calendar import router as calendar_router
from app.api.meeting_configs import router as meeting_configs_router
from app.api.providers import router as providers_router
from app.api.sessions import router as sessions_router
from app.api.templates import router as templates_router
from app.api.ws import router as ws_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run seeders on startup and wire the container launcher.

    The launcher choice is governed by ``JOHNNY_USE_DOCKER_LAUNCHER``;
    when unset, the API stays with the no-op launcher (matches the test
    runner / local dev experience). When set, the Docker SDK launcher
    is used so manual ``/sessions/start`` calls actually spawn a worker.
    """
    try:
        from app.db.session import session_scope
        from app.services.templates import seed_initial_templates

        with session_scope() as session:
            seed_initial_templates(session)
    except Exception as exc:  # noqa: BLE001 — seeding must never crash boot
        logger.warning("template seeding skipped: %s", exc)

    try:
        from app.api.sessions import set_launcher
        from app.services.docker_launcher import (
            DockerContainerLauncher,
            should_use_docker_launcher,
        )

        if should_use_docker_launcher():
            set_launcher(DockerContainerLauncher())
            logger.info("DockerContainerLauncher wired into /sessions API")
    except Exception as exc:  # noqa: BLE001 — launcher wiring must not crash boot
        logger.warning("docker launcher wiring skipped: %s", exc)

    yield


app = FastAPI(title="Johnny", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(calendar_router)
app.include_router(meeting_configs_router)
app.include_router(providers_router)
app.include_router(sessions_router)
app.include_router(templates_router)
app.include_router(ws_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
