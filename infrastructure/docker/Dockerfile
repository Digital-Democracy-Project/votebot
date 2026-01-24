# VoteBot 2.0 Dockerfile
# Multi-stage build for optimized production image

# Build stage
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim as production

WORKDIR /app

# Create non-root user
RUN groupadd --gid 1000 votebot && \
    useradd --uid 1000 --gid votebot --shell /bin/bash --create-home votebot

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY --chown=votebot:votebot src/ ./src/
COPY --chown=votebot:votebot scripts/ ./scripts/
COPY --chown=votebot:votebot pyproject.toml ./

# Install the package
RUN pip install --no-cache-dir -e .

# Switch to non-root user
USER votebot

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/votebot/v1/health/live')" || exit 1

# Default command
CMD ["uvicorn", "votebot.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Development stage
FROM production as development

USER root

# Install development dependencies
RUN pip install --no-cache-dir pytest pytest-asyncio pytest-cov ruff mypy

USER votebot

# Override command for development
CMD ["uvicorn", "votebot.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
