"""Alembic migration environment.

Supports both online (connected) and offline (SQL generation) modes.
Reads DATABASE_URL_SYNC from environment for migration execution.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add backend directory to path so models can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import Base  # noqa: E402
from app.db.url import normalize_sync_database_url  # noqa: E402
from app.models import *  # noqa: E402, F401, F403

# Alembic Config object
config = getattr(context, "config", None)

# Setup logging from alembic.ini
if config is not None and config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


def get_database_url() -> str:
    """Get sync database URL from environment."""
    url = os.environ.get("DATABASE_URL_SYNC")
    if not url:
        url = os.environ.get("DATABASE_URL")
    if not url:
        user = os.environ.get("POSTGRES_USER", "aik_user")
        password = os.environ.get("POSTGRES_PASSWORD", "")
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5433")
        db = os.environ.get("POSTGRES_DB", "aik_db")
        url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"
    return normalize_sync_database_url(url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — generates SQL without connecting."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects to database."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if config is not None and hasattr(context, "is_offline_mode"):
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
