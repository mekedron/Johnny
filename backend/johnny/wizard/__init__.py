"""Johnny interactive setup wizard (Johnny-61y).

The wizard is the friendlier alternative to ``docs/SETUP_LOCAL.md``:
instead of copy-pasting commands, the user answers prompts and the wizard
does the work. It is invokable as either::

    uv run python -m johnny.wizard
    uv run johnny-setup

The wizard runs from the project root (Johnny's repo top-level), so it
expects ``.env.example``, ``docker-compose.yml``, and ``backend/`` to be
visible relative to its working directory. Pass ``--project-root`` to
override.

The module structure splits concerns:

* :mod:`johnny.wizard.cli` — Click entrypoint, top-level orchestration.
* :mod:`johnny.wizard.state` — re-runnable-state tracking. Reads ``.env``
  + queries the running API to decide what to skip.
* :mod:`johnny.wizard.env_file` — read / write / merge ``.env`` files.
* :mod:`johnny.wizard.prereqs` — detect Docker, ``uv``, ``pnpm``, disk
  space, GPU presence and report missing pieces with install URLs.
* :mod:`johnny.wizard.providers` — provider catalog (display names,
  signup URLs, default options) shared between cloud/local paths.
* :mod:`johnny.wizard.models` — local-model download orchestration
  (faster-whisper, Piper, Ollama).
* :mod:`johnny.wizard.api_client` — thin httpx wrapper for ``/providers``
  and ``/health`` calls.
* :mod:`johnny.wizard.compose` — shell-out helpers to bring the Compose
  stack up / wait for ``/health``.
* :mod:`johnny.wizard.steps` — one function per wizard phase, glued by
  :mod:`johnny.wizard.cli`.
"""

__all__: list[str] = []
