# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
# Official Home Assistant base image with Python 3.12 (multi-arch: amd64 + arm64/aarch64).
# The Supervisor no longer passes BUILD_FROM (since 2026.04), so the default here is
# what gets built; it is pinned on purpose. See README.md, "Releasing a new version".
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.12-alpine3.24-2026.08.0
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/ember

# Wheels only: every dependency has a musl wheel, so a missing one fails loudly
# instead of trying to compile on a Raspberry Pi.
COPY requirements.txt ./
RUN pip3 install --only-binary=:all: -r requirements.txt

# config.yaml holds the version the app reports; CHANGELOG.md is what the agent
# reads to learn about its own upgrades.
COPY config.yaml CHANGELOG.md ./
COPY app ./app
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/ember/run /etc/s6-overlay/s6-rc.d/ember/finish

LABEL \
    org.opencontainers.image.title="Home Assistant App: Ember" \
    org.opencontainers.image.description="A self-sustaining AI agent that has to earn more than its API calls cost." \
    org.opencontainers.image.source="https://github.com/Stullee/SURVIVE"
