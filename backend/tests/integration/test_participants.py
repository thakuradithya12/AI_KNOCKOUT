"""Integration tests for participant registration and profile endpoints."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.participant import Participant


@pytest.mark.integration
class TestParticipantRegistration:
    """Test participant registration flow."""

    async def test_register_participant_success(self, api_client: httpx.AsyncClient):
        """Valid registration must return 201 with Participant ID and Access Key."""
        unique_name = f"Test_User_{uuid.uuid4().hex[:8]}"
        unique_email = f"{unique_name.lower()}@college.edu"

        payload = {
            "name": unique_name,
            "email": unique_email,
            "team_name": "Test Team",
        }

        response = await api_client.post("/api/v1/participants/register", json=payload)
        assert response.status_code == 201

        data = response.json()
        assert data["name"] == unique_name
        assert data["email"] == unique_email
        assert data["team_name"] == "Test Team"
        assert data["status"] == "ACTIVE"
        assert data["participant_id"].startswith("part_")
        assert data["access_key"].startswith("ak_")

    async def test_register_duplicate_name_rejected(self, api_client: httpx.AsyncClient):
        """Duplicate participant name must return 409 Conflict."""
        name = f"Dupe_User_{uuid.uuid4().hex[:8]}"
        payload = {"name": name, "email": f"{name.lower()}@college.edu"}

        # First registration
        r1 = await api_client.post("/api/v1/participants/register", json=payload)
        assert r1.status_code == 201

        # Second registration with same name
        r2 = await api_client.post(
            "/api/v1/participants/register",
            json={"name": name, "email": f"different_{name.lower()}@college.edu"},
        )
        assert r2.status_code == 409
        assert "already registered" in r2.json()["detail"]

    async def test_register_duplicate_email_rejected(self, api_client: httpx.AsyncClient):
        """Duplicate email must return 409 Conflict."""
        email = f"shared_{uuid.uuid4().hex[:8]}@college.edu"

        # User 1
        r1 = await api_client.post(
            "/api/v1/participants/register",
            json={"name": f"User_A_{uuid.uuid4().hex[:8]}", "email": email},
        )
        assert r1.status_code == 201

        # User 2 with same email
        r2 = await api_client.post(
            "/api/v1/participants/register",
            json={"name": f"User_B_{uuid.uuid4().hex[:8]}", "email": email},
        )
        assert r2.status_code == 409
        assert "already registered" in r2.json()["detail"]

    async def test_register_invalid_email_format(self, api_client: httpx.AsyncClient):
        """Malformed email must return 422 Unprocessable Entity."""
        payload = {
            "name": f"User_{uuid.uuid4().hex[:8]}",
            "email": "not-an-email",
        }
        response = await api_client.post("/api/v1/participants/register", json=payload)
        assert response.status_code == 422

    async def test_register_empty_name_rejected(self, api_client: httpx.AsyncClient):
        """Empty or whitespace-only name must return 422."""
        payload = {"name": "   "}
        response = await api_client.post("/api/v1/participants/register", json=payload)
        assert response.status_code == 422

    async def test_plaintext_access_key_not_stored_in_database(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Plaintext Access Key must NEVER be stored in the database."""
        name = f"Storage_Check_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert reg.status_code == 201
        data = reg.json()
        pid = data["participant_id"]
        raw_key = data["access_key"]

        # Directly query the database record
        stmt = select(Participant).where(Participant.participant_id == pid)
        participant = (await db_session.execute(stmt)).scalar_one()

        assert participant.access_key_hash != raw_key
        assert participant.access_key_hash.startswith("$scrypt$")
        assert raw_key not in participant.access_key_hash

    async def test_concurrent_duplicate_registration(self, api_client: httpx.AsyncClient):
        """Concurrent registration attempts with same name must result in exactly one success."""
        name = f"Race_User_{uuid.uuid4().hex[:8]}"
        payload = {"name": name}

        # Fire two concurrent registration requests
        responses = await asyncio.gather(
            api_client.post("/api/v1/participants/register", json=payload),
            api_client.post("/api/v1/participants/register", json=payload),
            return_exceptions=True,
        )

        status_codes = [r.status_code for r in responses if isinstance(r, httpx.Response)]
        assert 201 in status_codes
        assert 409 in status_codes
        assert len(status_codes) == 2


@pytest.mark.integration
class TestParticipantProfile:
    """Test /me profile endpoint and access boundaries."""

    async def test_get_me_unauthenticated_fails(self, api_client: httpx.AsyncClient):
        """Unauthenticated request to /me must return 401 Unauthorized."""
        response = await api_client.get("/api/v1/participants/me")
        assert response.status_code == 401

    async def test_get_me_authenticated_success(self, api_client: httpx.AsyncClient):
        """Authenticated request to /me must return participant profile without secrets."""
        name = f"Profile_User_{uuid.uuid4().hex[:8]}"
        reg_res = await api_client.post(
            "/api/v1/participants/register",
            json={"name": name},
        )
        assert reg_res.status_code == 201
        reg_data = reg_res.json()
        pid = reg_data["participant_id"]
        key = reg_data["access_key"]

        # Login to get token
        login_res = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        assert login_res.status_code == 200
        token = login_res.json()["access_token"]

        # Call /me
        me_res = await api_client.get(
            "/api/v1/participants/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_res.status_code == 200
        me_data = me_res.json()

        assert me_data["participant_id"] == pid
        assert me_data["name"] == name
        assert me_data["status"] == "ACTIVE"
        # Secrets MUST NOT be present in profile
        assert "access_key" not in me_data
        assert "access_key_hash" not in me_data

    async def test_cross_participant_isolation(self, api_client: httpx.AsyncClient):
        """Token of User A must only return User A's profile, never User B's."""
        # Create User A
        reg_a = await api_client.post(
            "/api/v1/participants/register",
            json={"name": f"User_A_{uuid.uuid4().hex[:8]}"},
        )
        data_a = reg_a.json()
        login_a = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": data_a["participant_id"], "access_key": data_a["access_key"]},
        )
        token_a = login_a.json()["access_token"]

        # Create User B
        reg_b = await api_client.post(
            "/api/v1/participants/register",
            json={"name": f"User_B_{uuid.uuid4().hex[:8]}"},
        )
        data_b = reg_b.json()

        # Query /me with Token A
        res_a = await api_client.get(
            "/api/v1/participants/me",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        profile_a = res_a.json()
        assert profile_a["participant_id"] == data_a["participant_id"]
        assert profile_a["participant_id"] != data_b["participant_id"]
