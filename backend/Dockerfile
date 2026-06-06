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

# Optional: NVIDIA NeMo for the Parakeet STT adapter (Johnny-stt.1).
# Heavy (~2.5 GB CPU torch + NeMo deps) so it's gated behind a build
# arg — defaults to on so the catalog `/stt_test` Test button works
# out of the box; set INSTALL_PARAKEET=0 to skip. CPU-only torch is
# baked in via the PyTorch CPU index; a CUDA variant is tracked as a
# follow-up under the Johnny-stt epic. The conditional install sits
# BEFORE the code COPY so editing app/johnny doesn't bust the ~2.5 GB
# cache layer. `--inexact` on the final `uv sync` below keeps these
# packages around (without it, uv would remove anything outside the
# resolved lock).
ARG INSTALL_PARAKEET=1
RUN if [ "$INSTALL_PARAKEET" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            libsndfile1 \
        && rm -rf /var/lib/apt/lists/* \
        && /opt/venv/bin/python -m pip install --no-cache-dir \
            --index-url https://download.pytorch.org/whl/cpu \
            torch torchaudio \
        && /opt/venv/bin/python -m pip install --no-cache-dir \
            "nemo_toolkit[asr]>=2.5.0"; \
    fi

# Copy source and install the project itself. The `johnny/` package
# carries SQLAlchemy-free voice-pipeline modules (e.g. `ApprovalGate`,
# event dataclasses) that `app.services.*` imports — required from
# US-027 onward.
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY johnny ./johnny
RUN uv sync --frozen --no-dev --inexact --extra local-stt --extra local-tts

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
