"""Unit tests for submission quotas, organizer allowances, and counting semantics."""

from __future__ import annotations

import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import (
    ArtifactState,
    CompetitionEvent,
    Submission,
    SubmissionVersion,
)
from app.models.event import (
    BuildSession,
    ParticipantSubmissionAllowance,
    SubmissionAllowanceRequest,
)
from app.models.participant import Participant, ParticipantStatus
from app.services.submission_validation import DEFAULT_LIMITS, PackageLimits, PackageRejected, validate_submission_package
from app.services.submissions import (
    DEFAULT_SUBMISSION_LIMIT,
    SubmissionLimitExceeded,
    accept_upload,
    count_accepted_submissions,
    create_allowance_request,
    decide_allowance_request,
    get_or_create_allowance,
    get_participant_quota_summary,
    update_participant_allowance,
)
from tests.unit.test_submission_validation import _valid_tar


from datetime import datetime, timezone

async def _make_participant(db: AsyncSession, name: str = "TestPlayer") -> Participant:
    p = Participant(
        participant_id=f"part_{uuid.uuid4().hex[:12]}",
        name=f"{name}_{uuid.uuid4().hex[:8]}",
        access_key_hash="hash",
        status=ParticipantStatus.ACTIVE,
    )
    db.add(p)
    await db.flush()
    db.add(BuildSession(participant_id=p.participant_id, start_source="TEST", started_at=datetime.now(timezone.utc)))
    db.add(ParticipantSubmissionAllowance(participant_id=p.participant_id, submission_limit=10))
    await db.commit()
    await db.refresh(p)
    return p


@pytest.mark.asyncio
async def test_default_allowance_is_ten(db_session: AsyncSession):
    participant = await _make_participant(db_session)
    allowance = await get_or_create_allowance(db_session, participant.participant_id)
    assert allowance.submission_limit == 10
    assert allowance.participant_id == participant.participant_id

    summary = await get_participant_quota_summary(db_session, participant.participant_id)
    assert summary["submission_limit"] == 10
    assert summary["accepted_submissions_count"] == 0
    assert summary["remaining_submissions"] == 10
    assert summary["has_pending_request"] is False


@pytest.mark.asyncio
async def test_counting_semantics_only_counts_accepted_states(db_session: AsyncSession):
    participant = await _make_participant(db_session)
    sub = Submission(id=uuid.uuid4(), participant_id=participant.participant_id)
    db_session.add(sub)
    await db_session.flush()

    # Upload 1: Valid package -> APPROVED
    res1 = await accept_upload(
        db_session,
        participant_id=participant.participant_id,
        submission_id=sub.id,
        archive_bytes=_valid_tar(source=b"# code 1\n"),
        correlation_id="c-1",
        limits=DEFAULT_LIMITS,
    )
    assert res1.version.state == ArtifactState.APPROVED

    # Upload 2: Valid package, promoted to ELIGIBLE via organizer approval
    res2 = await accept_upload(
        db_session,
        participant_id=participant.participant_id,
        submission_id=sub.id,
        archive_bytes=_valid_tar(source=b"# code 2\n"),
        correlation_id="c-2",
        limits=DEFAULT_LIMITS,
    )
    from app.services.submissions import OrganizerApproval, approve_submission_for_eligibility
    await approve_submission_for_eligibility(
        db_session,
        submission_version_id=res2.version.id,
        approval=OrganizerApproval(
            actor_id="organizer-test",
            reason_code="TEST_ELIGIBILITY",
            approval_reference="ref-123",
        ),
    )

    # Upload 3: Valid package -> APPROVED
    res3 = await accept_upload(
        db_session,
        participant_id=participant.participant_id,
        submission_id=sub.id,
        archive_bytes=_valid_tar(source=b"# code 3\n"),
        correlation_id="c-3",
        limits=DEFAULT_LIMITS,
    )
    assert res3.version.state == ArtifactState.APPROVED

    # Upload 4: Broken/invalid tar -> REJECTED
    res4 = await accept_upload(
        db_session,
        participant_id=participant.participant_id,
        submission_id=sub.id,
        archive_bytes=b"broken-non-tar-content",
        correlation_id="c-4",
        limits=DEFAULT_LIMITS,
    )
    assert res4.version.state == ArtifactState.REJECTED

    count = await count_accepted_submissions(db_session, participant.participant_id)
    assert count == 3

    summary = await get_participant_quota_summary(db_session, participant.participant_id)
    assert summary["accepted_submissions_count"] == 3
    assert summary["remaining_submissions"] == 7


@pytest.mark.asyncio
async def test_upload_quota_enforcement_and_limit_rejection(db_session: AsyncSession):
    participant = await _make_participant(db_session)
    sub = Submission(id=uuid.uuid4(), participant_id=participant.participant_id)
    db_session.add(sub)
    await db_session.commit()

    # Uploads 1 to 10 succeed
    for i in range(1, 11):
        archive = _valid_tar(source=f"# engine version {i}\n".encode())
        result = await accept_upload(
            db_session,
            participant_id=participant.participant_id,
            submission_id=sub.id,
            archive_bytes=archive,
            correlation_id=f"test-corr-{i}",
            limits=DEFAULT_LIMITS,
        )
        assert result.version.version == i
        assert result.version.state == ArtifactState.APPROVED

    assert await count_accepted_submissions(db_session, participant.participant_id) == 10

    # Idempotent retry of existing version 10 succeeds even at limit
    archive_10 = _valid_tar(source=b"# engine version 10\n")
    retry_result = await accept_upload(
        db_session,
        participant_id=participant.participant_id,
        submission_id=sub.id,
        archive_bytes=archive_10,
        correlation_id="test-retry",
        limits=DEFAULT_LIMITS,
    )
    assert retry_result.duplicate is True
    assert retry_result.version.version == 10

    # Upload 11 (new package) is rejected with SubmissionLimitExceeded
    archive_11 = _valid_tar(source=b"# engine version 11\n")
    with pytest.raises(SubmissionLimitExceeded) as exc_info:
        await accept_upload(
            db_session,
            participant_id=participant.participant_id,
            submission_id=sub.id,
            archive_bytes=archive_11,
            correlation_id="test-corr-11",
            limits=DEFAULT_LIMITS,
        )
    assert exc_info.value.limit == 10
    assert exc_info.value.current == 10


@pytest.mark.asyncio
async def test_organizer_exception_allows_exceeding_default_quota(db_session: AsyncSession):
    participant = await _make_participant(db_session)
    sub = Submission(id=uuid.uuid4(), participant_id=participant.participant_id)
    db_session.add(sub)
    await db_session.commit()

    # Upload 10 packages
    for i in range(1, 11):
        archive = _valid_tar(source=f"# code {i}\n".encode())
        await accept_upload(
            db_session,
            participant_id=participant.participant_id,
            submission_id=sub.id,
            archive_bytes=archive,
            correlation_id=f"c-{i}",
            limits=DEFAULT_LIMITS,
        )

    # Organizer increases allowance to 12
    updated = await update_participant_allowance(
        db_session,
        participant_id=participant.participant_id,
        new_limit=12,
        actor_id="lead-organizer",
        reason="Granted additional attempts for bug fix",
    )
    assert updated.submission_limit == 12
    assert updated.granted_by == "lead-organizer"

    # Upload 11 now succeeds!
    archive_11 = _valid_tar(source=b"# code 11\n")
    res11 = await accept_upload(
        db_session,
        participant_id=participant.participant_id,
        submission_id=sub.id,
        archive_bytes=archive_11,
        correlation_id="c-11",
        limits=DEFAULT_LIMITS,
    )
    assert res11.version.version == 11
    assert res11.version.state == ArtifactState.APPROVED

    # Upload 12 succeeds
    archive_12 = _valid_tar(source=b"# code 12\n")
    res12 = await accept_upload(
        db_session,
        participant_id=participant.participant_id,
        submission_id=sub.id,
        archive_bytes=archive_12,
        correlation_id="c-12",
        limits=DEFAULT_LIMITS,
    )
    assert res12.version.version == 12

    # Upload 13 fails with limit 12
    archive_13 = _valid_tar(source=b"# code 13\n")
    with pytest.raises(SubmissionLimitExceeded) as exc_info:
        await accept_upload(
            db_session,
            participant_id=participant.participant_id,
            submission_id=sub.id,
            archive_bytes=archive_13,
            correlation_id="c-13",
            limits=DEFAULT_LIMITS,
        )
    assert exc_info.value.limit == 12
    assert exc_info.value.current == 12


@pytest.mark.asyncio
async def test_allowance_request_workflow(db_session: AsyncSession):
    participant = await _make_participant(db_session)

    # Cannot request lower or equal limit
    with pytest.raises(ValueError, match="REQUESTED_LIMIT_MUST_EXCEED_CURRENT_LIMIT"):
        await create_allowance_request(
            db_session,
            participant_id=participant.participant_id,
            requested_limit=10,
            reason="Want same limit",
        )

    # Valid request
    req = await create_allowance_request(
        db_session,
        participant_id=participant.participant_id,
        requested_limit=15,
        reason="Need testing attempts for new evaluation feature",
    )
    assert req.status == "PENDING"
    assert req.requested_limit == 15

    # Cannot create second pending request while one is pending
    with pytest.raises(ValueError, match="PENDING_REQUEST_ALREADY_EXISTS"):
        await create_allowance_request(
            db_session,
            participant_id=participant.participant_id,
            requested_limit=20,
            reason="Another request",
        )

    # Organizer approves request
    decided = await decide_allowance_request(
        db_session,
        request_id=req.id,
        decision="APPROVED",
        actor_id="judge-alice",
        notes="Approved for tournament preparation",
    )
    assert decided.status == "APPROVED"
    assert decided.reviewed_by == "judge-alice"

    # Allowance is now updated to 15
    allowance = await get_or_create_allowance(db_session, participant.participant_id)
    assert allowance.submission_limit == 15


@pytest.mark.asyncio
async def test_ten_mib_archive_size_enforcement():
    # 10 MiB is 10 * 1024 * 1024 bytes (10,485,760 bytes)
    assert DEFAULT_LIMITS.max_archive_bytes == 10 * 1024 * 1024
    assert DEFAULT_LIMITS.max_total_bytes == 10 * 1024 * 1024

    # Package larger than 10 MiB is rejected with ARCHIVE_SIZE_EXCEEDED
    oversized = bytes(10 * 1024 * 1024 + 1)
    with pytest.raises(PackageRejected) as exc_info:
        validate_submission_package(oversized, DEFAULT_LIMITS)
    assert exc_info.value.code == "PACKAGE_TOO_LARGE"
