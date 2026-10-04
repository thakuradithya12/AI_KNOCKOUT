"""Server-authoritative event clock, official selection, and freeze operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import (
    ArtifactState,
    CompetitionEvent,
    Submission,
    SubmissionVersion,
    ValidationAttempt,
)
from app.models.event import BuildSession, EventControl, OfficialSubmissionSelection

_EVENT_CONTROL_AGGREGATE = uuid.UUID("ed0afc01-2c18-4dc2-bac2-f3ddc9200010")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _control(db: AsyncSession, *, lock: bool = False) -> EventControl:
    statement = select(EventControl).where(EventControl.id == 1)
    if lock:
        statement = statement.with_for_update()
    control = await db.scalar(statement)
    if control is None:
        control = EventControl(id=1)
        db.add(control)
        await db.flush()
    return control


async def get_participant_event_status(
    db: AsyncSession, participant_id: str
) -> dict:
    build = await db.get(BuildSession, participant_id)
    if build is None:
        raise LookupError("BUILD_SESSION_NOT_FOUND")
    server_now = _now()
    build_started_at = _utc(build.started_at)
    elapsed = max(0, int((server_now - build_started_at).total_seconds()))

    latest = (
        await db.execute(
            select(SubmissionVersion)
            .join(Submission, Submission.id == SubmissionVersion.submission_id)
            .where(Submission.participant_id == participant_id)
            .order_by(SubmissionVersion.created_at.desc(), SubmissionVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    validation = None
    if latest is not None:
        validation = await db.scalar(
            select(ValidationAttempt)
            .where(ValidationAttempt.submission_version_id == latest.id)
            .order_by(ValidationAttempt.completed_at.desc())
            .limit(1)
        )

    selection = await db.scalar(
        select(OfficialSubmissionSelection)
        .where(OfficialSubmissionSelection.participant_id == participant_id)
    )
    control = await _control(db)
    return {
        "server_now": server_now,
        "build_started_at": build_started_at,
        "build_elapsed_seconds": elapsed,
        "latest_submission": (
            {
                "submission_id": str(latest.submission_id),
                "version": latest.version,
                "submitted_at": _utc(latest.created_at),
                "build_duration_seconds": latest.build_duration_seconds,
                "state": latest.state.value,
                "validation_outcome": validation.outcome.value if validation else None,
            }
            if latest
            else None
        ),
        "official_submission": (
            {
                "version": (
                    await db.get(SubmissionVersion, selection.submission_version_id)
                ).version,
                "selected_at": _utc(selection.selected_at),
            }
            if selection
            else None
        ),
        "official_frozen_at": (
            _utc(control.official_freeze_at) if control.official_freeze_at else None
        ),
    }


async def select_official_submission(
    db: AsyncSession,
    *,
    participant_id: str,
    submission_id: uuid.UUID,
    version_number: int,
) -> OfficialSubmissionSelection:
    control = await _control(db, lock=True)
    if control.official_freeze_at is not None:
        raise ValueError("OFFICIAL_SUBMISSION_FROZEN")
    version = await db.scalar(
        select(SubmissionVersion)
        .join(Submission, Submission.id == SubmissionVersion.submission_id)
        .where(
            Submission.participant_id == participant_id,
            SubmissionVersion.participant_id == participant_id,
            SubmissionVersion.submission_id == submission_id,
            SubmissionVersion.version == version_number,
        )
        .with_for_update()
    )
    if version is None:
        raise LookupError("SUBMISSION_VERSION_NOT_FOUND")
    if version.state != ArtifactState.ELIGIBLE:
        raise ValueError("OFFICIAL_SUBMISSION_MUST_BE_ELIGIBLE")

    selection = await db.scalar(
        select(OfficialSubmissionSelection)
        .where(OfficialSubmissionSelection.participant_id == participant_id)
        .with_for_update()
    )
    previous_version_id = None
    if selection is not None and selection.submission_version_id == version.id:
        return selection

    selected_at = _now()
    if selection is None:
        selection = OfficialSubmissionSelection(
            participant_id=participant_id,
            submission_version_id=version.id,
            selected_at=selected_at,
        )
        db.add(selection)
    else:
        previous_version_id = str(selection.submission_version_id)
        selection.submission_version_id = version.id
        selection.selected_at = selected_at
    sequence = await db.scalar(
        select(func.max(CompetitionEvent.aggregate_sequence)).where(
            CompetitionEvent.aggregate_type == "official_submission",
            CompetitionEvent.aggregate_id == version.id,
        )
    )
    db.add(
        CompetitionEvent(
            event_type="official_submission.selected",
            actor_kind="participant",
            actor_id=participant_id,
            severity="info",
            aggregate_type="official_submission",
            aggregate_id=version.id,
            aggregate_sequence=(sequence or 0) + 1,
            schema_version=1,
            metadata_json={
                "participant_id": participant_id,
                "submission_id": str(submission_id),
                "version": version_number,
                "artifact_sha256": version.artifact_sha256,
                "previous_version_id": previous_version_id,
            },
            recorded_at=selected_at,
        )
    )
    await db.commit()
    await db.refresh(selection)
    return selection


async def freeze_official_submissions(
    db: AsyncSession, *, actor_id: str, reason_code: str
) -> EventControl:
    """Trusted internal operator operation; intentionally has no participant HTTP route."""
    if not actor_id or len(actor_id) > 64 or not reason_code or len(reason_code) > 64:
        raise ValueError("OFFICIAL_FREEZE_AUDIT_IDENTITY_REQUIRED")
    control = await _control(db, lock=True)
    if control.official_freeze_at is not None:
        raise ValueError("OFFICIAL_SUBMISSIONS_ALREADY_FROZEN")
    invalid_selection = await db.scalar(
        select(SubmissionVersion.id)
        .join(
            OfficialSubmissionSelection,
            OfficialSubmissionSelection.submission_version_id == SubmissionVersion.id,
        )
        .where(SubmissionVersion.state != ArtifactState.ELIGIBLE)
        .with_for_update()
        .limit(1)
    )
    if invalid_selection is not None:
        raise ValueError("OFFICIAL_SELECTION_NO_LONGER_ELIGIBLE")

    frozen_at = _now()
    control.official_freeze_at = frozen_at
    control.official_freeze_actor_id = actor_id
    control.official_freeze_reason = reason_code
    sequence = await db.scalar(
        select(func.max(CompetitionEvent.aggregate_sequence)).where(
            CompetitionEvent.aggregate_type == "event_control",
            CompetitionEvent.aggregate_id == _EVENT_CONTROL_AGGREGATE,
        )
    )
    selection_count = await db.scalar(select(func.count()).select_from(OfficialSubmissionSelection))
    db.add(
        CompetitionEvent(
            event_type="official_submissions.frozen",
            actor_kind="organizer",
            actor_id=actor_id,
            severity="info",
            aggregate_type="event_control",
            aggregate_id=_EVENT_CONTROL_AGGREGATE,
            aggregate_sequence=(sequence or 0) + 1,
            schema_version=1,
            metadata_json={"reason_code": reason_code, "selection_count": selection_count or 0},
            recorded_at=frozen_at,
        )
    )
    await db.commit()
    await db.refresh(control)
    return control
