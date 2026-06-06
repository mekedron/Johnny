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
# BuildKit auto-populates TARGETARCH (amd64 / arm64). We branch on it
# because the PyTorch CPU wheel index only ships linux_x86_64 wheels —
# on arm64 the install would 404. On arm64 we let NeMo's transitive
# torch come from PyPI, which ships aarch64 wheels that are CPU-only by
# definition (NVIDIA has no consumer aarch64 CUDA). On amd64 we pre-
# install torch from the CPU index so we don't accidentally pull
# PyPI's CUDA-bundled wheel (~2.7 GB). `uv pip install --python` is
# used because the uv-created venv at /opt/venv has no `pip` of its
# own — uv is minimal.
ARG TARGETARCH
RUN set -e; \
    if [ "$INSTALL_PARAKEET" = "1" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends libsndfile1; \
        rm -rf /var/lib/apt/lists/*; \
        case "$TARGETARCH" in \
            amd64) \
                uv pip install --no-cache-dir \
                    --python /opt/venv/bin/python \
                    --index-url https://download.pytorch.org/whl/cpu \
                    --extra-index-url https://pypi.org/simple \
                    torch torchaudio \
                ;; \
        esac; \
        uv pip install --no-cache-dir \
            --python /opt/venv/bin/python \
            "nemo_toolkit[asr]>=2.5.0" \
            "tokenizers<=0.23.0"; \
    fi
# Why the explicit tokenizers pin: nemo_toolkit[asr] resolves
# transformers ~=4.57.0, which requires tokenizers <=0.23.0,
# but the earlier `uv sync --extra local-stt` already pulled
# faster-whisper (range tokenizers <1), which picked
# tokenizers==0.23.1. Without re-pinning here, the second install
# does not backtrack, leaves the env inconsistent, and
# `import nemo.collections.asr` raises ImportError at module load
# (transformers' own version_check rejects 0.23.1). The Parakeet
# adapter catches ImportError generically and surfaces a "NeMo not
# installed" message — diagnosing the real problem required peeling
# back several layers of import chain. Pinning here fixes both
# images and keeps faster-whisper happy (0.23.0 is in its range too).

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
