"""``python -m johnny.meet_worker`` entry point.

Delegates to :func:`johnny.meet_worker.bootstrap.main` so the Dockerfile
CMD stays a one-liner. The bootstrap configures logging, validates env
vars, drives the join flow, idles in-meeting, and translates everything
to a process exit code the container monitor pass understands.
"""

from __future__ import annotations

import sys

from johnny.meet_worker.bootstrap import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
