"""End-to-end verification of user acceptance criteria:
1. Sign in as an organizer
2. Create a test participant through the authorized organizer route
3. Securely receive participant credentials (participant_id, access_key)
4. Sign in as that participant through the participant authentication route
5. Upload an agent package for server-side validation
"""

from __future__ import annotations

import io
import tarfile
import time
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.main import app


@pytest.mark.asyncio
async def test_full_acceptance_criteria_e2e_workflow(api_client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch):
    """Verify organizer sign-in -> authorized participant creation -> participant sign-in -> agent upload & validation."""
    # Step 1: Sign in as an organizer
    org_email = f"lead_organizer_{uuid.uuid4().hex[:8]}@knockout.org"
    org_password = "SecureOrganizerPass2026!"

    monkeypatch.setenv("ORGANIZER_ALLOWED_EMAILS", org_email)
    new_settings = Settings(_env_file=None)
    app.dependency_overrides[get_settings] = lambda: new_settings

    reg_resp = await api_client.post(
        "/api/v1/admin/auth/register",
        json={"email": org_email, "name": "Chief Arbiter", "password": org_password},
    )
    assert reg_resp.status_code == 201

    login_resp = await api_client.post(
        "/api/v1/admin/auth/login-password",
        json={"email": org_email, "password": org_password},
    )
    assert login_resp.status_code == 200, login_resp.text
    org_data = login_resp.json()
    assert org_data["session_token"]
    assert org_data["csrf_token"]
    org_token = org_data["session_token"]
    csrf_token = org_data["csrf_token"]

    # Step 2: Create a test participant through organizer dashboard endpoint
    part_name = f"TestBot_{uuid.uuid4().hex[:8]}"
    part_team = "OpenEngine Labs"
    part_email = f"bot_{uuid.uuid4().hex[:6]}@openengine.test"
    idempotency_key = str(uuid.uuid4())

    create_resp = await api_client.post(
        "/api/v1/admin/participants/register",
        json={"name": part_name, "team_name": part_team, "email": part_email},
        headers={
            "Authorization": f"Bearer {org_token}",
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": idempotency_key,
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    creds = create_resp.json()

    # Step 3: Securely receive participant credentials
    assert creds["participant_id"].startswith("part_")
    assert creds["access_key"].startswith("ak_")
    assert creds["name"] == part_name
    assert creds["team_name"] == part_team
    assert creds["email"] == part_email.lower()
    assert creds["status"] == "ACTIVE"

    participant_id = creds["participant_id"]
    access_key = creds["access_key"]

    # Step 4: Sign in as that participant
    part_login = await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": participant_id, "access_key": access_key},
    )
    assert part_login.status_code == 200, part_login.text
    part_auth = part_login.json()
    assert part_auth["token_type"] == "bearer"
    assert part_auth["access_token"]
    part_bearer = part_auth["access_token"]

    # Verify participant workspace profile
    part_headers = {"Authorization": f"Bearer {part_bearer}"}
    me_resp = await api_client.get("/api/v1/participants/me", headers=part_headers)
    assert me_resp.status_code == 200, me_resp.text
    me = me_resp.json()
    assert me["participant_id"] == participant_id
    assert me["name"] == part_name

    # Step 5: Upload an agent for server-side validation
    # Create logical submission
    sub_resp = await api_client.post("/api/v1/submissions", headers=part_headers)
    assert sub_resp.status_code == 201, sub_resp.text
    submission_id = sub_resp.json()["submission_id"]

    agent_code = (
        '"""AI Knockout competitive chess agent."""\n'
        "def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:\n"
        '    return legal_moves[0] if legal_moves else ""\n'
    ).encode("utf-8")

    # Upload version as text/x-python single-file intelligence
    upload_resp = await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=agent_code,
        headers={**part_headers, "Content-Type": "text/x-python"},
    )
    assert upload_resp.status_code == 201, upload_resp.text
    upload_data = upload_resp.json()
    version_info = upload_data["version"]
    assert version_info["participant_id"] == participant_id
    assert version_info["version"] >= 1
    assert version_info["artifact_sha256"]
    version_num = version_info["version"]

    # Verify server-side validation output
    val_data = upload_data.get("validation", {})
    assert val_data.get("outcome") in ("PASSED", "ELIGIBLE", "QUEUED", "RUNNING")

    # Inspect validation endpoint directly
    val_resp = await api_client.get(
        f"/api/v1/submissions/{submission_id}/versions/{version_num}/validation",
        headers=part_headers,
    )
    assert val_resp.status_code == 200, val_resp.text
    polled_val = val_resp.json()
    assert polled_val["submission_id"] == str(submission_id)
    assert polled_val["version"] == version_num
    assert len(polled_val["attempts"]) > 0
    assert polled_val["attempts"][0]["outcome"] in ("PASSED", "ELIGIBLE")

    app.dependency_overrides.pop(get_settings, None)
