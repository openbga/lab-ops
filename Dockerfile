FROM ghcr.io/astral-sh/uv:0.13.9 AS uv
FROM python:3.14.10-slim-bookworm
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
