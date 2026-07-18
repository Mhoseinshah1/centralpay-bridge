# Production image for the CentralPay Bridge API, worker, and migrations.
# Multi-arch: linux/amd64 and linux/arm64 (python:slim is multi-arch).

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Install into an isolated virtualenv that is copied into the runtime stage.
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install .


FROM python:3.12-slim AS runtime

# Build metadata (populated by CI; empty defaults keep local builds working).
ARG BUILD_REVISION=""
ARG BUILD_CREATED=""
LABEL org.opencontainers.image.title="centralpay-bridge" \
      org.opencontainers.image.description="Payment bridge between a Telegram bot gateway API and CentralPay" \
      org.opencontainers.image.source="https://github.com/Mhoseinshah1/centralpay-bridge" \
      org.opencontainers.image.version="0.5.0-rc1" \
      org.opencontainers.image.revision="${BUILD_REVISION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.licenses="UNLICENSED"

# PYTHONDONTWRITEBYTECODE: no .pyc files at runtime (read-only-friendly).
# PYTHONUNBUFFERED: JSON logs reach Docker immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is required for container health checks; nothing else is added.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 centralpay \
    && useradd --system --uid 10001 --gid centralpay \
        --home-dir /srv/app --shell /usr/sbin/nologin centralpay

WORKDIR /srv/app

COPY --from=builder /opt/venv /opt/venv
# Alembic files are needed at deploy time by the migration service.
COPY --chown=root:root alembic.ini ./alembic.ini
COPY --chown=root:root alembic ./alembic

USER centralpay

EXPOSE 8000

# Default health check suits the API service; compose overrides it for the
# worker (heartbeat file) and disables it for the one-shot migration service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["curl", "-fsS", "http://127.0.0.1:8000/health/live"]

# Exec form so uvicorn receives SIGTERM directly and shuts down cleanly.
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
