"""Administrative and organizer control plane router."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import Settings
from app.matches.controller import MatchConfiguration
from app.models.competition import (
    ArtifactState,
    CompetitionEvent,
    Submission,
    SubmissionVersion,
    ValidationAttempt,
)
from app.models.event import BuildSession, EventControl, OfficialSubmissionSelection
from app.models.match import Match, MatchPurpose, MatchState
from app.models.participant import Participant, ParticipantSession
from app.services.event import freeze_official_submissions
from app.services.submissions import OrganizerApproval, approve_submission_for_eligibility
from app.tournaments.service import schedule_official_match

router = APIRouter(prefix="/admin", tags=["admin"])


def get_settings() -> Settings:
    from app.core.config import Settings
    return Settings()


async def require_organizer(
    x_organizer_secret: Optional[str] = Header(None, alias="X-Organizer-Secret"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate the organizer shared secret against configured environment secret."""
    organizer_secret = os.environ.get("ORGANIZER_SECRET_KEY") or settings.secret_key
    if not x_organizer_secret or x_organizer_secret != organizer_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ORGANIZER_AUTHENTICATION_REQUIRED",
        )
    return "organizer-admin"


class FreezeRequest(BaseModel):
    reason_code: str = "OFFICIAL_BUILD_WINDOW_CLOSED"
    actor_id: str = "organizer-lead"


class ApproveSubmissionRequest(BaseModel):
    submission_version_id: uuid.UUID
    reviewer_id: str = "organizer-validator"
    notes: str = "Eligible for competition"


class ScheduleOfficialMatchRequest(BaseModel):
    white_submission_version_id: uuid.UUID
    black_submission_version_id: uuid.UUID
    initial_clock_ms: int = 300_000
    increment_ms: int = 0
    move_timeout_ms: int = 2_000


@router.get("/overview")
async def get_admin_overview(
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Summary dashboard of event readiness, participation, and match progress."""
    total_participants = await db.scalar(select(func.count(Participant.participant_id)))
    total_submissions = await db.scalar(select(func.count(Submission.id)))
    total_versions = await db.scalar(select(func.count(SubmissionVersion.id)))
    eligible_versions = await db.scalar(
        select(func.count(SubmissionVersion.id)).where(SubmissionVersion.state == ArtifactState.ELIGIBLE)
    )
    official_selections = await db.scalar(select(func.count(OfficialSubmissionSelection.participant_id)))

    control = await db.scalar(select(EventControl).where(EventControl.id == 1))
    official_frozen = control.official_freeze_at is not None if control else False

    total_matches = await db.scalar(select(func.count(Match.id)))
    active_matches = await db.scalar(
        select(func.count(Match.id)).where(Match.state.in_([MatchState.QUEUED, MatchState.STARTING, MatchState.RUNNING]))
    )
    completed_matches = await db.scalar(
        select(func.count(Match.id)).where(Match.state == MatchState.COMPLETED)
    )

    return {
        "participants_count": total_participants or 0,
        "submissions_count": total_submissions or 0,
        "submission_versions_count": total_versions or 0,
        "eligible_versions_count": eligible_versions or 0,
        "official_selections_count": official_selections or 0,
        "official_frozen": official_frozen,
        "official_freeze_at": control.official_freeze_at if control else None,
        "matches_total": total_matches or 0,
        "matches_active": active_matches or 0,
        "matches_completed": completed_matches or 0,
    }


@router.get("/participants")
async def list_admin_participants(
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> list[dict]:
    """Retrieve full participant accounts with submission and session counts."""
    participants = (await db.scalars(select(Participant).order_by(Participant.created_at.asc()))).all()
    results = []
    for p in participants:
        sub_count = await db.scalar(select(func.count(Submission.id)).where(Submission.participant_id == p.participant_id))
        sess_count = await db.scalar(select(func.count(ParticipantSession.id)).where(ParticipantSession.participant_id == p.id))
        selection = await db.scalar(select(OfficialSubmissionSelection).where(OfficialSubmissionSelection.participant_id == p.participant_id))

        results.append({
            "participant_id": p.participant_id,
            "name": p.name,
            "email": p.email,
            "team_name": p.team_name,
            "status": p.status.value,
            "created_at": p.created_at,
            "submissions_count": sub_count or 0,
            "sessions_count": sess_count or 0,
            "has_official_selection": selection is not None,
            "official_submission_version_id": str(selection.submission_version_id) if selection else None,
        })
    return results


@router.get("/submissions")
async def list_admin_submissions(
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> list[dict]:
    """Inspect all participant submissions, version state, digests, and validation outcomes."""
    statement = (
        select(
            SubmissionVersion.id.label("version_id"),
            SubmissionVersion.submission_id,
            SubmissionVersion.participant_id,
            SubmissionVersion.version,
            SubmissionVersion.state,
            SubmissionVersion.artifact_sha256,
            SubmissionVersion.artifact_size,
            SubmissionVersion.build_duration_seconds,
            SubmissionVersion.created_at,
            Participant.name.label("participant_name"),
        )
        .join(Participant, Participant.participant_id == SubmissionVersion.participant_id)
        .order_by(SubmissionVersion.created_at.desc())
    )
    rows = (await db.execute(statement)).all()

    results = []
    for r in rows:
        latest_validation = await db.scalar(
            select(ValidationAttempt)
            .where(ValidationAttempt.submission_version_id == r.version_id)
            .order_by(ValidationAttempt.completed_at.desc())
            .limit(1)
        )
        results.append({
            "version_id": str(r.version_id),
            "submission_id": str(r.submission_id),
            "participant_id": r.participant_id,
            "participant_name": r.participant_name,
            "version": r.version,
            "state": r.state.value if hasattr(r.state, "value") else str(r.state),
            "artifact_sha256": r.artifact_sha256,
            "artifact_size": r.artifact_size,
            "build_duration_seconds": r.build_duration_seconds,
            "created_at": r.created_at,
            "validation_outcome": latest_validation.outcome.value if latest_validation else None,
            "validation_report": latest_validation.report if latest_validation else None,
        })
    return results


@router.post("/submissions/approve")
async def approve_submission(
    payload: ApproveSubmissionRequest,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Explicitly approve an inspected submission version for tournament eligibility."""
    approval = OrganizerApproval(
        actor_id=payload.reviewer_id,
        reason_code="ELIGIBILITY_APPROVED",
        approval_reference=payload.notes[:64] or f"appr-{uuid.uuid4().hex[:12]}",
    )
    try:
        updated = await approve_submission_for_eligibility(
            db,
            submission_version_id=payload.submission_version_id,
            approval=approval,
        )
        return {
            "status": "APPROVED",
            "version_id": str(updated.id),
            "state": updated.state.value,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.post("/freeze")
async def trigger_freeze(
    payload: FreezeRequest,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Trigger the one-way atomic official submission freeze."""
    try:
        control = await freeze_official_submissions(
            db,
            actor_id=payload.actor_id,
            reason_code=payload.reason_code,
        )
        return {
            "status": "FROZEN",
            "official_freeze_at": control.official_freeze_at,
            "actor_id": control.official_freeze_actor_id,
            "reason_code": control.official_freeze_reason,
        }
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )


@router.post("/tournaments/schedule-match")
async def schedule_tournament_match(
    payload: ScheduleOfficialMatchRequest,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Schedule an official tournament match between two frozen official participant selections."""
    config = MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=payload.initial_clock_ms,
        increment_ms=payload.increment_ms,
        move_timeout_ms=payload.move_timeout_ms,
        match_seed=uuid.uuid4().hex + uuid.uuid4().hex,
    )
    try:
        match, sides = await schedule_official_match(
            db,
            white_submission_version_id=payload.white_submission_version_id,
            black_submission_version_id=payload.black_submission_version_id,
            configuration=config,
        )
        return {
            "status": "QUEUED",
            "match_id": str(match.id),
            "purpose": match.purpose.value,
            "white_side_id": str(sides[0].id),
            "black_side_id": str(sides[1].id),
            "created_at": match.created_at,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.get("/audit-log")
async def get_audit_log(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> list[dict]:
    """Retrieve immutable competition events and audit records."""
    events = (
        await db.scalars(
            select(CompetitionEvent)
            .order_by(CompetitionEvent.recorded_at.desc())
            .limit(min(limit, 200))
        )
    ).all()

    return [
        {
            "event_id": str(e.event_id),
            "event_type": e.event_type,
            "actor_kind": e.actor_kind,
            "actor_id": e.actor_id,
            "severity": e.severity,
            "aggregate_type": e.aggregate_type,
            "aggregate_id": str(e.aggregate_id),
            "sequence": e.aggregate_sequence,
            "metadata": e.metadata_json,
            "recorded_at": e.recorded_at,
        }
        for e in events
    ]
