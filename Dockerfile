# syntax=docker/dockerfile:1

# Vibe Radio in one container: the FastAPI backend serves the API, the HLS stream
# and the built listener console on a single port.

# --- Build the listener console ----------------------------------------------
FROM node:22-alpine AS console

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# --- Runtime ------------------------------------------------------------------
FROM python:3.14-slim

# ffmpeg/ffprobe normalize the library and cut HLS segments; curl fetches the
# Claude Code CLI, which the Claude Agent SDK drives for programming decisions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# The CLI lives outside the app user's home so that home can be bind-mounted for
# the Claude login without hiding the binary.
RUN mkdir -p /opt/claude \
    && HOME=/opt/claude bash -c 'curl -fsSL https://claude.ai/install.sh | bash' \
    && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude \
    && chmod -R a+rX /opt/claude
ENV DISABLE_AUTOUPDATER=1

# uid 1000 matches the host user, so the bind-mounted data directory and Claude
# credentials stay readable and writable on both sides.
RUN useradd --create-home --uid 1000 app

WORKDIR /app/backend
COPY --chown=app:app backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev && chown -R app:app /app

COPY --chown=app:app backend/ ./
COPY --from=console --chown=app:app /frontend/dist /app/frontend/dist

USER app
ENV HOME=/home/app \
    PATH=/app/backend/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "viberadio.main:app", "--host", "0.0.0.0", "--port", "8000"]
