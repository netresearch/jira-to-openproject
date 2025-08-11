FROM python:3.13-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set working directory
WORKDIR /app

# Install system dependencies (minimal attack surface)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*
# SECURITY: docker.io package removed - uses SSH-based remote Docker client
# See src/clients/docker_client.py for remote Docker operations via SSH

# Install uv (modern Python package manager) globally
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

# Create a non-root user early (before copying files)
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Copy project metadata and lock file first (for optimal caching)
COPY pyproject.toml uv.lock ./

# Install Python dependencies system-wide from lock file
RUN uv sync --frozen --no-install-project && \
    uv pip install --system .

# Switch to non-root user after dependency installation
USER appuser

# Copy project files (this layer changes most frequently, so it's last)
COPY --chown=appuser:appuser . .

# Set default command for development
CMD ["sleep", "infinity"]
