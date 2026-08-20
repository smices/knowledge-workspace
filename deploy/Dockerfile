FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir --timeout 300 --retries 5 . \
    && useradd --create-home --uid 10001 app
COPY --chown=10001:10001 app app
COPY --chown=10001:10001 worker worker
COPY --chown=10001:10001 migrations migrations
COPY --chown=10001:10001 alembic.ini ./
COPY --chown=10001:10001 web web
COPY --chown=10001:10001 admin/dist admin/dist
USER 10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
