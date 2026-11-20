# Two images from one file:
#   dev  - the tools for local work (tests, linters), run with the source mounted. Used by
#          `docker compose up`.
#   prod - runtime only, with the code baked in, served by gunicorn.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.5.31 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so this layer is cached until the lock file changes. The virtual environment
# lives outside /app so that mounting the source over /app does not hide it.
COPY pyproject.toml uv.lock ./

# Raw downloads (see RAW_STORAGE_DIR) go here; a named volume keeps them between runs.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/raw \
    && chown -R app /data

# ----------------------------------------------------------------------------------- dev
FROM base AS dev
RUN uv sync --frozen
COPY . .
RUN chown -R app /app
USER app
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py runserver 0.0.0.0:8000"]

# ---------------------------------------------------------------------------------- prod
FROM base AS prod
RUN uv sync --frozen --no-dev
COPY . .
RUN chown -R app /app
USER app
EXPOSE 8000
# Static files are collected at start-up so the image does not need the runtime environment.
CMD ["sh", "-c", "python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2"]
