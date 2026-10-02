"""Integration tests for Phase 2 rate limiting, abuse protection, and session resilience."""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.config import get_settings


@pytest.mark.integration
class TestAbuseProtectionAndRateLimiting:
    """Test brute-force protection and rate limiting on authentication endpoints."""

    async def test_login_rate_limiting_enforced(self, api_client: httpx.AsyncClient):
        """Repeated failed login attempts must trigger HTTP 429 Too Many Requests."""
        settings = get_settings()
        unique_pid = f"part_ratelimit_{uuid.uuid4().hex[:8]}"

        # Exhaust the rate limit for this specific participant ID
        rate_limited = False
        for _ in range(settings.rate_limit_login_attempts + 3):
            res = await api_client.post(
                "/api/v1/auth/login",
                json={"participant_id": unique_pid, "access_key": "ak_wrong_key_12345"},
            )
            if res.status_code == 429:
                rate_limited = True
                assert "Retry-After" in res.headers
                assert "rate limit exceeded" in res.json()["detail"].lower()
                break

        assert rate_limited is True

        # Test fixtures use an in-memory limiter and clear it after this isolated test.


@pytest.mark.integration
class TestSessionPersistenceAndResilience:
    """Test session token validity and persistence."""

    async def test_session_survives_multiple_queries(self, api_client: httpx.AsyncClient):
        """A bearer token must remain valid across multiple consecutive requests."""
        name = f"Multi_Req_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert reg.status_code == 201
        data = reg.json()

        login = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": data["participant_id"], "access_key": data["access_key"]},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Make 5 consecutive authenticated requests
        for _ in range(5):
            res = await api_client.get("/api/v1/participants/me", headers=headers)
            assert res.status_code == 200
            assert res.json()["participant_id"] == data["participant_id"]
