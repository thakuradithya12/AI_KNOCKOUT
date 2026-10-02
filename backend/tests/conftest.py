"""Shared test fixtures backed only by an in-memory SQLite database and ASGI app.

No test fixture uses API_BASE_URL, the user's DATABASE_URL, a host Redis URL,
or any destructive shared-service operation.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

# Override any shell/.env values before importing application configuration.
os.environ.update(
    {
        "APP_ENV": "test",
        "SECRET_KEY": "isolated-test-only-secret-key-0123456789",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "DATABASE_URL_SYNC": "sqlite+pysqlite:///:memory:",
        "REDIS_URL": "redis://127.0.0.1:1/15",
        "ARTIFACT_STORAGE_BACKEND": "postgresql",
    }
)

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.core.config import get_settings
from app.db.base import Base
import app.models  # noqa: F401  # populate Base.metadata
from app.services.rate_limiter import _memory_store

get_settings.cache_clear()


@pytest.fixture
async def test_engine(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'isolated-test.sqlite3'}",
        connect_args={"timeout": 15},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def test_session_factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def db_session(test_session_factory) -> AsyncGenerator[AsyncSession, None]:
    async with test_session_factory() as session:
        yield session


@pytest.fixture
async def api_client(test_session_factory, monkeypatch):
    from app.main import app

    async def isolated_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = isolated_get_db

    async def healthy_db():
        return {"status": "ok"}

    async def healthy_redis():
        return {"status": "ok"}

    monkeypatch.setattr("app.api.v1.health.check_database_health", healthy_db)
    monkeypatch.setattr("app.api.v1.health.check_redis_health", healthy_redis)
    _memory_store.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://isolated.test") as client:
        yield client
    app.dependency_overrides.clear()
    _memory_store.clear()
