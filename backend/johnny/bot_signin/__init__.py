"""Bot sign-in container runtime (Johnny-105).

In-container supervisor that drives Playwright Chromium under Xvfb
toward a signed-in Google session, then persists the resulting
storage_state.json plus a marker file onto a shared volume so the
API process can finalise the flow (move the file into
``google_auth_state``, attach to an account row).

The companion API + launcher live in ``app.services.bot_signin`` and
``app.services.bot_signin_launcher``; the FastAPI router that exposes
the flow to the browser lives in ``app.api.bot_signin``.
"""
