"""End-to-end integration test verifying the canonical agent package lifecycle."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import ArtifactState
from app.services.submissions import OrganizerApproval, approve_submission_for_eligibility


# These archives live outside the repository; point the env vars at them to run this test.
CANONICAL_TAR_PATH = Path(
    os.environ.get("AIK_CANONICAL_TAR", "C:/Users/thaku/Downloads/aik-python-agent-canonical.tar")
)
REJECTED_TAR_PATH = Path(
    os.environ.get("AIK_REJECTED_TAR", "C:/Users/thaku/Downloads/aik-python-agent-dist.tar")
)


async def _register_and_login(client: httpx.AsyncClient, name_prefix: str = "Player") -> tuple[str, dict[str, str]]:
    name = f"{name_prefix}_{uuid.uuid4().hex[:8]}"
    reg = await client.post("/api/v1/participants/register", json={"name": name})
    assert reg.status_code == 201, reg.text
    part_id = reg.json()["participant_id"]
    access_key = reg.json()["access_key"]

    login = await client.post("/api/v1/auth/login", json={"participant_id": part_id, "access_key": access_key})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return part_id, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_canonical_agent_submission_lifecycle(api_client: httpx.AsyncClient, db_session: AsyncSession):
    if not CANONICAL_TAR_PATH.is_file():
        pytest.skip(f"canonical archive not available at {CANONICAL_TAR_PATH} (set AIK_CANONICAL_TAR)")
    # 1. Register & login participant A
    part_a_id, headers_a = await _register_and_login(api_client, "CanonicalParticipant")

    # 2. Check initial quota
    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers_a)
    assert quota_res.status_code == 200
    q_init = quota_res.json()
    assert q_init["accepted_submissions_count"] == 0
    assert q_init["remaining_submissions"] == 10

    # 3. Create logical submission
    sub_res = await api_client.post("/api/v1/submissions", headers=headers_a)
    assert sub_res.status_code == 201, sub_res.text
    submission_id = sub_res.json()["submission_id"]

    # 4. Upload canonical archive
    assert CANONICAL_TAR_PATH.is_file(), f"Canonical archive missing at {CANONICAL_TAR_PATH}"
    canon_bytes = CANONICAL_TAR_PATH.read_bytes()
    expected_sha256 = hashlib.sha256(canon_bytes).hexdigest()

    up_res = await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=canon_bytes,
        headers={**headers_a, "Content-Type": "application/x-tar"},
    )
    assert up_res.status_code == 201, up_res.text
    up_data = up_res.json()
    assert up_data["version"]["version"] == 1
    assert up_data["version"]["state"] == "APPROVED"
    assert up_data["version"]["artifact_sha256"] == expected_sha256
    assert up_data["validation"]["outcome"] == "PASSED"
    assert up_data["validation"]["report"]["code"] == "STATIC_VALIDATION_PASSED"
    v1_id = uuid.UUID(up_data["version"]["id"])

    # 5. Duplicate upload of canonical archive -> idempotent HTTP 200
    dup_res = await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=canon_bytes,
        headers={**headers_a, "Content-Type": "application/x-tar"},
    )
    assert dup_res.status_code == 200, dup_res.text
    dup_data = dup_res.json()
    assert dup_data["version"]["duplicate"] is True
    assert dup_data["version"]["id"] == str(v1_id)

    # 6. Upload rejected original archive -> HTTP 422 ARCHIVE_FORMAT_UNSUPPORTED
    assert REJECTED_TAR_PATH.is_file(), f"Rejected archive missing at {REJECTED_TAR_PATH}"
    rej_bytes = REJECTED_TAR_PATH.read_bytes()
    rej_res = await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=rej_bytes,
        headers={**headers_a, "Content-Type": "application/x-tar"},
    )
    assert rej_res.status_code == 422, rej_res.text
    rej_data = rej_res.json()
    assert rej_data["error"] == "ARCHIVE_FORMAT_UNSUPPORTED"
    assert rej_data["version"]["version"] == 2
    assert rej_data["version"]["state"] == "REJECTED"

    # 7. Check submission history (both versions preserved)
    history_res = await api_client.get(f"/api/v1/submissions/{submission_id}/versions", headers=headers_a)
    assert history_res.status_code == 200, history_res.text
    items = history_res.json()["items"]
    assert len(items) == 2
    # items sorted or ordered
    v_map = {item["version"]: item for item in items}
    assert v_map[1]["state"] == "APPROVED"
    assert v_map[1]["artifact_sha256"] == expected_sha256
    assert v_map[2]["state"] == "REJECTED"

    # 8. Check quota enforcement (only approved counts against quota)
    quota_after = (await api_client.get("/api/v1/submissions/quota", headers=headers_a)).json()
    assert quota_after["accepted_submissions_count"] == 1
    assert quota_after["remaining_submissions"] == 9

    # 9. Check validation details endpoint
    val_res = await api_client.get(f"/api/v1/submissions/{submission_id}/versions/1/validation", headers=headers_a)
    assert val_res.status_code == 200, val_res.text
    val_data = val_res.json()
    assert val_data["state"] == "APPROVED"
    assert val_data["attempts"][0]["outcome"] == "PASSED"
    assert val_data["attempts"][0]["report"]["code"] == "STATIC_VALIDATION_PASSED"

    # 10. Organizer approval for official tournament eligibility
    approved_ver = await approve_submission_for_eligibility(
        db_session,
        submission_version_id=v1_id,
        approval=OrganizerApproval(
            actor_id="organizer-main",
            reason_code="MANUAL_REVIEW_PASSED",
            approval_reference="ticket-canonical-001",
        ),
    )
    assert approved_ver.state == ArtifactState.ELIGIBLE
    assert approved_ver.artifact_sha256 == expected_sha256

    # 11. Isolation: Participant B cannot access Participant A's submission
    _, headers_b = await _register_and_login(api_client, "OtherParticipant")
    assert (await api_client.get(f"/api/v1/submissions/{submission_id}", headers=headers_b)).status_code == 404
    assert (await api_client.get(f"/api/v1/submissions/{submission_id}/versions", headers=headers_b)).status_code == 404
    assert (await api_client.get(f"/api/v1/submissions/{submission_id}/versions/1/validation", headers=headers_b)).status_code == 404
    assert (await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=canon_bytes,
        headers={**headers_b, "Content-Type": "application/x-tar"},
    )).status_code == 404
