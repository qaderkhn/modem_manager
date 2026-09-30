# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy UV_CACHE_DIR=/home/app/.cache/uv PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Source-only edits preserve this layer; dependency changes reuse downloaded wheels.
RUN --mount=type=cache,id=modem-manager-uv,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
RUN groupadd -g 10001 app \
    && useradd -u 10001 -g app -m -d /home/app app \
    && install -d -o app -g app -m 0700 /data /home/app/.cache/uv

COPY config ./config
COPY modem ./modem
COPY templates ./templates
COPY static ./static
COPY LICENSE manage.py gunicorn.conf.py ./
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/modem-entrypoint
RUN DJANGO_SECRET_KEY=build-only-placeholder-not-a-runtime-secret-000000000000000000 python manage.py collectstatic --noinput

# Code/dependencies stay root-owned and readable; only data and home need writes.
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["modem-entrypoint"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "--bind", "0.0.0.0:8000", "config.wsgi:application"]
