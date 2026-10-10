"""Multi-account isolation checks through the real participant API and test database."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.competition import ArtifactState, SubmissionVersion
from app.services.submissions import OrganizerApproval, approve_submission_for_eligibility
from tests.integration.test_development_matches import _configured_house_ai
from tests.unit.test_submission_validation import _valid_tar


async def _create_identity(client: httpx.AsyncClient, label: str) -> dict[str, str]:
    registration = await client.post(
        "/api/v1/participants/register",
        json={"name": f"{label}_{uuid.uuid4().hex}"},
    )
    assert registration.status_code == 201, registration.text
    credential = registration.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={"participant_id": credential["participant_id"],
              "access_key": credential["access_key"]},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {
        "participant_id": credential["participant_id"],
        "access_key": credential["access_key"],
        "token": token,
        "authorization": f"Bearer {token}",
    }


@pytest.mark.integration
async def test_five_independent_participants_isolate_accounts_submissions_and_matches(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    users = {
        label: await _create_identity(api_client, label)
        for label in ("P001", "P002", "P003", "P004", "P005")
    }
    assert len({user["participant_id"] for user in users.values()}) == 5
    assert len({user["token"] for user in users.values()}) == 5

    # Each actual credential pair maps to its own server-authoritative /me identity.
    for user in users.values():
        response = await api_client.get(
            "/api/v1/participants/me", headers={"Authorization": user["authorization"]}
        )
        assert response.status_code == 200
        assert response.json()["participant_id"] == user["participant_id"]
        assert "access_key" not in response.json()

    p1, p2, p3, p4, p5 = (users[f"P00{i}"] for i in range(1, 6))
    assert (await api_client.get(
        f"/api/v1/participants/me?participant_id={p2['participant_id']}",
        headers={"Authorization": p1["authorization"]},
    )).json()["participant_id"] == p1["participant_id"]
    assert (await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": p1["participant_id"], "access_key": p2["access_key"]},
    )).status_code == 401
    assert (await api_client.get(
        "/api/v1/participants/me", headers={"Authorization": "Bearer not-a-token"}
    )).status_code == 401
    assert (await api_client.get(
        "/api/v1/participants/me", headers={"Authorization": "Basic abc"}
    )).status_code == 401

    # Authentication revocation and account suspension both invalidate old sessions.
    logout = await api_client.post(
        "/api/v1/auth/logout", headers={"Authorization": p5["authorization"]}
    )
    assert logout.status_code == 200
    assert (await api_client.get(
        "/api/v1/participants/me", headers={"Authorization": p5["authorization"]}
    )).status_code == 401

    # Submission creation derives ownership from the bearer identity, ignoring injected IDs.
    p1_headers = {"Authorization": p1["authorization"]}
    p2_headers = {"Authorization": p2["authorization"]}
    p3_headers = {"Authorization": p3["authorization"]}
    p4_headers = {"Authorization": p4["authorization"]}
    p1_submission = await api_client.post(
        "/api/v1/submissions", headers=p1_headers,
        json={"participant_id": p2["participant_id"]},
    )
    p2_submission = await api_client.post("/api/v1/submissions", headers=p2_headers)
    p4_submission = await api_client.post("/api/v1/submissions", headers=p4_headers)
    assert (
        p1_submission.status_code == p2_submission.status_code == p4_submission.status_code == 201
    )
    p1_sid, p2_sid, p4_sid = (
        p1_submission.json()["submission_id"],
        p2_submission.json()["submission_id"],
        p4_submission.json()["submission_id"],
    )
    assert p1_submission.json()["participant_id"] == p1["participant_id"]

    uploaded = await api_client.post(
        f"/api/v1/submissions/{p1_sid}/versions", content=_valid_tar(),
        headers={**p1_headers, "Content-Type": "application/x-tar"},
    )
    assert uploaded.status_code == 201, uploaded.text
    version = uploaded.json()["version"]
    version_path = f"/api/v1/submissions/{p1_sid}/versions/1"
    validation_path = f"{version_path}/validation"

    # Every direct cross-owner endpoint is denied, including nested version/validation routes.
    for attacker_headers in (p2_headers, p3_headers):
        for path in (
            f"/api/v1/submissions/{p1_sid}",
            f"/api/v1/submissions/{p1_sid}/versions",
            version_path,
            validation_path,
        ):
            response = await api_client.get(path, headers=attacker_headers)
            assert response.status_code == 404, (path, response.status_code, response.text)
    assert (await api_client.post(
        f"/api/v1/submissions/{p1_sid}/versions", content=_valid_tar(),
        headers={**p2_headers, "Content-Type": "application/x-tar"},
    )).status_code == 404
    assert (await api_client.get(
        f"/api/v1/submissions/{p2_sid}", headers=p1_headers
    )).status_code == 404

    # Same digest replay is deterministic; malformed/oversized uploads are client errors.
    replay = await api_client.post(
        f"/api/v1/submissions/{p1_sid}/versions", content=_valid_tar(),
        headers={**p1_headers, "Content-Type": "application/x-tar"},
    )
    assert replay.status_code == 200
    assert replay.json()["version"]["duplicate"] is True
    malformed = await api_client.post(
        f"/api/v1/submissions/{p4_sid}/versions", content=b"not a tar archive",
        headers={**p4_headers, "Content-Type": "application/x-tar"},
    )
    assert malformed.status_code == 422, malformed.text
    settings = get_settings()
    original_upload_limit = settings.submission_max_archive_bytes
    monkeypatch.setattr(settings, "submission_max_archive_bytes", 64)
    oversized = await api_client.post(
        f"/api/v1/submissions/{p4_sid}/versions", content=b"x" * 65,
        headers={**p4_headers, "Content-Type": "application/x-tar"},
    )
    assert oversized.status_code == 413, oversized.text
    monkeypatch.setattr(settings, "submission_max_archive_bytes", original_upload_limit)

    # Only this isolated test DB receives organizer approval and a development match.
    version_row = await db_session.get(SubmissionVersion, uuid.UUID(version["id"]))
    assert version_row.state == ArtifactState.APPROVED
    await approve_submission_for_eligibility(
        db_session, submission_version_id=version_row.id,
        approval=OrganizerApproval(
            actor_id="integration-organizer", reason_code="MULTI_USER_TEST",
            approval_reference=f"test-{uuid.uuid4().hex[:12]}",
        ),
    )
    await _configured_house_ai(db_session)
    idempotency_key = f"five-users-{uuid.uuid4().hex}"
    match_response = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": p1_sid, "version": 1},
        headers={**p1_headers, "Idempotency-Key": idempotency_key},
    )
    assert match_response.status_code == 201, match_response.text
    match_id = match_response.json()["match_id"]
    assert match_response.json()["state"] == "QUEUED"
    assert "execution_id" not in match_response.json()
    assert "worker_id" not in match_response.json()
    assert (await api_client.get(
        f"/api/v1/participants/me/development-matches/{match_id}", headers=p2_headers
    )).status_code == 404
    assert (await api_client.get(
        f"/api/v1/participants/me/development-matches/{match_id}", headers=p3_headers
    )).status_code == 404
    replay_match = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": p1_sid, "version": 1},
        headers={**p1_headers, "Idempotency-Key": idempotency_key},
    )
    assert replay_match.status_code == 200
    assert replay_match.json()["match_id"] == match_id
    assert replay_match.json()["idempotent_replay"] is True
    second_version = await api_client.post(
        f"/api/v1/submissions/{p1_sid}/versions", content=_valid_tar(source=b"# second version\n"),
        headers={**p1_headers, "Content-Type": "application/x-tar"},
    )
    assert second_version.status_code == 201, second_version.text
    changed_payload_replay = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": p1_sid, "version": 2},
        headers={**p1_headers, "Idempotency-Key": idempotency_key},
    )
    assert changed_payload_replay.status_code == 409

    # A suspended identity cannot reuse its already-issued session or log in again.
    from app.models.participant import Participant, ParticipantStatus

    suspended = await db_session.scalar(
        select(Participant).where(Participant.participant_id == p3["participant_id"])
    )
    suspended.status = ParticipantStatus.SUSPENDED
    await db_session.commit()
    assert (await api_client.get(
        "/api/v1/participants/me", headers=p3_headers
    )).status_code == 401
    assert (await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": p3["participant_id"], "access_key": p3["access_key"]},
    )).status_code == 403

    # Cross-user simultaneous creation uses five independent bearer identities.
    concurrent = await asyncio.gather(*[
        api_client.post("/api/v1/submissions", headers={"Authorization": user["authorization"]})
        for user in (p1, p2, p4)
    ])
    assert all(response.status_code == 201 for response in concurrent)
    assert len({response.json()["submission_id"] for response in concurrent}) == 3
    assert [response.json()["participant_id"] for response in concurrent] == [
        p1["participant_id"], p2["participant_id"], p4["participant_id"]
    ]


@pytest.mark.integration
async def test_five_active_participants_create_submissions_concurrently(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
):
    users = await asyncio.gather(*[
        _create_identity(api_client, f"Concurrent{i:03}") for i in range(1, 6)
    ])
    responses = await asyncio.gather(*[
        api_client.post(
            "/api/v1/submissions",
            headers={"Authorization": user["authorization"]},
        )
        for user in users
    ])
    assert all(response.status_code == 201 for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert len({response.json()["submission_id"] for response in responses}) == 5
    assert [response.json()["participant_id"] for response in responses] == [
        user["participant_id"] for user in users
    ]

    # Each owner creates a distinct version in the same overlapping request window.
    uploads = await asyncio.gather(*[
        api_client.post(
            f"/api/v1/submissions/{response.json()['submission_id']}/versions",
            content=_valid_tar(source=f"# {index}\n".encode()),
            headers={"Authorization": users[index]["authorization"],
                     "Content-Type": "application/x-tar"},
        )
        for index, response in enumerate(responses)
    ])
    assert all(response.status_code == 201 for response in uploads), [
        (response.status_code, response.text) for response in uploads
    ]

    # Development-match requests also run concurrently through the participant API.
    versions = [upload.json()["version"] for upload in uploads]
    for version in versions:
        await approve_submission_for_eligibility(
            db_session,
            submission_version_id=uuid.UUID(version["id"]),
            approval=OrganizerApproval(
                actor_id="integration-organizer", reason_code="CONCURRENT_TEST",
                approval_reference=f"concurrent-{uuid.uuid4().hex[:10]}",
            ),
        )
    await _configured_house_ai(db_session)
    match_responses = await asyncio.gather(*[
        api_client.post(
            "/api/v1/participants/me/development-matches",
            json={"submission_id": response.json()["submission_id"], "version": 1},
            headers={"Authorization": user["authorization"],
                     "Idempotency-Key": f"concurrent-{uuid.uuid4().hex}"},
        )
        for user, response in zip(users, responses, strict=True)
    ])
    assert all(response.status_code == 201 for response in match_responses), [
        (response.status_code, response.text) for response in match_responses
    ]
    assert len({response.json()["match_id"] for response in match_responses}) == 5
    assert all(response.json()["state"] == "QUEUED" for response in match_responses)
