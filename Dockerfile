# syntax=docker/dockerfile:1.23

# ===== Stage 1: build =====
FROM python:3.14-slim AS build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Build-time OS deps (compilers, headers). Kept only in this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN --mount=type=cache,target=/root/.cache/uv \
    python -m pip install --no-cache-dir uv

# Resolve + install dependencies to /install
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Now copy sources and install the project itself
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# ===== Stage 2: runtime =====
FROM python:3.14-slim AS runtime

LABEL maintainer="Netresearch DTT GmbH <sebastian.mendel@netresearch.de>" \
      org.opencontainers.image.title="jira-to-openproject" \
      org.opencontainers.image.description="Jira → OpenProject migration tool" \
      org.opencontainers.image.source="https://github.com/netresearch/jira-to-openproject" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Runtime OS deps only. Build-essential is NOT copied.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
        openssh-client \
        tmux \
    && rm -rf /var/lib/apt/lists/*
# docker.io intentionally omitted - uses SSH-based remote Docker client
# See src/infrastructure/openproject/docker_client.py for remote Docker operations via SSH

RUN useradd -m -u 1000 appuser

# Copy installed venv and sources from build stage
COPY --from=build --chown=appuser:appuser /app /app

USER appuser

# The container's CMD is `sleep infinity` — operators exec `j2o ...`
# against it. The healthcheck exercises the actual CLI entry point so a
# broken package install, missing dependency, or import-time crash flips
# the container to unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD j2o --help > /dev/null 2>&1 || exit 1

CMD ["sleep", "infinity"]
