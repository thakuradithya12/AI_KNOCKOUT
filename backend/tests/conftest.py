"""Isolated ASGI fixtures using SQLite by default or guarded local test PostgreSQL.

The fixtures never use DATABASE_URL, API_BASE_URL, a host Redis URL, or a
shared-service database. PostgreSQL is opt-in through AIK_TEST_DATABASE_URL,
restricted to a local aik_test_* database and per-test private schemas.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import text

from test_support.postgres_test_database import postgres_test_database_urls

# Override any shell/.env values before importing application configuration.
postgres_test_urls = postgres_test_database_urls(os.environ.get("AIK_TEST_DATABASE_URL"))
database_url = postgres_test_urls[0] if postgres_test_urls else "sqlite+aiosqlite:///:memory:"
database_url_sync = postgres_test_urls[1] if postgres_test_urls else "sqlite+pysqlite:///:memory:"
os.environ.update(
    {
        "APP_ENV": "test",
        "SECRET_KEY": "isolated-test-only-secret-key-0123456789",
        "DATABASE_URL": database_url,
        "DATABASE_URL_SYNC": database_url_sync,
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
    schema = f"aik_test_{uuid.uuid4().hex}" if postgres_test_urls else None
    if schema:
        bootstrap_engine = create_async_engine(postgres_test_urls[0], pool_pre_ping=True)
        try:
            async with bootstrap_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        finally:
            await bootstrap_engine.dispose()

        engine = create_async_engine(
            postgres_test_urls[0],
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": schema}},
        )
    else:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'isolated-test.sqlite3'}",
            connect_args={"timeout": 15},
        )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        if schema:
            cleanup_engine = create_async_engine(postgres_test_urls[0], pool_pre_ping=True)
            try:
                async with cleanup_engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                await cleanup_engine.dispose()


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
    class AutoAuthAsyncClient(httpx.AsyncClient):
        async def request(self, method, url, **kwargs):
            headers = dict(kwargs.pop("headers", None) or {})
            if "participants/register" in str(url):
                if headers.get("X-Anonymous") == "true":
                    headers.pop("X-Anonymous", None)
                elif "X-Organizer-Secret" not in headers and "Authorization" not in headers and "cookie" not in headers:
                    headers["X-Organizer-Secret"] = "isolated-test-only-secret-key-0123456789"
            return await super().request(method, url, headers=headers, **kwargs)

    transport = httpx.ASGITransport(app=app)
    async with AutoAuthAsyncClient(transport=transport, base_url="http://isolated.test") as client:
        yield client
    app.dependency_overrides.clear()
    _memory_store.clear()
