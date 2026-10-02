"""Redis client management.

Provides:
- Connection lifecycle (init/close)
- Health checking
- Clean dependency injection
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

logger = logging.getLogger("ai-knockout.redis")

# Module-level Redis client — initialized at startup
_redis_client: aioredis.Redis | None = None


def init_redis(
    redis_url: str,
    socket_timeout: int = 5,
    socket_connect_timeout: int = 5,
    max_connections: int = 20,
) -> None:
    """Initialize the Redis client.

    Must be called once during application startup.
    """
    global _redis_client

    _redis_client = aioredis.from_url(
        redis_url,
        socket_timeout=socket_timeout,
        socket_connect_timeout=socket_connect_timeout,
        max_connections=max_connections,
        decode_responses=True,
    )

    logger.info("Redis client initialized")


async def close_redis() -> None:
    """Close the Redis client.

    Must be called during application shutdown.
    """
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        logger.info("Redis client closed")
    _redis_client = None


def get_redis() -> aioredis.Redis:
    """Get the Redis client instance (dependency injection)."""
    if _redis_client is None:
        raise RuntimeError("Redis client not initialized. Call init_redis() first.")
    return _redis_client


async def check_redis_health() -> dict:
    """Check Redis connectivity for readiness endpoint."""
    if _redis_client is None:
        return {"status": "error", "detail": "Client not initialized"}
    try:
        pong = await _redis_client.ping()
        if pong:
            return {"status": "ok"}
        return {"status": "error", "detail": "Ping returned False"}
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return {"status": "error", "detail": str(e)}
