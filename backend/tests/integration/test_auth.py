"""Integration tests for authentication, sessions, and revocation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.participant import Participant, ParticipantSession, ParticipantStatus


@pytest.mark.integration
class TestAuthenticationFlow:
    """Test login, logout, token revocation, rate limiting, and lifecycle checks."""

    async def test_login_success(self, api_client: httpx.AsyncClient):
        """Valid credentials must return a bearer session token."""
        name = f"Auth_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post(
            "/api/v1/participants/register",
            json={"name": name},
        )
        assert reg.status_code == 201
        reg_data = reg.json()
        pid = reg_data["participant_id"]
        key = reg_data["access_key"]

        login_res = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        assert login_res.status_code == 200
        data = login_res.json()

        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0
        assert data["participant"]["participant_id"] == pid
        assert "access_key" not in data["participant"]

    async def test_login_wrong_key_fails(self, api_client: httpx.AsyncClient):
        """Wrong access key must return 401 Unauthorized with generic message."""
        name = f"Auth_Fail_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        pid = reg.json()["participant_id"]

        res = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": "ak_wrong_credential_value"},
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "Invalid credentials."

    async def test_login_nonexistent_participant_fails(self, api_client: httpx.AsyncClient):
        """Nonexistent participant must return 401 with identical generic message."""
        res = await api_client.post(
            "/api/v1/auth/login",
            json={
                "participant_id": "part_9999999999999999",
                "access_key": "ak_any_valid_format_key_12345",
            },
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "Invalid credentials."

    async def test_enumeration_resistance_identical_responses(self, api_client: httpx.AsyncClient):
        """Response structure and error message must be strictly identical for existent and non-existent IDs."""
        name = f"Enum_Test_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        existing_pid = reg.json()["participant_id"]

        r_exist = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": existing_pid, "access_key": "ak_wrong_key_1234567890"},
        )
        r_nonexist = await api_client.post(
            "/api/v1/auth/login",
            json={
                "participant_id": "part_0000000000000000",
                "access_key": "ak_wrong_key_1234567890",
            },
        )

        assert r_exist.status_code == r_nonexist.status_code == 401
        assert r_exist.json() == r_nonexist.json() == {"detail": "Invalid credentials."}

    async def test_login_suspended_participant_rejected(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Suspended participant must be rejected with 403 Forbidden."""
        name = f"Suspended_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        pid = reg.json()["participant_id"]
        key = reg.json()["access_key"]

        # Suspend participant in database
        stmt = (
            update(Participant)
            .where(Participant.participant_id == pid)
            .values(status=ParticipantStatus.SUSPENDED)
        )
        await db_session.execute(stmt)
        await db_session.commit()

        # Login attempt
        res = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        assert res.status_code == 403
        assert "suspended" in res.json()["detail"].lower()

    async def test_session_expiration(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Expired session token must be rejected with 401."""
        name = f"Expire_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        pid = reg.json()["participant_id"]
        key = reg.json()["access_key"]

        login = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        token = login.json()["access_token"]
        auth_header = {"Authorization": f"Bearer {token}"}

        # Verify works initially
        r1 = await api_client.get("/api/v1/participants/me", headers=auth_header)
        assert r1.status_code == 200

        # Expire session in DB
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        await db_session.execute(update(ParticipantSession).values(expires_at=past_time))
        await db_session.commit()

        # Must now be rejected
        r2 = await api_client.get("/api/v1/participants/me", headers=auth_header)
        assert r2.status_code == 401

    async def test_logout_and_token_revocation(self, api_client: httpx.AsyncClient):
        """Logging out must revoke token so subsequent requests fail."""
        name = f"Logout_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        pid = reg.json()["participant_id"]
        key = reg.json()["access_key"]

        login = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        token = login.json()["access_token"]
        auth_header = {"Authorization": f"Bearer {token}"}

        # 1. /me works before logout
        me1 = await api_client.get("/api/v1/participants/me", headers=auth_header)
        assert me1.status_code == 200

        # 2. Logout
        logout_res = await api_client.post("/api/v1/auth/logout", headers=auth_header)
        assert logout_res.status_code == 200
        assert logout_res.json()["status"] == "ok"

        # 3. /me fails after logout
        me2 = await api_client.get("/api/v1/participants/me", headers=auth_header)
        assert me2.status_code == 401

    async def test_malformed_auth_headers(self, api_client: httpx.AsyncClient):
        """Malformed authorization headers must return 401."""
        # Missing scheme
        r1 = await api_client.get(
            "/api/v1/participants/me", headers={"Authorization": "just_a_token"}
        )
        assert r1.status_code == 401

        # Wrong scheme
        r2 = await api_client.get(
            "/api/v1/participants/me", headers={"Authorization": "Basic 12345"}
        )
        assert r2.status_code == 401

        # Empty token (missing token parameter)
        r3 = await api_client.get("/api/v1/participants/me", headers={"Authorization": "Bearer"})
        assert r3.status_code == 401
