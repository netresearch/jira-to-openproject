FROM python:3.13-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    docker.io \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and setuptools
RUN python -m pip install --upgrade pip \
    && python -m pip install --upgrade setuptools \
    && python -m pip install --upgrade wheel

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Create a non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Set default command
CMD ["python", "-m", "pytest"]
