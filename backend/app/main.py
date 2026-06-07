"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.bot_signin import router as bot_signin_router
from app.api.bot_signin import ws_router as bot_signin_ws_router
from app.api.browser_sessions import router as browser_sessions_router
from app.api.browser_sessions import ws_router as browser_sessions_ws_router
from app.api.calendar import router as calendar_router
from app.api.decisions import router as decisions_router
from app.api.history import router as history_router
from app.api.meeting_configs import router as meeting_configs_router
from app.api.providers import router as providers_router
from app.api.sessions import router as sessions_router
from app.api.stt_stream import router as stt_stream_router
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
    # Run migrations + abort on model/DB drift BEFORE anything queries
    # an ORM-mapped table. Johnny-ckz.9: shipping a model change without
    # the matching migration used to surface as a 500 on the first
    # `/sessions/active` request; the drift check turns it into a loud
    # boot-time crash. Exceptions intentionally propagate (no try/except)
    # so the container restarts and the operator sees the real error.
    from app.db.bootstrap import bootstrap as db_bootstrap

    db_bootstrap()

    try:
        from app.db.session import session_scope
        from app.services.templates import seed_initial_templates

        with session_scope() as session:
            seed_initial_templates(session)
    except Exception as exc:  # noqa: BLE001 — seeding must never crash boot
        logger.warning("template seeding skipped: %s", exc)

    try:
        from app.db.session import session_scope
        from app.security.crypto import get_crypto
        from app.services.providers_seed import seed_providers_from_file

        with session_scope() as session:
            seed_providers_from_file(session, get_crypto())
    except Exception as exc:  # noqa: BLE001 — seeding must never crash boot
        logger.warning("providers seeding skipped: %s", exc)

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

    # Runtime-installed Parakeet packages (Johnny-stt.1). Empty bind-
    # mount is a no-op; if the operator has clicked Install, the dir
    # contains nemo+torch and gets appended to sys.path so the Parakeet
    # adapter can import without a container rebuild.
    try:
        from app.services.parakeet_packages import (
            get_packages_dir,
            register_sys_path,
        )

        if register_sys_path():
            logger.info("parakeet packages on sys.path: %s", get_packages_dir())
    except Exception as exc:  # noqa: BLE001 — sys.path wiring must not crash boot
        logger.warning("parakeet packages sys.path wiring skipped: %s", exc)

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
app.include_router(bot_signin_router)
app.include_router(bot_signin_ws_router)
app.include_router(browser_sessions_router)
app.include_router(browser_sessions_ws_router)
app.include_router(calendar_router)
app.include_router(decisions_router)
app.include_router(history_router)
app.include_router(meeting_configs_router)
app.include_router(providers_router)
app.include_router(sessions_router)
app.include_router(stt_stream_router)
app.include_router(templates_router)
app.include_router(ws_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
