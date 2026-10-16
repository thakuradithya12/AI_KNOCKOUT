"""Integration tests for organizer registration, password login, and admin approval workflows."""

from __future__ import annotations

import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organizer import OrganizerAccount, OrganizerSession
from app.services.auth import AuthService


@pytest.mark.asyncio
async def test_organizer_public_registration_creates_pending_account(api_client, db_session: AsyncSession):
    # 1. Register a new untrusted organizer
    resp = await api_client.post(
        "/api/v1/admin/auth/register",
        json={
            "email": "new-arbiter@community.org",
            "name": "New Arbiter",
            "password": "SecurePassword123!",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "PENDING_APPROVAL"
    assert data["email"] == "new-arbiter@community.org"
    assert data["is_approved"] is False
    assert data["role"] == "PENDING_ORGANIZER"

    # Verify DB persistence and password hashing
    account = await db_session.scalar(
        select(OrganizerAccount).where(OrganizerAccount.email == "new-arbiter@community.org")
    )
    from app.core.security import verify_access_key
    assert verify_access_key("SecurePassword123!", account.password_hash)


@pytest.mark.asyncio
async def test_organizer_registration_duplicate_email_rejected(api_client, db_session: AsyncSession):
    resp1 = await api_client.post(
        "/api/v1/admin/auth/register",
        json={
            "email": "duplicate@test.org",
            "name": "First Attempt",
            "password": "SecurePassword123!",
        },
    )
    assert resp1.status_code == 201

    # Second registration with same email must fail with 409
    resp2 = await api_client.post(
        "/api/v1/admin/auth/register",
        json={
            "email": "duplicate@test.org",
            "name": "Second Attempt",
            "password": "AnotherPassword456!",
        },
    )
    assert resp2.status_code == 409, resp2.text
    assert resp2.json()["detail"] == "ORGANIZER_EMAIL_ALREADY_REGISTERED"


@pytest.mark.asyncio
async def test_organizer_registration_password_complexity_enforced(api_client):
    # Too short (<12 chars)
    r1 = await api_client.post(
        "/api/v1/admin/auth/register",
        json={"email": "weak1@test.org", "name": "Weak", "password": "Short1!"},
    )
    assert r1.status_code == 422

    # No digits
    r2 = await api_client.post(
        "/api/v1/admin/auth/register",
        json={"email": "weak2@test.org", "name": "Weak", "password": "NoDigitsInThisPassword!"},
    )
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_pending_organizer_cannot_login_or_perform_admin_actions(api_client, db_session: AsyncSession):
    await api_client.post(
        "/api/v1/admin/auth/register",
        json={
            "email": "pending@test.org",
            "name": "Pending User",
            "password": "ValidPassword999!",
        },
    )

    # Login attempt fails with 403 Forbidden because account is pending approval
    login_resp = await api_client.post(
        "/api/v1/admin/auth/login-password",
        json={"email": "pending@test.org", "password": "ValidPassword999!"},
    )
    assert login_resp.status_code == 403, login_resp.text
    assert "ORGANIZER_ACCOUNT_PENDING_APPROVAL" in login_resp.json()["detail"]

    # Directly requesting admin routes without session must be rejected with 401
    admin_resp = await api_client.get("/api/v1/admin/auth/accounts")
    assert admin_resp.status_code == 401


@pytest.mark.asyncio
async def test_bootstrap_allowlisted_organizer_auto_approved_and_can_login(api_client, db_session: AsyncSession, monkeypatch):
    monkeypatch.setenv("ORGANIZER_ALLOWED_EMAILS", "bootstrap-admin@event.org")
    from app.core.config import Settings, get_settings
    from app.main import app
    new_settings = Settings(_env_file=None)
    app.dependency_overrides[get_settings] = lambda: new_settings

    try:
        # Register allowlisted email
        reg_resp = await api_client.post(
            "/api/v1/admin/auth/register",
            json={
                "email": "bootstrap-admin@event.org",
                "name": "Bootstrap Admin",
                "password": "AdminPassword123!",
            },
        )
        assert reg_resp.status_code == 201
        assert reg_resp.json()["status"] == "APPROVED"
        assert reg_resp.json()["role"] == "ORGANIZER"

        # Login succeeds and issues session
        login_resp = await api_client.post(
            "/api/v1/admin/auth/login-password",
            json={"email": "bootstrap-admin@event.org", "password": "AdminPassword123!"},
        )
        assert login_resp.status_code == 200, login_resp.text
        login_data = login_resp.json()
        assert login_data["session_token"]
        assert login_data["csrf_token"]
        session_token = login_data["session_token"]
        csrf_token = login_data["csrf_token"]

        # Admin can inspect profile
        prof_resp = await api_client.get(
            "/api/v1/admin/auth/session",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        assert prof_resp.status_code == 200
        assert prof_resp.json()["organizer_email"] == "bootstrap-admin@event.org"

        # Admin can approve another pending organizer
        await api_client.post(
            "/api/v1/admin/auth/register",
            json={
                "email": "assistant@event.org",
                "name": "Assistant Arbiter",
                "password": "AssistantPassword123!",
            },
        )

        # Prior to approval, assistant cannot login
        asst_login = await api_client.post(
            "/api/v1/admin/auth/login-password",
            json={"email": "assistant@event.org", "password": "AssistantPassword123!"},
        )
        assert asst_login.status_code == 403

        # Bootstrap admin approves assistant
        approve_resp = await api_client.post(
            "/api/v1/admin/auth/approve",
            headers={
                "Authorization": f"Bearer {session_token}",
                "X-CSRF-Token": csrf_token,
            },
            json={"email": "assistant@event.org", "role": "ORGANIZER"},
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["is_approved"] is True

        # Now assistant can login!
        asst_login_ok = await api_client.post(
            "/api/v1/admin/auth/login-password",
            json={"email": "assistant@event.org", "password": "AssistantPassword123!"},
        )
        assert asst_login_ok.status_code == 200
        assert asst_login_ok.json()["organizer_email"] == "assistant@event.org"

    finally:
        app.dependency_overrides.pop(get_settings, None)
