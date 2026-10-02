"""Readiness response shape checks against isolated, mocked dependencies."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
class TestRedisReadinessShape:
    """Test Redis readiness fields without contacting Redis."""

    async def test_readiness_confirms_redis_connection(self, api_client: httpx.AsyncClient):
        """Readiness endpoint should confirm Redis is connected."""
        response = await api_client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["dependencies"]["redis"]["status"] == "ok"
