FROM python:3.11-alpine

WORKDIR /app

# System dependencies:
#   - curl: used by the container health check below
#   - sqlite: SQLite CLI (matches the sqlite3 stdlib module already used by database.py)
#   - docker-cli: lets this backend issue `docker` commands against the
#     HOST's Docker daemon via the socket mounted in docker-compose.yml.
#     Note this image intentionally does NOT install/run its own Docker
#     daemon -- AutoPod Cloud controls the host's Docker Engine, it does not
#     run a Docker-in-Docker setup (kept simple on purpose; see ARCHITECTURE.md).
#
# Previously this image installed `podman`, `sudo`, and `shadow`, and granted
# the app user passwordless sudo (NOPASSWD:ALL) -- effectively root-equivalent
# access. That is removed below (see CURRENT_STATUS_AND_IMPLEMENTATION_PLAN.md,
# security findings). The app user is still granted Docker socket access,
# which is itself a broad permission (Docker socket access is roughly
# equivalent to root on the host) -- this is a known, accepted trade-off for
# a simple single-host CI/CD tool, not something this Dockerfile pretends
# to fully eliminate.
RUN apk update && apk add --no-cache \
    curl \
    sqlite \
    docker-cli \
    git \
    && rm -rf /var/cache/apk/*

# Create a non-root application user. No sudo, no passwordless anything.
# The DOCKER_GID build arg should be set to match the `docker` group's GID
# on the HOST machine so this user can use the mounted Docker socket
# (`docker.sock` is typically owned by a host group, commonly named
# `docker`). Find the host's docker GID with: `getent group docker`.
ARG DOCKER_GID=999
RUN (addgroup -g ${DOCKER_GID} -S dockerhost 2>/dev/null || true) && \
    adduser -D -s /bin/sh autopod && \
    (addgroup autopod dockerhost 2>/dev/null || true) && \
    (addgroup autopod root 2>/dev/null || true)

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

RUN mkdir -p logs database && chown -R autopod:autopod /app

USER autopod

EXPOSE 5000

ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
ENV FLASK_RUN_HOST=0.0.0.0
# FLASK_DEBUG intentionally left unset here -- production images never run
# with Werkzeug's debug/interactive-debugger mode enabled. Set FLASK_DEBUG=true
# only in a local, non-networked development override.

# Production entrypoint: Gunicorn, not Flask's built-in dev server.
# `python app.py` (still fine for local development, see README.md) hits
# Flask's own dev server, which previously ran with debug=True hardcoded --
# a serious risk if this process is ever reachable outside localhost.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "300", "app:app"]
