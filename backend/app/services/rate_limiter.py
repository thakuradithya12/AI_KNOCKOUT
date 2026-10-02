"""Redis-backed rate limiter with memory fallback.

Mitigates brute-force credential stuffing and DoS on authentication endpoints.
Fail-safe design: if Redis is unavailable, rate limiting degrades to an
in-memory cache instead of bypassing security controls.
"""

from __future__ import annotations

import logging
import time

from app.services.redis import get_redis

logger = logging.getLogger("ai-knockout.rate_limiter")

# In-memory fallback storage: key -> (count, expires_at)
_memory_store: dict[str, tuple[int, float]] = {}


class RateLimiter:
    """Sliding-window counter rate limiter."""

    @staticmethod
    async def check_rate_limit(
        key_prefix: str,
        identifier: str,
        max_attempts: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        """Check if action is permitted under the rate limit.

        Returns:
            Tuple of (is_allowed: bool, retry_after_seconds: int)
        """
        redis_key = f"aik:rl:{key_prefix}:{identifier}"

        # 1. Try Redis
        try:
            redis_client = get_redis()
            current = await redis_client.incr(redis_key)
            if current == 1:
                await redis_client.expire(redis_key, window_seconds)

            if current > max_attempts:
                ttl = await redis_client.ttl(redis_key)
                retry_after = max(1, ttl if ttl > 0 else window_seconds)
                return False, retry_after
            return True, 0
        except Exception as e:
            logger.warning(f"Redis unavailable for rate limiter; failing back to in-memory: {e}")

        # 2. In-Memory Fail-Safe Fallback
        now = time.monotonic()
        # Clean expired entries occasionally
        expired_keys = [k for k, (_, exp) in _memory_store.items() if exp <= now]
        for k in expired_keys:
            _memory_store.pop(k, None)

        if redis_key in _memory_store:
            count, exp = _memory_store[redis_key]
            if now < exp:
                new_count = count + 1
                _memory_store[redis_key] = (new_count, exp)
                if new_count > max_attempts:
                    retry_after = max(1, int(exp - now))
                    return False, retry_after
                return True, 0
            else:
                _memory_store[redis_key] = (1, now + window_seconds)
                return True, 0
        else:
            _memory_store[redis_key] = (1, now + window_seconds)
            return True, 0

    @staticmethod
    async def reset(key_prefix: str, identifier: str) -> None:
        """Reset the rate limit counter for a specific identifier."""
        redis_key = f"aik:rl:{key_prefix}:{identifier}"
        try:
            redis_client = get_redis()
            await redis_client.delete(redis_key)
        except Exception:
            pass
        _memory_store.pop(redis_key, None)
