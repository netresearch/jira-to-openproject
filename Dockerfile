# syntax=docker/dockerfile:1

# ===== Stage 1: build =====
FROM python:3-slim AS build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Upstream publishes a rolling binary image and has no major-only uv:0 tag.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Resolve + install dependencies into /opt/venv. The venv lives outside /app
# so compose's bind mount of the source tree over /app does not hide it.
COPY pyproject.toml uv.lock ./
COPY scripts/locked_pyyaml_requirement.py /tmp/
# PyYAML has no CPython 3.15 Linux wheel. Only this hash-locked dependency
# may build from source. Install every other third-party package wheel-only.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv --python /usr/local/bin/python /opt/venv \
    && python /tmp/locked_pyyaml_requirement.py > /tmp/j2o-pyyaml.txt \
    && UV_PROJECT_ENVIRONMENT=/tmp/j2o-builder uv sync --frozen --only-group image-build --no-build \
    && python -m pip --python /tmp/j2o-builder/bin/python wheel --no-build-isolation --require-hashes \
        --no-deps -r /tmp/j2o-pyyaml.txt --wheel-dir /tmp/j2o-pyyaml \
    && uv pip install --python /opt/venv/bin/python --no-build --no-deps /tmp/j2o-pyyaml/*.whl

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --inexact --no-build --no-install-package pyyaml --no-install-project

# Now copy sources and install the project itself
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --python /tmp/j2o-builder/bin/python --no-build-isolation --wheel --out-dir /tmp/j2o-dist . \
    && uv pip install --python /opt/venv/bin/python --no-build --no-deps /tmp/j2o-dist/*.whl

# ===== Stage 2: runtime =====
FROM python:3-slim AS runtime

LABEL maintainer="Netresearch DTT GmbH <sebastian.mendel@netresearch.de>" \
      org.opencontainers.image.title="jira-to-openproject" \
      org.opencontainers.image.description="Jira → OpenProject migration tool" \
      org.opencontainers.image.source="https://github.com/netresearch/jira-to-openproject" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Runtime OS deps only. Build-essential is NOT copied.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
        openssh-client \
        tmux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 appuser
# docker.io intentionally omitted - uses SSH-based remote Docker client
# See src/infrastructure/openproject/docker_client.py for remote Docker operations via SSH


# Copy the venv and the sources from the build stage. The project is
# installed from a wheel. PYTHONPATH keeps a Compose checkout at /app active.
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=appuser:appuser /app /app

# var/ is excluded by .dockerignore, and WORKDIR left /app itself root-owned,
# so appuser could not create it at startup. j2o writes logs, data and
# checkpoints only below var/; compose bind-mounts the host tree over /app.
RUN install -d -o appuser -g appuser /app/var

USER appuser

# The container's CMD is `sleep infinity` — operators exec `j2o ...`
# against it. The healthcheck exercises the actual CLI entry point so a
# broken package install, missing dependency, or import-time crash flips
# the container to unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD j2o --help > /dev/null 2>&1 || exit 1

CMD ["sleep", "infinity"]
