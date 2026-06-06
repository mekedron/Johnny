# Backend image: FastAPI app served by uvicorn; also the worker entrypoint
# (compose overrides CMD to `python -m app.worker`). Dependencies managed by uv.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.11.19

WORKDIR /workspace

# Install runtime dependencies first so the layer is reused unless deps change.
# `--extra local-stt` pulls in faster-whisper, `--extra local-tts` pulls
# in piper-tts so the providers `/test` endpoint can run real smoke
# calls against on-device STT and TTS; without them the smoke calls die
# on `import faster_whisper` / "piper binary not found". The meet-worker
# image installs its own pinned copies via pip; this layer covers the
# api/worker.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra local-stt --extra local-tts

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
RUN uv sync --frozen --no-dev --extra local-stt --extra local-tts

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
