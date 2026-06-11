# Johnny skills-sandbox (Johnny-trt.35) — the execution boundary for all
# skill / CLI tool work. The api/worker never run skill commands themselves;
# they POST to this container's internal exec API (execd.py) and the command
# runs HERE, as a non-root user, with timeouts, output caps, and compose
# resource limits. Mirrors openclaw's sandbox model
# (scripts/docker/sandbox/Dockerfile): debian-slim + a guaranteed toolset,
# skills mapped in via a volume, requires.bins checked inside the container.
#
# Operators: to add your own CLI tools, edit the OPERATOR-CUSTOMIZABLE layer
# below (or drop a script into setup.d/) and rerun ./run-dev.sh or ./run.sh —
# both rebuild this image. See sandbox/README.md for the full guide.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# LAYER 1 — BASELINE TOOLSET (guaranteed; DO NOT remove entries).
#
# Skills may rely on every one of these without declaring them in
# requires.bins. The list is pinned by the integration test
# backend/tests/integration/test_skills_sandbox.py (the /bins all-present
# assertion IS the guarantee) — if you remove a package here, that test is
# the contract you are breaking. Several entries (bash, coreutils, grep,
# sed, tar, gzip, findutils) are Debian Essential and already present; they
# are listed anyway so the guarantee is explicit and self-documenting.
#
# Guaranteed bins: bash, cat, cut, head, tail, tr, sort, uniq, wc (coreutils),
# grep, sed, awk/gawk, find, xargs (findutils), tar, gzip, rg (ripgrep),
# curl, jq, git, python3 — plus TLS trust via ca-certificates.
# ---------------------------------------------------------------------------
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    coreutils \
    curl \
    findutils \
    gawk \
    git \
    grep \
    gzip \
    jq \
    python3 \
    ripgrep \
    sed \
    tar \
  && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# LAYER 2 — gog, the reference Google CLI (github.com/openclaw/gogcli).
#
# Preinstalled so the first skills (calendar/gmail via gog, Johnny-trt.23/.32)
# work on a clean install. Version-pinned + checksum-verified release binary;
# bump GOG_VERSION deliberately. Auth state lives under /home/sandbox (the
# sandbox-home volume), so `gog auth` survives image rebuilds — see
# sandbox/README.md for the one-time auth flow.
# ---------------------------------------------------------------------------
ARG GOG_VERSION=0.24.0
ARG TARGETARCH
RUN set -eux; \
  arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
  case "$arch" in amd64|arm64) ;; *) echo "unsupported arch: $arch" >&2; exit 1 ;; esac; \
  tarball="gogcli_${GOG_VERSION}_linux_${arch}.tar.gz"; \
  base="https://github.com/openclaw/gogcli/releases/download/v${GOG_VERSION}"; \
  cd /tmp; \
  curl -fsSL -O "${base}/${tarball}"; \
  curl -fsSL -O "${base}/checksums.txt"; \
  grep " ${tarball}\$" checksums.txt | sha256sum -c -; \
  tar -xzf "${tarball}" -C /usr/local/bin gog; \
  chmod 0755 /usr/local/bin/gog; \
  rm -f "/tmp/${tarball}" /tmp/checksums.txt; \
  gog --version

# ===========================================================================
# >>> LAYER 3 — OPERATOR-CUSTOMIZABLE LAYER: install YOUR CLI tools here. <<<
#
# Anything you install in this block becomes visible to skills (and to the
# eligibility check GET /bins) after the next ./run-dev.sh / ./run.sh, which
# rebuild this image. Keep customizations HERE (or in setup.d/, below) —
# never `docker compose exec skills-sandbox apt-get install ...`, that
# vanishes on the next rebuild (see the clean-install rule in CLAUDE.md).
#
# Examples:
#
#   RUN apt-get update && apt-get install -y --no-install-recommends \
#       imagemagick ffmpeg pandoc \
#     && rm -rf /var/lib/apt/lists/*
#
#   RUN curl -fsSL -o /usr/local/bin/mytool https://example.com/mytool \
#     && chmod 0755 /usr/local/bin/mytool
#
# For multi-step installs, prefer a script in setup.d/ — every *.sh there
# runs (sorted, as root) in the step below.
# ===========================================================================

# Setup-script hook: operator-provided build steps without touching this
# Dockerfile. Each sandbox/setup.d/*.sh runs at image-build time, sorted,
# as root. setup.d/ ships with only a README, so a clean checkout builds
# with this loop as a no-op.
COPY setup.d/ /opt/sandbox-setup.d/
RUN set -eux; \
  for f in /opt/sandbox-setup.d/*.sh; do \
    [ -e "$f" ] || continue; \
    echo ">>> running setup hook: $f"; \
    bash "$f"; \
  done

# ---------------------------------------------------------------------------
# LAYER 4 — non-root runtime user + the exec daemon.
# ---------------------------------------------------------------------------
RUN useradd --create-home --shell /bin/bash --uid 1000 sandbox

COPY execd.py /opt/sandbox/execd.py

USER sandbox
WORKDIR /home/sandbox
ENV HOME=/home/sandbox

# Internal-only — compose adds no `ports:` mapping for this service.
EXPOSE 8088

CMD ["python3", "/opt/sandbox/execd.py"]
