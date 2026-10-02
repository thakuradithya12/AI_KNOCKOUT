"""Database session and connection management.

Provides async SQLAlchemy engine and session factory with:
- Connection pooling
- Proper timeouts
- Startup/shutdown lifecycle
- Health checking
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger("ai-knockout.db")

# Module-level engine and session factory — initialized at startup
_engine = None
_session_factory = None


def init_engine(
    database_url: str,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: int = 30,
    pool_recycle: int = 1800,
    echo: bool = False,
) -> None:
    """Initialize the async database engine.

    Must be called once during application startup.
    """
    global _engine, _session_factory

    _engine = create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=pool_recycle,
        echo=echo,
        pool_pre_ping=True,
    )

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    logger.info("Database engine initialized", extra={"pool_size": pool_size})


async def close_engine() -> None:
    """Close the database engine and all connections.

    Must be called during application shutdown.
    """
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        logger.info("Database engine closed")
    _engine = None
    _session_factory = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session (dependency injection)."""
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    async with _session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_database_health() -> dict:
    """Check database connectivity for readiness endpoint."""
    if _engine is None:
        return {"status": "error", "detail": "Engine not initialized"}
    try:
        async with _engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            result.close()
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return {"status": "error", "detail": str(e)}
