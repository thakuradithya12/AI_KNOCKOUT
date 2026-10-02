"""Integration tests verifying audit event recording and absence of sensitive leaks."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.participant import AuthEvent, AuthEventType


@pytest.mark.integration
class TestAuditability:
    """Test security audit log creation and privacy."""

    async def test_audit_events_recorded_for_lifecycle(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Registration, login, and logout must each generate audit events."""
        name = f"Audit_User_{uuid.uuid4().hex[:8]}"

        # 1. Register
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert reg.status_code == 201
        pid = reg.json()["participant_id"]
        key = reg.json()["access_key"]

        # 2. Login
        login = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]

        # 3. Logout
        logout = await api_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert logout.status_code == 200

        # 4. Inspect audit events in database using db_session
        stmt = (
            select(AuthEvent)
            .where(AuthEvent.participant_id == pid)
            .order_by(AuthEvent.created_at.asc())
        )
        events = (await db_session.execute(stmt)).scalars().all()

        event_types = [e.event_type for e in events]
        assert AuthEventType.REGISTRATION_SUCCESS in event_types
        assert AuthEventType.LOGIN_SUCCESS in event_types
        assert AuthEventType.LOGOUT in event_types

        # Verify NO secrets leaked in details or any audit fields
        for event in events:
            if event.details:
                assert key not in event.details
                assert token not in event.details
                assert "ak_" not in event.details
                assert "aik_sess_" not in event.details
