"""Unit tests for rate limiter service."""

from __future__ import annotations

import pytest

from app.services.rate_limiter import RateLimiter, _memory_store


class TestRateLimiter:
    """Test rate limiting logic."""

    @pytest.mark.asyncio
    async def test_in_memory_fallback_rate_limit(self, monkeypatch):
        """Rate limiter must enforce limits even when Redis is down."""

        # Force Redis client to raise exception
        def failing_redis():
            raise ConnectionError("Redis unreachable")

        monkeypatch.setattr("app.services.rate_limiter.get_redis", failing_redis)

        key = "test_user_ip"
        prefix = "unit_test"

        # Clear memory store for clean test
        _memory_store.clear()

        # Attempts 1 and 2: Allowed (limit = 2)
        allowed1, _ = await RateLimiter.check_rate_limit(
            prefix, key, max_attempts=2, window_seconds=60
        )
        assert allowed1 is True

        allowed2, _ = await RateLimiter.check_rate_limit(
            prefix, key, max_attempts=2, window_seconds=60
        )
        assert allowed2 is True

        # Attempt 3: Blocked
        allowed3, retry_after = await RateLimiter.check_rate_limit(
            prefix, key, max_attempts=2, window_seconds=60
        )
        assert allowed3 is False
        assert retry_after > 0

        # Reset
        await RateLimiter.reset(prefix, key)
        allowed4, _ = await RateLimiter.check_rate_limit(
            prefix, key, max_attempts=2, window_seconds=60
        )
        assert allowed4 is True
