FROM python:3.10-slim AS base

# Security: run as non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# Install system dependencies for psycopg2 and healthcheck
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq-dev gcc curl && \
    rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Copy project files with proper ownership
COPY --chown=appuser:appuser pyproject.toml README.md ./
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser tests/ ./tests/
COPY --chown=appuser:appuser alembic.ini ./

# Install project and development dependencies
RUN pip install --no-cache-dir ".[dev]"

# Ensure appuser owns working directory
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
