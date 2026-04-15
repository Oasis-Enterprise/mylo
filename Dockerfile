# syntax=docker/dockerfile:1.7
ARG BUILD_FROM

# ─── UI build stage ──────────────────────────────────────────────────────────
# Build the frontend in a standalone Node image so the runtime image stays
# Python-only. Output lands at /app/src/mylo/server/static/ which the
# Python server serves verbatim.
FROM node:20-alpine AS ui-builder
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY ui/ ./
# Redirect Vite output into the Python package's static dir.
RUN npx vite build --outDir /ui/dist

# ─── Runtime stage ───────────────────────────────────────────────────────────
FROM ${BUILD_FROM} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tini is intentionally NOT installed — the HA base-python image already
# uses s6-overlay as PID 1 (ENTRYPOINT=/init). Adding tini on top made
# Docker try to run `/init /init` and s6 refused with "can only run as
# pid 1".
RUN apk add --no-cache \
        ca-certificates \
    && apk add --no-cache --virtual .build-deps \
        build-base \
        python3-dev \
        libffi-dev

WORKDIR /app

COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src
COPY data /app/data

# Drop the built UI into the Python package's static dir.
COPY --from=ui-builder /ui/dist /app/src/mylo/server/static

RUN pip install --no-cache-dir . \
    && apk del .build-deps

COPY rootfs /

# No CMD — inherit ENTRYPOINT=/init from the base image. s6-overlay takes
# over from there and runs our service under rootfs/etc/services.d/mylo/.
