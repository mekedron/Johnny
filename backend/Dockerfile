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
# `--extra local-stt` pulls in faster-whisper so the providers `/test`
# endpoint can run a real smoke call against on-device STT; without it
# the smoke call dies on `import faster_whisper`. The meet-worker image
# installs its own pinned copy via pip; this layer covers the api/worker.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra local-stt

# Copy source and install the project itself. The `johnny/` package
# carries SQLAlchemy-free voice-pipeline modules (e.g. `ApprovalGate`,
# event dataclasses) that `app.services.*` imports — required from
# US-027 onward.
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY johnny ./johnny
RUN uv sync --frozen --no-dev --extra local-stt

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
