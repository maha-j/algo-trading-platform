# ─────────────────────────────────────────────────────────────────────────────
# Multi-stage Dockerfile — Institutional Algorithmic Trading Platform
#
# Stages:
#   base       — Python 3.12 slim + system deps + poetry install
#   core       — Trading core engines (strategy, risk, execution, portfolio)
#   api        — FastAPI gateway
#   dashboard  — Streamlit dashboard
#
# DEPLOY-01 Fix: docker-compose.yml now references this single file
# with target: core | api | dashboard
# ─────────────────────────────────────────────────────────────────────────────

# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

LABEL maintainer="quant-team@trading-platform.io"
LABEL org.opencontainers.image.source="https://github.com/org/trading-platform"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    libssl-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd -r trading && useradd -r -g trading -m -s /bin/bash trading

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --upgrade pip && \
    pip install poetry==1.7.1 && \
    poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --no-root

# Copy application source
COPY --chown=trading:trading . .

USER trading

# ── Trading Core ──────────────────────────────────────────────────────────────
FROM base AS core

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

CMD ["python", "-m", "uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--workers", "1", \
     "--log-config", "shared/logging_setup.py"]

# ── FastAPI Gateway ───────────────────────────────────────────────────────────
FROM base AS api

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]

# ── Streamlit Dashboard ───────────────────────────────────────────────────────
FROM base AS dashboard

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "dashboard/app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
