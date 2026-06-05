"""Background worker process.

Placeholder until a real task queue (Celery/Dramatiq) lands in
US-007 and US-029. The process keeps the worker container alive,
imports application config (surfacing errors early), and writes a
heartbeat file consumed by the container healthcheck.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.config import get_settings

HEARTBEAT_PATH = Path("/var/lib/johnny/worker/heartbeat")
INTERVAL_SECONDS = 5

logger = logging.getLogger(__name__)


def write_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    logger.info(
        "worker starting; database_url=%s redis_url=%s",
        settings.database_url,
        settings.redis_url,
    )

    while True:
        write_heartbeat()
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
