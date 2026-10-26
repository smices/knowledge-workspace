FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.13.5 \
    && uv sync --frozen --no-dev --no-install-project \
    && useradd --create-home --uid 10001 app
COPY --chown=10001:10001 app app
COPY --chown=10001:10001 worker worker
COPY --chown=10001:10001 migrations migrations
COPY --chown=10001:10001 alembic.ini ./
COPY --chown=10001:10001 web web
COPY --chown=10001:10001 admin/dist admin/dist
USER 10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
