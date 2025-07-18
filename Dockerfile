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

# Create a non-root user early (before copying files)
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Switch to non-root user for dependency installation
USER appuser

# Copy only requirements first (for optimal caching)
COPY --chown=appuser:appuser requirements.txt .

# Upgrade pip and install Python dependencies as non-root user
RUN python -m pip install --user --upgrade pip setuptools wheel && \
    python -m pip install --user --no-cache-dir -r requirements.txt

# Add user's local Python bin to PATH
ENV PATH="/home/appuser/.local/bin:${PATH}"

# Copy project files (this layer changes most frequently, so it's last)
COPY --chown=appuser:appuser . .

# Set default command for development
CMD ["sleep", "infinity"]
