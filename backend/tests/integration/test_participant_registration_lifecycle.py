"""Comprehensive integration test suite for participant registration lifecycle,
identity integrity, idempotency, credential security, and rate limiting.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import BuildSession, ParticipantSubmissionAllowance
from app.models.participant import Participant
from app.services.rate_limiter import RateLimiter


@pytest.mark.integration
class TestParticipantRegistrationFlow:
    """Verify registration input handling, validation, normalization, and credentials."""

    async def test_unauthenticated_registration_rejected_with_401(self, api_client: httpx.AsyncClient):
        """Anonymous registration attempts without organizer authorization must be rejected with 401."""
        res = await api_client.post(
            "/api/v1/participants/register",
            json={"name": "Attacker"},
            headers={"X-Anonymous": "true"},
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "ORGANIZER_AUTHENTICATION_REQUIRED"

    async def test_unauthenticated_admin_registration_rejected_with_401(self, api_client: httpx.AsyncClient):
        """Anonymous registration attempts to admin endpoint must be rejected with 401."""
        res = await api_client.post(
            "/api/v1/admin/participants/register",
            json={"name": "Attacker"},
            headers={"X-Anonymous": "true"},
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "ORGANIZER_AUTHENTICATION_REQUIRED"

    async def test_admin_registration_endpoint_succeeds_with_organizer_credentials(
        self, api_client: httpx.AsyncClient
    ):
        """Admin registration endpoint successfully creates participant under organizer authority."""
        name = f"AdminReg_{uuid.uuid4().hex[:8]}"
        res = await api_client.post(
            "/api/v1/admin/participants/register",
            json={"name": name, "team_name": "Admin Team", "email": "adminteam@example.com"},
            headers={"X-Organizer-Secret": "isolated-test-only-secret-key-0123456789"},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == name
        assert data["team_name"] == "Admin Team"
        assert data["email"] == "adminteam@example.com"
        assert data["status"] == "ACTIVE"
        assert data["participant_id"].startswith("part_")
        assert data["access_key"].startswith("ak_")

    async def test_register_with_minimal_valid_fields(self, api_client: httpx.AsyncClient):
        """Registering with only name succeeds with correct formats and status."""
        name = f"Solo_{uuid.uuid4().hex[:8]}"
        res = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == name
        assert data["team_name"] is None
        assert data["email"] is None
        assert data["status"] == "ACTIVE"
        assert data["participant_id"].startswith("part_")
        assert len(data["participant_id"]) == 21
        assert data["access_key"].startswith("ak_")
        assert len(data["access_key"]) == 46
        assert "created_at" in data

    async def test_register_normalizes_empty_strings_to_none(self, api_client: httpx.AsyncClient):
        """Empty team_name and email strings from HTML forms normalize to None."""
        name = f"Norm_{uuid.uuid4().hex[:8]}"
        res = await api_client.post(
            "/api/v1/participants/register",
            json={"name": name, "team_name": "", "email": ""},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == name
        assert data["team_name"] is None
        assert data["email"] is None

    async def test_register_normalizes_email_to_lowercase(self, api_client: httpx.AsyncClient):
        """Email is trimmed and converted to lowercase."""
        name = f"EmailCase_{uuid.uuid4().hex[:8]}"
        res = await api_client.post(
            "/api/v1/participants/register",
            json={"name": name, "email": "  USER.TEAM@COLLEGE.EDU  "},
        )
        assert res.status_code == 201
        assert res.json()["email"] == "user.team@college.edu"

    @pytest.mark.parametrize(
        "invalid_name,expected_detail",
        [
            ("", "String should have at least 2 characters"),
            ("   ", "String should have at least 2 characters"),
            ("A", "String should have at least 2 characters"),
            ("X" * 101, "String should have at most 100 characters"),
            ("<script>alert(1)</script>", "Name contains prohibited characters"),
            ("Participant\x00Name", "Name contains prohibited characters"),
            ("Participant<b>Bold</b>", "Name contains prohibited characters"),
        ],
    )
    async def test_register_rejects_invalid_names(
        self, api_client: httpx.AsyncClient, invalid_name: str, expected_detail: str
    ):
        """Invalid, oversized, or malicious participant names are rejected with 422."""
        res = await api_client.post(
            "/api/v1/participants/register",
            json={"name": invalid_name},
        )
        assert res.status_code == 422
        assert res.json()["detail"] == "Request validation failed."

        # Verify underlying Pydantic schema validation detail
        from pydantic import ValidationError
        from app.schemas.participant import ParticipantRegisterRequest

        with pytest.raises(ValidationError) as exc_info:
            ParticipantRegisterRequest(name=invalid_name)
        assert expected_detail in str(exc_info.value)

    async def test_registration_rate_limiting_triggers_429(
        self, api_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        """Rapid registrations exhaust allowance and trigger 429 Too Many Requests."""
        from app.core.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "rate_limit_register_attempts", 3)
        limit = 3

        client_ip = api_client.base_url.host

        await RateLimiter.reset("register", client_ip)
        await RateLimiter.reset("register", "testclient")
        await RateLimiter.reset("register", "127.0.0.1")

        hit_429 = False
        retry_after_present = False
        try:
            for _ in range(limit + 5):
                res = await api_client.post(
                    "/api/v1/participants/register",
                    json={"name": f"Rate_{uuid.uuid4().hex[:8]}"},
                )
                if res.status_code == 429:
                    hit_429 = True
                    retry_after_present = "retry-after" in res.headers
                    assert "Registration rate limit exceeded" in res.json()["detail"]
                    break
        finally:
            await RateLimiter.reset("register", client_ip)
            await RateLimiter.reset("register", "testclient")
            await RateLimiter.reset("register", "127.0.0.1")

        assert hit_429 is True
        assert retry_after_present is True

    @pytest.mark.parametrize(
        "invalid_team",
        [
            "Team<script>",
            "Team\x1bEscape",
            "T" * 105,
        ],
    )
    async def test_register_rejects_invalid_team_names(
        self, api_client: httpx.AsyncClient, invalid_team: str
    ):
        """Team names with prohibited characters or exceeding max length return 422."""
        res = await api_client.post(
            "/api/v1/participants/register",
            json={"name": f"Valid_{uuid.uuid4().hex[:6]}", "team_name": invalid_team},
        )
        assert res.status_code == 422

    @pytest.mark.parametrize(
        "invalid_email",
        [
            "not-an-email",
            "@missing-user.com",
            "missing-domain@",
            "spaces in@email.com",
        ],
    )
    async def test_register_rejects_invalid_email_format(
        self, api_client: httpx.AsyncClient, invalid_email: str
    ):
        """Malformed email strings return 422."""
        res = await api_client.post(
            "/api/v1/participants/register",
            json={"name": f"Valid_{uuid.uuid4().hex[:6]}", "email": invalid_email},
        )
        assert res.status_code == 422


@pytest.mark.integration
class TestParticipantDatabaseIntegrity:
    """Verify database uniqueness constraints, transactional atomicity, and hash protection."""

    async def test_access_key_stored_as_scrypt_hash_only(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Plaintext access key is never written to DB; only scrypt hash is stored."""
        name = f"HashCheck_{uuid.uuid4().hex[:8]}"
        res = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert res.status_code == 201
        data = res.json()
        pid = data["participant_id"]
        raw_key = data["access_key"]

        stmt = select(Participant).where(Participant.participant_id == pid)
        row = (await db_session.execute(stmt)).scalar_one()

        assert row.access_key_hash != raw_key
        assert row.access_key_hash.startswith("$scrypt$")
        assert raw_key not in row.access_key_hash

    async def test_atomic_creation_of_companion_records(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Registration transaction atomically commits Participant, BuildSession, and Allowance."""
        name = f"Atomic_{uuid.uuid4().hex[:8]}"
        res = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert res.status_code == 201
        pid = res.json()["participant_id"]

        # 1. Participant exists
        p_row = (await db_session.execute(select(Participant).where(Participant.participant_id == pid))).scalar_one_or_none()
        assert p_row is not None
        assert p_row.name == name

        # 2. BuildSession created with start_source REGISTRATION
        bs_row = (await db_session.execute(select(BuildSession).where(BuildSession.participant_id == pid))).scalar_one_or_none()
        assert bs_row is not None
        assert bs_row.start_source == "REGISTRATION"

        # 3. Allowance created
        allow_row = (await db_session.execute(select(ParticipantSubmissionAllowance).where(ParticipantSubmissionAllowance.participant_id == pid))).scalar_one_or_none()
        assert allow_row is not None
        assert allow_row.submission_limit == 10

    async def test_duplicate_name_enforces_clean_rollback(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Duplicate registration attempt fails with 409 and leaves no orphaned companion records."""
        name = f"Rollback_{uuid.uuid4().hex[:8]}"
        r1 = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert r1.status_code == 201

        # Second attempt with same name
        r2 = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert r2.status_code == 409
        assert "already registered" in r2.json()["detail"]

        # Ensure only 1 record exists in the database
        stmt = select(Participant).where(Participant.name == name)
        rows = (await db_session.execute(stmt)).scalars().all()
        assert len(rows) == 1


@pytest.mark.integration
class TestParticipantIdempotency:
    """Verify server-side idempotency key replay, collision detection, and validation."""

    async def test_idempotent_replay_returns_identical_credentials(
        self, api_client: httpx.AsyncClient
    ):
        """Replaying registration with identical payload and Idempotency-Key returns cached credentials."""
        name = f"Idemp_{uuid.uuid4().hex[:8]}"
        payload = {"name": name, "team_name": "Idemp Team"}
        idempotency_key = f"key_{uuid.uuid4().hex}"

        headers = {"Idempotency-Key": idempotency_key}
        res1 = await api_client.post("/api/v1/participants/register", json=payload, headers=headers)
        assert res1.status_code == 201
        data1 = res1.json()

        # Replay same request with same key
        res2 = await api_client.post("/api/v1/participants/register", json=payload, headers=headers)
        assert res2.status_code == 201
        data2 = res2.json()

        # Credentials and ID must match exactly
        assert data1["participant_id"] == data2["participant_id"]
        assert data1["access_key"] == data2["access_key"]
        assert data1["name"] == data2["name"]

    async def test_idempotency_key_reused_with_different_payload_returns_409(
        self, api_client: httpx.AsyncClient
    ):
        """Using the same Idempotency-Key with a different payload returns 409 Conflict."""
        idempotency_key = f"mismatch_{uuid.uuid4().hex}"
        headers = {"Idempotency-Key": idempotency_key}

        payload1 = {"name": f"User1_{uuid.uuid4().hex[:6]}"}
        res1 = await api_client.post("/api/v1/participants/register", json=payload1, headers=headers)
        assert res1.status_code == 201

        payload2 = {"name": f"User2_{uuid.uuid4().hex[:6]}"}
        res2 = await api_client.post("/api/v1/participants/register", json=payload2, headers=headers)
        assert res2.status_code == 409
        assert "Idempotency key was previously used with a different request payload" in res2.json()["detail"]

    async def test_invalid_idempotency_key_format_returns_422(
        self, api_client: httpx.AsyncClient
    ):
        """Idempotency-Key exceeding 128 characters or containing control chars returns 422."""
        res_oversized = await api_client.post(
            "/api/v1/participants/register",
            json={"name": f"Valid_{uuid.uuid4().hex[:6]}"},
            headers={"Idempotency-Key": "k" * 129},
        )
        assert res_oversized.status_code == 422

        res_control = await api_client.post(
            "/api/v1/participants/register",
            json={"name": f"Valid_{uuid.uuid4().hex[:6]}"},
            headers={"Idempotency-Key": "key\x00bad"},
        )
        assert res_control.status_code == 422


@pytest.mark.integration
class TestParticipantRegistrationToLoginLifecycle:
    """Verify complete flow: register -> login -> profile -> authenticated portal actions -> logout."""

    async def test_full_lifecycle(self, api_client: httpx.AsyncClient):
        """Participant registers, logs in, accesses protected endpoints, and signs out."""
        name = f"Lifecycle_{uuid.uuid4().hex[:8]}"
        reg_res = await api_client.post(
            "/api/v1/participants/register",
            json={"name": name, "team_name": "Lifecycle Team"},
        )
        assert reg_res.status_code == 201
        reg_data = reg_res.json()
        pid = reg_data["participant_id"]
        key = reg_data["access_key"]

        # 1. Login with correct credentials
        login_res = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        assert login_res.status_code == 200
        token_data = login_res.json()
        token = token_data["access_token"]
        assert token_data["token_type"].lower() == "bearer"
        assert token_data["expires_in"] > 0
        auth_headers = {"Authorization": f"Bearer {token}"}

        # 2. Access /participants/me
        me_res = await api_client.get("/api/v1/participants/me", headers=auth_headers)
        assert me_res.status_code == 200
        me_data = me_res.json()
        assert me_data["participant_id"] == pid
        assert me_data["name"] == name
        assert me_data["team_name"] == "Lifecycle Team"
        assert "access_key" not in me_data
        assert "access_key_hash" not in me_data

        # 3. Access protected portal endpoints
        status_res = await api_client.get(
            "/api/v1/participants/me/event-status", headers=auth_headers
        )
        assert status_res.status_code == 200

        quota_res = await api_client.get(
            "/api/v1/submissions/quota", headers=auth_headers
        )
        assert quota_res.status_code == 200
        assert quota_res.json()["remaining_submissions"] == 10

        # 4. Attempt login with bad credentials fails
        bad_login = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": "ak_wrongkey_0000000000000000000000000"},
        )
        assert bad_login.status_code in (401, 403)

        # 5. Sign out
        logout_res = await api_client.post(
            "/api/v1/auth/logout", headers=auth_headers
        )
        assert logout_res.status_code == 200

        # 6. Post-logout access with revoked token fails
        post_logout = await api_client.get("/api/v1/participants/me", headers=auth_headers)
        assert post_logout.status_code == 401
