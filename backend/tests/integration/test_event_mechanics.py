"""Isolated event lifecycle tests: SQLite + ASGI only; artifacts remain inert."""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.event import (
    DevelopmentMatchAssociation,
    HouseAIIdentity,
    MatchPurpose,
)
from app.models.competition import ArtifactState, SubmissionVersion
from app.models.event import BuildSession, OfficialSubmissionSelection
from app.services.event import freeze_official_submissions
from app.services.submissions import (
    OrganizerApproval,
    approve_submission_for_eligibility,
)
from tests.unit.test_submission_validation import _valid_tar


async def _register_and_login(
    client: httpx.AsyncClient, *, label: str
) -> tuple[str, dict[str, str], dict]:
    response = await client.post(
        "/api/v1/participants/register",
        json={
            "name": f"Event_{label}_{uuid.uuid4().hex}",
            "build_started_at": "2001-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    receipt = response.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={"participant_id": receipt["participant_id"], "access_key": receipt["access_key"]},
    )
    assert login.status_code == 200, login.text
    return (
        receipt["participant_id"],
        {"Authorization": f"Bearer {login.json()['access_token']}"},
        receipt,
    )


@pytest.mark.integration
async def test_registration_starts_one_immutable_server_build_session(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    participant_id, headers, receipt = await _register_and_login(api_client, label="clock")
    rows = await db_session.scalar(
        select(func.count()).select_from(BuildSession).where(
            BuildSession.participant_id == participant_id
        )
    )
    session = await db_session.get(BuildSession, participant_id)
    assert rows == 1
    assert session.start_source == "REGISTRATION"
    receipt_created = datetime.fromisoformat(receipt["created_at"].replace("Z", "+00:00"))
    session_started = session.started_at.replace(tzinfo=timezone.utc)
    assert abs((session_started - receipt_created).total_seconds()) < 0.01
    assert session_started.year != 2001

    event_status = await api_client.get(
        "/api/v1/participants/me/event-status", headers=headers
    )
    assert event_status.status_code == 200
    assert event_status.json()["build_elapsed_seconds"] >= 0
    session.started_at = session.started_at.replace(year=session.started_at.year - 1)
    with pytest.raises(ValueError, match="APPEND_ONLY_RECORD_IMMUTABLE"):
        await db_session.flush()


@pytest.mark.integration
async def test_server_receipt_duration_official_freeze_and_version_history(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    participant_id, headers, receipt = await _register_and_login(api_client, label="submission")
    created = await api_client.post("/api/v1/submissions", headers=headers)
    assert created.status_code == 201
    submission_id = created.json()["submission_id"]

    versions = []
    for source in (b"# inert version one\n", b"# inert version two\n"):
        upload = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions",
            content=_valid_tar(source=source),
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        assert upload.status_code == 201, upload.text
        payload = upload.json()["version"]
        assert payload["submitted_at"] == payload["created_at"]
        assert payload["build_duration_seconds"] >= 0
        versions.append(payload)
        await approve_submission_for_eligibility(
            db_session,
            submission_version_id=uuid.UUID(payload["id"]),
            approval=OrganizerApproval(
                actor_id="event-test-organizer",
                reason_code="TEST_APPROVED",
                approval_reference=f"event-test-{payload['version']}",
            ),
        )

    event_status = await api_client.get(
        "/api/v1/participants/me/event-status", headers=headers
    )
    assert event_status.status_code == 200, event_status.text
    state = event_status.json()
    assert state["build_started_at"] == receipt["created_at"]
    assert state["latest_submission"]["version"] == 2
    assert state["latest_submission"]["state"] == ArtifactState.ELIGIBLE.value
    assert state["latest_submission"]["validation_outcome"] == "PASSED"
    server_received = datetime.fromisoformat(
        state["latest_submission"]["submitted_at"].replace("Z", "+00:00")
    )
    server_started = datetime.fromisoformat(state["build_started_at"].replace("Z", "+00:00"))
    assert state["latest_submission"]["build_duration_seconds"] == max(
        0, int((server_received - server_started).total_seconds())
    )
    assert state["server_now"]

    first_selection = await api_client.put(
        "/api/v1/participants/me/official-submission",
        headers=headers,
        json={"submission_id": submission_id, "version": 1},
    )
    assert first_selection.status_code == 200, first_selection.text
    await freeze_official_submissions(
        db_session, actor_id="event-test-organizer", reason_code="SCHEDULED_FREEZE"
    )
    changed_selection = await api_client.put(
        "/api/v1/participants/me/official-submission",
        headers=headers,
        json={"submission_id": submission_id, "version": 2},
    )
    assert changed_selection.status_code == 409

    persisted = (
        await db_session.execute(
            select(OfficialSubmissionSelection).where(
                OfficialSubmissionSelection.participant_id == participant_id
            )
        )
    ).scalar_one()
    assert persisted.submission_version_id == uuid.UUID(versions[0]["id"])
    history = (
        await db_session.execute(
            select(SubmissionVersion)
            .where(SubmissionVersion.submission_id == uuid.UUID(submission_id))
            .order_by(SubmissionVersion.version)
        )
    ).scalars().all()
    assert [row.version for row in history] == [1, 2]
    assert history[0].artifact_sha256 == versions[0]["artifact_sha256"]
    assert history[1].artifact_sha256 == versions[1]["artifact_sha256"]
    history[0].build_duration_seconds += 1
    with pytest.raises(ValueError, match="ARTIFACT_IDENTITY_IMMUTABLE"):
        await db_session.flush()


def test_house_ai_version_and_development_match_contract_are_immutable_and_separate():
    house = HouseAIIdentity(
        house_ai_id="organizer-house-ai",
        version=3,
        artifact_digest="a" * 64,
        protocol_version="aik-jsonl/1",
        runtime_profile="runtime-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        policy_version="house-policy-v1",
        runtime_identity="runtime-image-sha256:fixture",
    )
    binding = DevelopmentMatchAssociation(
        development_match_id=uuid.uuid4(),
        participant_id="part_fixture",
        submission_version_id=uuid.uuid4(),
        participant_artifact_digest="b" * 64,
        house_ai=house,
    )
    assert binding.purpose is MatchPurpose.DEVELOPMENT
    with pytest.raises(FrozenInstanceError):
        house.version = 4
    with pytest.raises(FrozenInstanceError):
        binding.purpose = MatchPurpose.OFFICIAL
    with pytest.raises(ValueError, match="HOUSE_AI_DIGEST_INVALID"):
        HouseAIIdentity(
            house_ai_id="house",
            version=1,
            artifact_digest="not-a-digest",
            protocol_version="aik-jsonl/1",
            runtime_profile="runtime-v1",
            rules_version="rules-v1",
            policy_version="policy-v1",
            runtime_identity="runtime-v1",
        )
