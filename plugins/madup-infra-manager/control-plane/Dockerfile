FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv

FROM docker.io/library/python:3.13.14-slim-trixie@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:/app/bin:$PATH"

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --from=uv /uv /uvx /usr/local/bin/

COPY pyproject.toml ./
COPY uv.lock ./
COPY src ./src

RUN uv sync --frozen --no-dev

EXPOSE 8080

USER appuser

CMD ["uvicorn", "mim_control_plane.main:app", "--host", "0.0.0.0", "--port", "8080"]
