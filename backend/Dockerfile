# Backend image: FastAPI app served by uvicorn; also the worker entrypoint
# (compose overrides CMD to `python -m app.worker`). Dependencies managed by uv.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # LiveKit Agents model cache (epic Johnny-7g5). `download-files` writes
    # the Silero VAD + multilingual turn-detector artifacts here at build
    # time and the runtime reads them back from the same paths. Kept OUTSIDE
    # /workspace and /opt/venv on purpose: the run-dev.sh source bind mount
    # (./backend:/workspace) and `uv sync` must never shadow the baked
    # weights, otherwise first launch would re-fetch them over the network.
    HF_HOME=/opt/livekit-models/huggingface \
    TORCH_HOME=/opt/livekit-models/torch

RUN pip install --no-cache-dir uv==0.11.19

WORKDIR /workspace

# Install runtime dependencies first so the layer is reused unless deps change.
# `--extra local-stt` pulls in faster-whisper, `--extra local-tts` pulls
# in piper-tts so the providers `/test` endpoint can run real smoke
# calls against on-device STT and TTS; without them the smoke calls die
# on `import faster_whisper` / "piper binary not found". `--extra neural-tts`
# bakes in Kokoro + KittenTTS + misaki[en] so the `in-container` runtime of
# those TTS providers works on a clean boot with no manual install step
# (Johnny-1ge.11; torch is pinned CPU-only via [tool.uv] in pyproject so this
# stays ~1.5 GB, not ~5 GB). The meet-worker image installs its own pinned
# copies via pip; this layer covers the api/worker.
#
# The `dev` dependency-group (pytest, pytest-asyncio, ruff, mypy, aiosqlite)
# is intentionally LEFT IN — note the absence of `--no-dev`. CLAUDE.md /
# AGENTS.md make `docker compose exec api pytest` (and `ruff` / `mypy`) THE
# quality gates, run against this same image; "Docker is the only runtime"
# means the operator never installs test tooling on the host. Installing the
# group here (in the cached deps layer) keeps the documented gate working on
# a clean `./run.sh` and stops the recurring failure mode of agents
# hot-patching `uv pip install pytest` into the running container — a change
# that vanishes on the next `./stop.sh` (Johnny-012). The group is
# pure-Python + the ruff/mypy binaries (~60 MB total), negligible next to the
# baked ML deps.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project \
    --extra local-stt --extra local-tts --extra neural-tts --extra agent

# Bake the LiveKit Agents model files (Silero VAD + multilingual turn
# detector) into the image so a clean `./run.sh` works with NO network
# (clean-install contract, AGENTS.md). `python -m livekit.agents
# download-files` discovers the installed livekit-plugins-* packages and
# fetches their model artifacts into HF_HOME / TORCH_HOME (set above to a
# path outside /workspace + /opt/venv). This RUN sits before the source
# COPY so the heavy model-download layer stays cached across code-only
# changes (epic Johnny-7g5).
RUN python -m livekit.agents download-files

# Audio system libs used by NeMo's transitive deps when the user
# installs Parakeet at runtime (Johnny-stt.1). `libsndfile1` is
# soundfile's libsndfile; `ffmpeg` is what pydub falls back to for
# several audio I/O paths. Tiny (a few MB combined) so we bake them
# in even when Parakeet isn't installed — the runtime pip install
# itself happens via the `POST /providers/{id}/package/install`
# endpoint and lands in a host-bind-mounted dir, NOT in the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy source and install the project itself. The `johnny/` package
# carries SQLAlchemy-free voice-pipeline modules (e.g. `ApprovalGate`,
# event dataclasses) that `app.services.*` imports — required from
# US-027 onward.
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY johnny ./johnny
# No `--no-dev`: the dev group (pytest/ruff/mypy/aiosqlite) was installed in
# the cached deps layer above so `docker compose exec api pytest` works on a
# clean build (Johnny-012). This sync only installs the project itself.
RUN uv sync --frozen \
    --extra local-stt --extra local-tts --extra neural-tts --extra agent

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
