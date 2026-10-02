"""HTTP-level health/readiness checks against isolated in-process fixtures."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
class TestHealthEndpoint:
    """Test liveness endpoint."""

    async def test_health_returns_200(self, api_client: httpx.AsyncClient):
        """GET /health should return 200 with status ok."""
        response = await api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "ai-knockout"

    async def test_health_has_request_id(self, api_client: httpx.AsyncClient):
        """Health response should include X-Request-ID header."""
        response = await api_client.get("/health")
        assert "X-Request-ID" in response.headers

    async def test_health_has_security_headers(self, api_client: httpx.AsyncClient):
        """Responses should include security headers."""
        response = await api_client.get("/health")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"

    async def test_v1_health_returns_200(self, api_client: httpx.AsyncClient):
        """GET /api/v1/health should also work."""
        response = await api_client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.integration
class TestReadinessEndpoint:
    """Test readiness endpoint."""

    async def test_ready_returns_200_when_healthy(self, api_client: httpx.AsyncClient):
        """GET /ready should return 200 with dependency statuses."""
        response = await api_client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["dependencies"]["postgresql"]["status"] == "ok"
        assert data["dependencies"]["redis"]["status"] == "ok"

    async def test_ready_returns_503_when_unhealthy(self, monkeypatch):
        """GET /ready should return 503 when a dependency is down."""
        from app.main import app

        async def mock_failing_db():
            return {"status": "error", "detail": "connection refused"}

        monkeypatch.setattr("app.api.v1.health.check_database_health", mock_failing_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "degraded"
            assert data["dependencies"]["postgresql"]["status"] == "error"


@pytest.mark.integration
class TestVersionEndpoint:
    """Test version endpoint."""

    async def test_version_returns_200(self, api_client: httpx.AsyncClient):
        """GET /version should return version info."""
        response = await api_client.get("/version")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "AI Knockout"
        assert "version" in data
        assert "environment" in data


@pytest.mark.integration
class TestRequestCorrelation:
    """Test request correlation ID propagation."""

    async def test_request_id_propagation(self, api_client: httpx.AsyncClient):
        """Client-provided X-Request-ID should be returned."""
        custom_id = "test-correlation-id-12345"
        response = await api_client.get("/health", headers={"X-Request-ID": custom_id})
        assert response.headers.get("X-Request-ID") == custom_id

    async def test_auto_generated_request_id(self, api_client: httpx.AsyncClient):
        """Request without X-Request-ID should get auto-generated one."""
        response = await api_client.get("/health")
        request_id = response.headers.get("X-Request-ID")
        assert request_id is not None
        assert len(request_id) > 0
