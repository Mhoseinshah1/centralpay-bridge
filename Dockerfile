# Production image for the CentralPay Bridge API, worker, and migrations.
# Multi-arch: linux/amd64 and linux/arm64 (python:slim is multi-arch).
#
# Base image is pinned by digest, not just tag: a floating `python:3.12-slim`
# silently moves to whatever Debian point-in-time snapshot Docker's official
# image happened to publish last, which is exactly how a past release build
# picked up a `libssl3t64`/`openssl` package one Debian security point-
# release behind the fix for CVE-2026-14456 (HIGH) without any change on our
# side. The digest below is `python:3.12-slim-trixie`, i.e. the same
# content `python:3.12-slim` currently resolves to, pinned explicitly so a
# rebuild is reproducible instead of silently drifting. Bump it deliberately
# (re-resolve via the Docker Hub v2 API, verify amd64+arm64 both present)
# when a newer base is needed, not by accident.
ARG BASE_IMAGE=python:3.12-slim-trixie@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

# Shared `base` stage: both `builder` and `runtime` derive from this, not
# from `${BASE_IMAGE}` directly, so the apt security refresh below runs
# exactly once and both stages get it -- an earlier version of this file
# refreshed only the runtime stage, leaving `builder`'s `pip install .`
# (a network operation) running against the same unrefreshed snapshot
# identified as carrying the vulnerable OpenSSL package, and meant Trivy
# (which only scans the final shipped image) could never have reported
# on the discarded builder stage's own exposure.
FROM ${BASE_IMAGE} AS base

# General Debian security refresh, not a one-CVE hardcoded package pin: the
# pinned base digest above is a point-in-time snapshot, so any package with a
# newer build in Debian's own security repo by the time *this* image is
# built (e.g. the libssl3t64/openssl fix for CVE-2026-14456) is picked up
# here instead of waiting on the next upstream python:3.12-slim-trixie
# publish. Plain `apt-get upgrade` — never the more aggressive variant that
# can also add or remove packages — only ever replaces an existing package
# with a newer build of itself, so it cannot introduce anything Trivy
# hasn't already scanned this base for.
#
# Cache-busting: BuildKit caches this RUN layer keyed on its own literal
# command text and the parent layer -- neither the pinned base digest nor
# this instruction's text changes on their own, so with GHA layer caching
# enabled (cache-from/cache-to: type=gha in both ci.yml and release.yml)
# this layer would otherwise be reused unchanged forever after the first
# successful build, silently never re-running `apt-get update`/`upgrade`
# again and defeating the whole point of a build-time security refresh.
# CI supplies a fresh value once per UTC day via --build-arg (see
# `Resolve APP_VERSION` in both workflows); referencing it inside the RUN
# command (not just declaring the ARG) is what actually changes this
# layer's cache key when the value changes.
#
# curl is installed here, in the same RUN as `apt-get update`, and the apt
# package lists are removed here too -- Docker layers are immutable, so a
# later `rm -rf /var/lib/apt/lists/*` in a downstream layer only hides the
# lists from the merged filesystem view; the bytes stay committed in this
# layer and still ship with (and are pulled/stored as part of) the image.
# curl only costs the runtime stage anything (it needs it for the
# HEALTHCHECK below); the discarded `builder` stage also gets it here, but
# nothing from a discarded stage's own layers reaches the final image.
ARG APT_REFRESH_CACHEBUST=""
RUN echo "apt security refresh cache-bust: ${APT_REFRESH_CACHEBUST}" \
    && apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*


FROM base AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Install into an isolated virtualenv that is copied into the runtime stage.
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install .


FROM base AS runtime

# Build metadata (populated by CI; empty defaults keep local builds working).
# APP_VERSION is supplied by CI/release from app.version.APP_VERSION — never a
# hardcoded literal here, so the image label can never drift from the app
# version. A local build with no --build-arg gets an empty (not stale) label.
ARG APP_VERSION=""
ARG BUILD_REVISION=""
ARG BUILD_CREATED=""
LABEL org.opencontainers.image.title="centralpay-bridge" \
      org.opencontainers.image.description="Payment bridge between a Telegram bot gateway API and CentralPay" \
      org.opencontainers.image.source="https://github.com/Mhoseinshah1/centralpay-bridge" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${BUILD_REVISION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.licenses="UNLICENSED"

# PYTHONDONTWRITEBYTECODE: no .pyc files at runtime (read-only-friendly).
# PYTHONUNBUFFERED: JSON logs reach Docker immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# curl was already installed in the shared `base` stage above (same layer
# that fetched the apt lists it needed, and that already removed them).
RUN groupadd --system --gid 10001 centralpay \
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
