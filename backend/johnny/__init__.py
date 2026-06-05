"""Johnny — meet-worker runtime package.

Houses code that runs inside the per-session meet-worker container
(Playwright + Chromium + Xvfb + PulseAudio). Kept distinct from the
API backend (`app.*`) so the meet-worker image can ship without the
FastAPI/SQLAlchemy stack.
"""

__version__ = "0.1.0"
