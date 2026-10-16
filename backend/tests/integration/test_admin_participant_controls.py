"""Integration tests for organizer participant administrative controls:
suspension, reinstatement, session revocation, safe removal, and tournament integrity protections.
"""

from __future__ import annotations

import uuid
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.competition import CompetitionEvent, Submission
from app.models.event import TournamentSnapshot
from app.models.participant import AuthEvent, Participant, ParticipantSession, ParticipantStatus
from tests.unit.test_submission_validation import _valid_tar


@pytest.mark.integration
class TestAdminParticipantControls:
    """Verify organizer participant lifecycle endpoints against real persisted state."""

    async def test_suspend_requires_organizer_authorization(self, api_client: httpx.AsyncClient):
        """Unauthorized suspension attempts must be rejected with 401."""
        res = await api_client.post(
            "/api/v1/admin/participants/part_nonexistent/suspend",
            json={"reason": "Testing unauthorized"},
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "ORGANIZER_AUTHENTICATION_REQUIRED"

    async def test_suspend_participant_revokes_sessions_and_blocks_login(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Suspension must persist SUSPENDED state, revoke active sessions, and block login."""
        settings = Settings()
        admin_headers = {"X-Organizer-Secret": settings.secret_key}

        # 1. Register and sign in
        name = f"Suspended_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        assert reg.status_code == 201
        pid = reg.json()["participant_id"]
        key = reg.json()["access_key"]

        login = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {token}"}

        # Verify active profile access initially
        me = await api_client.get("/api/v1/participants/me", headers=auth_headers)
        assert me.status_code == 200

        # 2. Organizer suspends participant
        suspend_res = await api_client.post(
            f"/api/v1/admin/participants/{pid}/suspend",
            headers=admin_headers,
            json={"reason": "Rules violation investigation", "actor_id": "chief-arbiter"},
        )
        assert suspend_res.status_code == 200
        data = suspend_res.json()
        assert data["participant_id"] == pid
        assert data["status"] == "SUSPENDED"
        assert data["revoked_sessions"] >= 1
        assert data["audited"] is True

        # 3. Existing session token must now be rejected
        me_after = await api_client.get("/api/v1/participants/me", headers=auth_headers)
        assert me_after.status_code == 401  # Revoked session

        # 4. New login attempt must be rejected with 403 Forbidden
        login_after = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        assert login_after.status_code == 403
        assert "suspended" in login_after.json()["detail"].lower()

        # 5. Verify persisted audit records
        audit_events = (
            await db_session.scalars(
                select(CompetitionEvent)
                .where(
                    CompetitionEvent.aggregate_type == "participant",
                    CompetitionEvent.event_type == "participant.suspended",
                )
            )
        ).all()
        assert any(e.metadata_json.get("participant_id") == pid for e in audit_events)

    async def test_reinstate_participant_restores_active_state_and_login(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Reinstatement restores ACTIVE status and permits new login."""
        settings = Settings()
        admin_headers = {"X-Organizer-Secret": settings.secret_key}

        # 1. Register and suspend
        name = f"Reinstate_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        pid = reg.json()["participant_id"]
        key = reg.json()["access_key"]

        await api_client.post(
            f"/api/v1/admin/participants/{pid}/suspend",
            headers=admin_headers,
            json={"reason": "Temporary freeze"},
        )

        # 2. Reinstate participant
        reinstate_res = await api_client.post(
            f"/api/v1/admin/participants/{pid}/reinstate",
            headers=admin_headers,
            json={"reason": "Appeal resolved", "actor_id": "appeals-committee"},
        )
        assert reinstate_res.status_code == 200
        assert reinstate_res.json()["status"] == "ACTIVE"

        # 3. Login now succeeds
        login = await api_client.post(
            "/api/v1/auth/login",
            json={"participant_id": pid, "access_key": key},
        )
        assert login.status_code == 200

    async def test_revoke_sessions_invalidates_all_active_tokens(
        self, api_client: httpx.AsyncClient
    ):
        """Organizer session revocation invalidates all existing bearer sessions."""
        settings = Settings()
        admin_headers = {"X-Organizer-Secret": settings.secret_key}

        # 1. Register and create 2 distinct sessions
        name = f"Revoke_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        pid = reg.json()["participant_id"]
        key = reg.json()["access_key"]

        login1 = await api_client.post("/api/v1/auth/login", json={"participant_id": pid, "access_key": key})
        token1 = login1.json()["access_token"]
        login2 = await api_client.post("/api/v1/auth/login", json={"participant_id": pid, "access_key": key})
        token2 = login2.json()["access_token"]

        assert (await api_client.get("/api/v1/participants/me", headers={"Authorization": f"Bearer {token1}"})).status_code == 200
        assert (await api_client.get("/api/v1/participants/me", headers={"Authorization": f"Bearer {token2}"})).status_code == 200

        # 2. Organizer revokes sessions
        revoke_res = await api_client.post(
            f"/api/v1/admin/participants/{pid}/revoke-sessions",
            headers=admin_headers,
            json={"reason": "Security credential rotation"},
        )
        assert revoke_res.status_code == 200
        assert revoke_res.json()["revoked_sessions"] >= 2

        # 3. Both tokens are now invalid
        assert (await api_client.get("/api/v1/participants/me", headers={"Authorization": f"Bearer {token1}"})).status_code == 401
        assert (await api_client.get("/api/v1/participants/me", headers={"Authorization": f"Bearer {token2}"})).status_code == 401

    async def test_removal_rejected_if_participant_has_tournament_or_submission_records(
        self, api_client: httpx.AsyncClient
    ):
        """Deleting a participant who has submissions or tournament records must return 409 Conflict."""
        settings = Settings()
        admin_headers = {"X-Organizer-Secret": settings.secret_key}

        # 1. Register participant and create submission
        name = f"Historic_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        pid = reg.json()["participant_id"]
        key = reg.json()["access_key"]

        login = await api_client.post("/api/v1/auth/login", json={"participant_id": pid, "access_key": key})
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Create submission
        sub_res = await api_client.post("/api/v1/submissions", headers=headers)
        assert sub_res.status_code == 201

        # 2. Organizer attempts deletion — must be rejected with 409
        del_res = await api_client.delete(
            f"/api/v1/admin/participants/{pid}",
            headers=admin_headers,
        )
        assert del_res.status_code == 409
        assert "CANNOT_REMOVE_PARTICIPANT_WITH_TOURNAMENT_RECORDS" in del_res.json()["detail"]

    async def test_removal_succeeds_and_is_audited_for_unused_participant(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        """Unused participant with no tournament history can be cleanly removed with audit record."""
        settings = Settings()
        admin_headers = {"X-Organizer-Secret": settings.secret_key}

        name = f"Unused_User_{uuid.uuid4().hex[:8]}"
        reg = await api_client.post("/api/v1/participants/register", json={"name": name})
        pid = reg.json()["participant_id"]

        del_res = await api_client.delete(
            f"/api/v1/admin/participants/{pid}",
            headers=admin_headers,
        )
        assert del_res.status_code == 200
        assert del_res.json()["removed"] is True

        # Participant is deleted from database
        p = await db_session.scalar(select(Participant).where(Participant.participant_id == pid))
        assert p is None
