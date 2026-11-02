# ---- Args available before FROM (some builders read these early) ----
ARG BUILD
ARG BUILD_DATE
ARG GITHUB_SHA

# ---- Base image ----
FROM python:3.12-slim-bookworm AS base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# ---- Builder stage ----
FROM base AS builder
# Make BUILD_DATE available in this stage (we use it below)
ARG BUILD_DATE

# System deps for building and curl for fetching artifacts
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        curl; \
    rm -rf /var/lib/apt/lists/*

# Copy app sources into build context
COPY /app/ /build/app/

# Ensure entrypoint script is executable
RUN chmod +x /build/app/run

# Install Python deps into /build/usr/local (staged)
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    pip3 install --disable-pip-version-check --prefix=/build/usr/local -r /build/app/requirements.txt

# Fetch ffmpeg + mediamtx, stage SDK libs, and write a build date safely
RUN set -eux; \
    echo "BUILDING IMAGE FOR $(uname -m)"; \
    # Pinned releases + SHA-256 of each download (from the releases' published
    # digests). When bumping FFMPEG_TAG (here) or MTX_TAG (app/.env), update the hashes.
    FFMPEG_TAG=v2.2.2; \
    if [ "$(uname -m)" = "armv7l" ]; then \
        TARGETARCH=arm; FFMPEG_ARCH=arm32v7; MTX_ARCH=armv7; \
        FFMPEG_SHA256=7ca3a11389c3f1f06d620a3a1949a5ecfdc16112dd88ba2e7e9cdc8ec278d3af; \
        MTX_SHA256=97c8e9167a8ba06dbe0c3cc86879b109b653d3c4699552dad69509b47d6cd36e; \
    elif [ "$(uname -m)" = "aarch64" ]; then \
        TARGETARCH=arm64; FFMPEG_ARCH=aarch64; MTX_ARCH=arm64; \
        FFMPEG_SHA256=3ed1724533921b6b54d1fd35802af62abd29e8672e0afb94bb2f8c18158323a3; \
        MTX_SHA256=61b37116e36417a1afb9395f9f0d0c63d97f30cbdb0e6a6dccf95d69289e97f4; \
    else \
        TARGETARCH=amd64; FFMPEG_ARCH=x86_64; MTX_ARCH=amd64; \
        FFMPEG_SHA256=b29b9d64111410a322e5e5558d1c4f968864ca44630329dc6cd31295997999a3; \
        MTX_SHA256=450d1172bf6708cbd630eada115ccfc33453227e16750369113d1dfe34f876d8; \
    fi; \
    cd /build; \
    . app/.env; \
    mkdir -p tokens; \
    # ffmpeg (static) for the detected architecture
    curl -fsSL "https://github.com/homebridge/ffmpeg-for-homebridge/releases/download/${FFMPEG_TAG}/ffmpeg-alpine-${FFMPEG_ARCH}.tar.gz" -o ffmpeg.tar.gz; \
    echo "${FFMPEG_SHA256}  ffmpeg.tar.gz" | sha256sum -c - || (echo "ffmpeg checksum mismatch for ${FFMPEG_TAG} ${FFMPEG_ARCH}" && exit 1); \
    tar tzf ffmpeg.tar.gz || (echo "Invalid ffmpeg tarball for ${FFMPEG_ARCH}" && exit 1); \
    tar xzf ffmpeg.tar.gz -C .; \
    rm ffmpeg.tar.gz; \
    # MediaMTX server
    curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MTX_TAG}/mediamtx_v${MTX_TAG}_linux_${MTX_ARCH}.tar.gz" -o mediamtx.tar.gz; \
    echo "${MTX_SHA256}  mediamtx.tar.gz" | sha256sum -c - || (echo "mediamtx checksum mismatch for v${MTX_TAG} ${MTX_ARCH}; update MTX_SHA256" && exit 1); \
    tar tzf mediamtx.tar.gz || (echo "Invalid mediamtx tarball for ${MTX_ARCH}" && exit 1); \
    tar xzf mediamtx.tar.gz -C app; \
    rm mediamtx.tar.gz; \
    # Stage Wyze SDK lib (provided in repo under app/lib/lib.<arch>)
    cp app/lib/lib.${TARGETARCH} usr/local/lib/libIOTCAPIs_ALL.so; \
    # Clean staging-only artifacts
    rm -rf app/*.txt app/lib/; \
    # SAFE: tolerate unset BUILD_DATE under 'set -u'
    BUILD_DATE="${BUILD_DATE:-$(date)}"; \
    echo BUILD_DATE="${BUILD_DATE}" > .build_date

# ---- Final runtime image ----
FROM base
ARG BUILD
ARG GITHUB_SHA
ARG BUILD_DATE
ARG BUILD_VERSION

# Trim a bit
RUN set -eux; \
    rm -f /var/log/*.log; \
    rm -rf /var/log/apt

# Bring staged files from builder
COPY --from=builder /build /

# Runtime env
ENV PYTHONUNBUFFERED=1 \
    FLASK_APP=frontend \
    BUILD=${BUILD} \
    BUILD_VERSION=${BUILD_VERSION} \
    BUILD_DATE=${BUILD_DATE} \
    GITHUB_SHA=${GITHUB_SHA} \
    FLASK_ENV=production \
    FLASK_DEBUG=0

WORKDIR /app
ENTRYPOINT [ "/app/run" ]

# Labels (kept from your existing file)
LABEL \
    io.hass.name="Docker Wyze Bridge for Home Assistant" \
    io.hass.description="Bridges Wyze Cameras into Home Assistant using MediaMTX in Docker container" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="Marc Brooks <idisposable@gmail.com>" \
    org.label-schema.name="Docker Wyze Bridge HA" \
    org.label-schema.description="Docker Wyze Bridge for Home Assistant" \
    org.label-schema.build-date=${BUILD_DATE} \
    org.label-schema.schema-version="1.0" \
    org.label-schema.usage="https://github.com/IDisposable/docker-wyze-bridge/blob/main/README.md" \
    org.label-schema.vcs-ref=${GITHUB_SHA} \
    org.label-schema.vcs-url="https://github.com/IDisposable/docker-wyze-bridge/" \
    org.label-schema.vendor="HomeAssistant add-ons by Marc Brooks"

