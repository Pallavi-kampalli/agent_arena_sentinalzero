FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Install curl for container healthcheck
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user and group (UID/GID 10001)
RUN addgroup --system --gid 10001 appuser && \
    adduser --system --uid 10001 --gid 10001 --no-create-home appuser

WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy production source code and migrations
COPY src/ /app/src/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini
COPY pyproject.toml /app/pyproject.toml

# Install package in editable mode
RUN pip install --no-cache-dir -e .

# Secure directory permissions
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Default command: single-worker Uvicorn preserving in-process mutexes and async concurrency
CMD ["uvicorn", "agent_arena.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
