"""Integration tests for authorized participant creation workflow in organizer console.

Tests cover:
1. Authorized participant creation via organizer session + CSRF token
2. Unauthorized rejection (401 without organizer session, 403 on missing/invalid CSRF)
3. Duplicate input rejection (409 Conflict for existing name or email)
4. Invalid input rejection (422 Unprocessable Entity for invalid name/email)
5. Credential issuance verification (part_ and ak_ formats, one-time access key)
6. Idempotency behavior for creation
7. Participant login with issued credentials and access to authenticated workspace
8. Session revocation (logout and organizer administrative revocation)
"""

from __future__ import annotations

import uuid
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.main import app
from app.models.organizer import OrganizerAccount
from app.models.participant import Participant


@pytest.fixture
async def organizer_session_tokens(api_client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch):
    """Sets up an allowlisted organizer account, signs in, and yields session & CSRF tokens."""
    org_email = f"org_{uuid.uuid4().hex[:8]}@event.org"
    org_password = "OrganizerPassword2026!"

    monkeypatch.setenv("ORGANIZER_ALLOWED_EMAILS", org_email)
    new_settings = Settings(_env_file=None)
    app.dependency_overrides[get_settings] = lambda: new_settings

    # Register allowlisted organizer
    reg_resp = await api_client.post(
        "/api/v1/admin/auth/register",
        json={
            "email": org_email,
            "name": "Head Arbiter",
            "password": org_password,
        },
    )
    assert reg_resp.status_code == 201

    # Login to obtain session
    login_resp = await api_client.post(
        "/api/v1/admin/auth/login-password",
        json={"email": org_email, "password": org_password},
    )
    assert login_resp.status_code == 200, login_resp.text
    data = login_resp.json()
    session_token = data["session_token"]
    csrf_token = data["csrf_token"]

    yield {
        "email": org_email,
        "session_token": session_token,
        "csrf_token": csrf_token,
    }

    app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_unauthorized_participant_creation_rejected(api_client: httpx.AsyncClient):
    """Requests without organizer session must be rejected with 401."""
    # Direct admin registration without credentials
    res1 = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": "Attacker"},
        headers={"X-Anonymous": "true"},
    )
    assert res1.status_code == 401
    assert "ORGANIZER_AUTHENTICATION_REQUIRED" in res1.text

    # Public registration without credentials
    res2 = await api_client.post(
        "/api/v1/participants/register",
        json={"name": "Attacker"},
        headers={"X-Anonymous": "true"},
    )
    assert res2.status_code == 401
    assert "ORGANIZER_AUTHENTICATION_REQUIRED" in res2.text


@pytest.mark.asyncio
async def test_csrf_protection_enforced_on_organizer_registration(
    api_client: httpx.AsyncClient, organizer_session_tokens: dict
):
    """Requests with organizer session cookie but missing or invalid CSRF token must fail with 403."""
    session_token = organizer_session_tokens["session_token"]
    name = f"Test_{uuid.uuid4().hex[:8]}"

    # Missing X-CSRF-Token
    res_no_csrf = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": name},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert res_no_csrf.status_code == 403, res_no_csrf.text
    assert "CSRF_VERIFICATION_FAILED" in res_no_csrf.text

    # Invalid X-CSRF-Token
    res_bad_csrf = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": name},
        headers={
            "Authorization": f"Bearer {session_token}",
            "X-CSRF-Token": "invalid-csrf-token-12345",
        },
    )
    assert res_bad_csrf.status_code == 403, res_bad_csrf.text
    assert "CSRF_VERIFICATION_FAILED" in res_bad_csrf.text


@pytest.mark.asyncio
async def test_authorized_participant_creation_and_credential_issuance(
    api_client: httpx.AsyncClient, organizer_session_tokens: dict, db_session: AsyncSession
):
    """Organizer with valid session and CSRF token creates participant; returns valid credentials once."""
    session_token = organizer_session_tokens["session_token"]
    csrf_token = organizer_session_tokens["csrf_token"]
    name = f"AlphaKnight_{uuid.uuid4().hex[:8]}"
    team = "Team Deep"
    email = f"alpha_{uuid.uuid4().hex[:6]}@college.edu"
    idempotency_key = str(uuid.uuid4())

    res = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": name, "team_name": team, "email": email},
        headers={
            "Authorization": f"Bearer {session_token}",
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": idempotency_key,
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["name"] == name
    assert data["team_name"] == team
    assert data["email"] == email.lower()
    assert data["status"] == "ACTIVE"
    assert data["participant_id"].startswith("part_")
    assert len(data["participant_id"]) == 21
    assert data["access_key"].startswith("ak_")
    assert len(data["access_key"]) == 46
    assert "created_at" in data

    # Verify participant is in database
    p_db = await db_session.scalar(
        select(Participant).where(Participant.participant_id == data["participant_id"])
    )
    assert p_db is not None
    assert p_db.name == name

    # Idempotent replay with same key returns identical credentials
    res_replay = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": name, "team_name": team, "email": email},
        headers={
            "Authorization": f"Bearer {session_token}",
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": idempotency_key,
        },
    )
    assert res_replay.status_code == 201
    assert res_replay.json() == data


@pytest.mark.asyncio
async def test_duplicate_participant_inputs_rejected(
    api_client: httpx.AsyncClient, organizer_session_tokens: dict
):
    """Duplicate participant names and emails must return 409 Conflict."""
    session_token = organizer_session_tokens["session_token"]
    csrf_token = organizer_session_tokens["csrf_token"]
    headers = {"Authorization": f"Bearer {session_token}", "X-CSRF-Token": csrf_token}

    name = f"Unique_{uuid.uuid4().hex[:8]}"
    email = f"unique_{uuid.uuid4().hex[:6]}@domain.org"

    # Initial registration
    r1 = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": name, "email": email},
        headers=headers,
    )
    assert r1.status_code == 201

    # Duplicate name
    r_dup_name = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": name, "email": f"other_{uuid.uuid4().hex[:6]}@domain.org"},
        headers=headers,
    )
    assert r_dup_name.status_code == 409
    assert "name is already registered" in r_dup_name.json()["detail"].lower()

    # Duplicate email
    r_dup_email = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": f"Other_{uuid.uuid4().hex[:8]}", "email": email},
        headers=headers,
    )
    assert r_dup_email.status_code == 409
    assert "already registered" in r_dup_email.json()["detail"].lower()


@pytest.mark.asyncio
async def test_invalid_participant_inputs_rejected(
    api_client: httpx.AsyncClient, organizer_session_tokens: dict
):
    """Validation errors on name or email must return 422 Unprocessable Entity."""
    session_token = organizer_session_tokens["session_token"]
    csrf_token = organizer_session_tokens["csrf_token"]
    headers = {"Authorization": f"Bearer {session_token}", "X-CSRF-Token": csrf_token}

    # Name too short
    r_short = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": "A"},
        headers=headers,
    )
    assert r_short.status_code == 422

    # Name too long
    r_long = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": "X" * 101},
        headers=headers,
    )
    assert r_long.status_code == 422

    # Prohibited characters in name
    r_xss = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": "<script>alert(1)</script>"},
        headers=headers,
    )
    assert r_xss.status_code == 422

    # Malformed email
    r_bad_email = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": "ValidName", "email": "not-an-email"},
        headers=headers,
    )
    assert r_bad_email.status_code == 422


@pytest.mark.asyncio
async def test_participant_login_and_workspace_access(
    api_client: httpx.AsyncClient, organizer_session_tokens: dict
):
    """Newly created participant signs in with issued credentials and accesses workspace."""
    session_token = organizer_session_tokens["session_token"]
    csrf_token = organizer_session_tokens["csrf_token"]

    name = f"Signer_{uuid.uuid4().hex[:8]}"
    team = "Signer Team"

    # 1. Organizer creates participant
    create_resp = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": name, "team_name": team},
        headers={"Authorization": f"Bearer {session_token}", "X-CSRF-Token": csrf_token},
    )
    assert create_resp.status_code == 201
    creds = create_resp.json()
    participant_id = creds["participant_id"]
    access_key = creds["access_key"]

    # 2. Participant signs in via /auth/login
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": participant_id, "access_key": access_key},
    )
    assert login_resp.status_code == 200, login_resp.text
    auth_data = login_resp.json()
    assert auth_data["token_type"] == "bearer"
    assert auth_data["access_token"]
    p_token = auth_data["access_token"]

    # 3. Participant accesses /participants/me
    me_resp = await api_client.get(
        "/api/v1/participants/me",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert me_resp.status_code == 200, me_resp.text
    profile = me_resp.json()
    assert profile["participant_id"] == participant_id
    assert profile["name"] == name
    assert profile["team_name"] == team
    assert profile["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_participant_session_revocation(
    api_client: httpx.AsyncClient, organizer_session_tokens: dict
):
    """Test participant session revocation both self-service and by organizer."""
    session_token = organizer_session_tokens["session_token"]
    csrf_token = organizer_session_tokens["csrf_token"]

    name = f"Revokee_{uuid.uuid4().hex[:8]}"
    create_resp = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": name},
        headers={"Authorization": f"Bearer {session_token}", "X-CSRF-Token": csrf_token},
    )
    assert create_resp.status_code == 201
    creds = create_resp.json()

    # Self-service logout
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": creds["participant_id"], "access_key": creds["access_key"]},
    )
    p_token = login_resp.json()["access_token"]

    logout_resp = await api_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert logout_resp.status_code == 200

    # Token is now revoked
    me_resp = await api_client.get(
        "/api/v1/participants/me",
        headers={"Authorization": f"Bearer {p_token}"},
    )
    assert me_resp.status_code == 401

    # Organizer-initiated session revocation
    login_resp2 = await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": creds["participant_id"], "access_key": creds["access_key"]},
    )
    p_token2 = login_resp2.json()["access_token"]

    # Verify session is valid
    assert (await api_client.get("/api/v1/participants/me", headers={"Authorization": f"Bearer {p_token2}"})).status_code == 200

    # Organizer revokes sessions
    revoke_resp = await api_client.post(
        f"/api/v1/admin/participants/{creds['participant_id']}/revoke-sessions",
        json={"reason": "Security audit", "actor_id": "organizer-admin"},
        headers={"Authorization": f"Bearer {session_token}", "X-CSRF-Token": csrf_token},
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["audited"] is True

    # Participant session is now revoked
    me_resp2 = await api_client.get(
        "/api/v1/participants/me",
        headers={"Authorization": f"Bearer {p_token2}"},
    )
    assert me_resp2.status_code == 401
