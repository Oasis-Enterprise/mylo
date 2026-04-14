# syntax=docker/dockerfile:1.7
ARG BUILD_FROM
FROM ${BUILD_FROM} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps: build-base is needed to compile pydantic-core / rapidfuzz wheels
# on architectures without prebuilt wheels. Kept in a single RUN so the image
# stays small after cleanup.
RUN apk add --no-cache \
        ca-certificates \
        tini \
    && apk add --no-cache --virtual .build-deps \
        build-base \
        python3-dev \
        libffi-dev

WORKDIR /app

# Copy only metadata first so the dep install layer caches across code changes.
COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src

RUN pip install --no-cache-dir . \
    && apk del .build-deps

# s6-overlay service wiring (base image provides s6).
COPY addon/rootfs /

CMD ["/init"]
