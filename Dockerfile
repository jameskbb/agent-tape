# syntax=docker/dockerfile:1.7
# AgentTape container image: builds the web UI, installs the Python package with uv, and
# runs `agenttape serve` (API + UI + worker + scheduler) as a non-root user.
#
# The container listens on 0.0.0.0, so AgentTape REQUIRES an API token: set
# AGENTTAPE_API_TOKEN_HASH (see docs/DEPLOYMENT.md) or `serve` refuses to start.

########################################################################## web UI build
FROM node:24-bookworm-slim AS web
WORKDIR /src
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json frontend/package.json
COPY e2e/package.json e2e/package.json
RUN pnpm install --frozen-lockfile --filter @agenttape/frontend...
COPY frontend/ frontend/
RUN pnpm --dir frontend build

########################################################################## Python build
FROM python:3.13-slim-bookworm AS app
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3
COPY backend/src ./src
COPY --from=web /src/frontend/dist ./src/agenttape/web_dist
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3

########################################################################## runtime
FROM python:3.13-slim-bookworm AS runtime
# The runtime user's ids. Agents edit repositories bind-mounted at /repos, and Git refuses a
# repository owned by another uid, so on Linux build with your own ids (see docs/DEPLOYMENT.md).
ARG AGENTTAPE_UID=10001
ARG AGENTTAPE_GID=10001
# git: repository snapshots, worktrees, and the Demo Agent's fixture repository.
# tini: signal handling so `docker stop` cancels running agents cleanly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --non-unique --gid "${AGENTTAPE_GID}" agenttape \
    && useradd --system --non-unique --uid "${AGENTTAPE_UID}" --gid agenttape --home-dir /home/agenttape --create-home agenttape \
    && mkdir -p /data /repos \
    && chown agenttape:agenttape /data /repos
COPY --from=app --chown=agenttape:agenttape /app /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    AGENTTAPE_DATA_DIR=/data \
    AGENTTAPE_HOST=0.0.0.0 \
    AGENTTAPE_PORT=8787 \
    AGENTTAPE_ALLOWED_ROOTS=/repos
USER agenttape
WORKDIR /home/agenttape
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/v1/health', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["agenttape", "serve"]
