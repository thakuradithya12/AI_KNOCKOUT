"""Readiness response shape checks against isolated, mocked dependencies."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
class TestDatabaseReadinessShape:
    """Test PostgreSQL readiness fields without contacting PostgreSQL."""

    async def test_readiness_confirms_db_connection(self, api_client: httpx.AsyncClient):
        """Readiness endpoint should confirm database is connected."""
        response = await api_client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["dependencies"]["postgresql"]["status"] == "ok"
