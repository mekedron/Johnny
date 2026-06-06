"""Test-suite-wide setup.

Disables :func:`app.db.bootstrap.bootstrap` for every test that boots
the FastAPI app via ``TestClient``. The bootstrap connects to a live
Postgres (Johnny-ckz.9) which is unavailable in the host test runner —
each integration test rebuilds its own SQLite in-memory schema via
``Base.metadata.create_all`` and overrides the relevant FastAPI
dependencies, so the bootstrap pass would only crash on a hostname
that isn't reachable from the host.

Setting the env var before any ``app.*`` import keeps the override
in effect across every collected test, including those that capture
``app.main.app`` at import time.
"""

from __future__ import annotations

import os

os.environ.setdefault("JOHNNY_DB_BOOTSTRAP", "off")
