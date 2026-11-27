FROM ghcr.io/astral-sh/uv:0.13.10 AS uv
FROM python:3.14.10-slim-bookworm
# Debian's published updates, security fixes included, which the base image can lag behind
# (SECURITY.md: the image check fails on a High or Critical vulnerability with a published fix).
# Docker caches this step: build with --pull --no-cache to fetch updates published since.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN DEBUG=true uv run --no-sync python manage.py collectstatic --noinput \
    && useradd --system --uid 10001 --create-home --shell /bin/bash app \
    && install -d -m 0700 -o app -g app /home/app/.ssh \
    && chown -R app:app /app
USER app
ENV HOME="/home/app" PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --timeout 60 --error-logfile -"]
