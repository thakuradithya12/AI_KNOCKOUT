"""Administrative and organizer control plane router."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_organizer
from app.services.development_matches import (
    DevelopmentMatchError,
    request_trusted_house_development_match,
    run_trusted_development_match,
    serialize_participant_development_match,
)
from app.api.v1.organizer_auth import router as organizer_auth_router
from app.api.v1.participants import _get_cached_registration, _save_cached_registration
from app.core.config import Settings
from app.schemas.participant import (
    ParticipantRegisterRequest,
    ParticipantRegisterResponse,
)
from app.services.auth import AuthService
from app.matches.controller import MatchConfiguration
from app.models.competition import (
    ArtifactState,
    CompetitionEvent,
    Submission,
    SubmissionVersion,
    ValidationAttempt,
)
from app.models.event import (
    BuildSession,
    EventControl,
    OfficialSubmissionSelection,
    ParticipantSubmissionAllowance,
    SubmissionAllowanceRequest,
    TournamentPairingRecord,
    TournamentRoundRecord,
    TournamentSnapshot,
)
from app.matches.service import MatchServiceError, create_match
from app.models.match import (
    Match,
    MatchFailure,
    MatchPurpose,
    MatchResultSource,
    MatchSide,
    MatchSideColor,
    MatchState,
    ParticipantMatchSideBinding,
)
from app.models.organizer import OrganizerSession
from app.models.participant import (
    AuthEvent,
    AuthEventType,
    Participant,
    ParticipantSession,
    ParticipantStatus,
)
from app.services.event import freeze_official_submissions
from app.services.organizer_auth import (
    extract_organizer_token,
    hash_session_token,
    verify_csrf_token,
)
from app.services.submissions import (
    OrganizerApproval,
    approve_submission_for_eligibility,
    count_accepted_submissions,
    decide_allowance_request,
    get_or_create_allowance,
    update_participant_allowance,
)
from app.tournaments.engine import evaluate_pairing_two_games
from app.tournaments.service import (
    CutoffTieError,
    advance_final_clash_stage,
    advance_playoff_stage,
    advance_six_player_finals,
    initialize_final_clash,
    initialize_playoff_bracket,
    initialize_six_player_finals,
    schedule_category_round_robin_round,
    schedule_official_match,
    schedule_swiss_round,
)

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)
router.include_router(organizer_auth_router, prefix="/auth")


# require_organizer dependency is imported from app.api.deps


class FreezeRequest(BaseModel):
    reason_code: str = "OFFICIAL_BUILD_WINDOW_CLOSED"
    actor_id: Optional[str] = None


class ApproveSubmissionRequest(BaseModel):
    submission_version_id: uuid.UUID
    reviewer_id: Optional[str] = None
    notes: str = "Eligible for competition"



class ScheduleOfficialMatchRequest(BaseModel):
    white_submission_version_id: uuid.UUID
    black_submission_version_id: uuid.UUID
    initial_clock_ms: int = 300_000
    increment_ms: int = 0
    move_timeout_ms: int = 2_000
    dispatch_jobs: bool = True
    execution_mode: str = "MODAL_AI_EXECUTION"


class CreateManualMatchRequest(BaseModel):
    white_participant_id: str
    black_participant_id: str
    white_submission_version_id: uuid.UUID
    black_submission_version_id: uuid.UUID
    stage: str = "OFFICIAL"
    initial_clock_ms: int = 300_000
    increment_ms: int = 0
    move_timeout_ms: int = 2_000
    dispatch_jobs: bool = False
    execution_mode: str = "MODAL_AI_EXECUTION"


class CancelMatchRequest(BaseModel):
    reason: str = "Organizer manual cancellation"


class DecideAllowanceRequestPayload(BaseModel):
    decision: str
    notes: Optional[str] = None
    granted_limit: Optional[int] = None
    actor_id: Optional[str] = None


class UpdateAllowancePayload(BaseModel):
    submission_limit: int
    reason: str
    actor_id: Optional[str] = None


class ParticipantActionPayload(BaseModel):
    reason: str = "Organizer administrative action"
    actor_id: Optional[str] = None



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
        allowance = await get_or_create_allowance(db, p.participant_id)
        accepted_sub_count = await count_accepted_submissions(db, p.participant_id)

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
            "submission_limit": allowance.submission_limit,
            "accepted_submissions_count": accepted_sub_count,
            "remaining_submissions": max(0, allowance.submission_limit - accepted_sub_count),
            "allowance_granted_by": allowance.granted_by,
            "allowance_granted_at": allowance.granted_at,
            "allowance_reason": allowance.reason,
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
    actor = payload.reviewer_id or _actor
    approval = OrganizerApproval(
        actor_id=actor,
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
    actor = payload.actor_id or _actor
    try:
        control = await freeze_official_submissions(
            db,
            actor_id=actor,
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
            dispatch_jobs=payload.dispatch_jobs,
            execution_mode=payload.execution_mode,
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


class DispatchMatchJobsPayload(BaseModel):
    execution_mode: str = "MODAL_AI_EXECUTION"


@router.post("/matches/{match_id}/dispatch")
async def admin_dispatch_match_jobs(
    match_id: uuid.UUID,
    payload: DispatchMatchJobsPayload = DispatchMatchJobsPayload(),
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Dispatch execution jobs for a queued match to mTLS workers."""
    from app.workers.service import dispatch_match_jobs, WorkerProtocolError
    try:
        job1, job2 = await dispatch_match_jobs(
            db, match_id=match_id, execution_mode=payload.execution_mode
        )
        return {
            "status": "DISPATCHED",
            "match_id": str(match_id),
            "job_ids": [str(job1.execution_id), str(job2.execution_id)],
            "execution_mode": payload.execution_mode,
        }
    except WorkerProtocolError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/matches")
async def list_admin_matches(
    purpose_filter: Optional[str] = None,
    stage_filter: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> list[dict]:
    """List persisted matches with participants, submission versions, stage, status, and results."""
    stmt = select(Match).order_by(Match.created_at.desc()).limit(min(limit, 200))
    if purpose_filter:
        try:
            p_enum = MatchPurpose(purpose_filter.upper())
            stmt = stmt.where(Match.purpose == p_enum)
        except ValueError:
            pass
    matches_raw = (await db.scalars(stmt)).all()
    results = []
    for m in matches_raw:
        white_binding = await db.scalar(
            select(ParticipantMatchSideBinding)
            .join(MatchSide, MatchSide.id == ParticipantMatchSideBinding.match_side_id)
            .where(
                MatchSide.match_id == m.id,
                MatchSide.color == MatchSideColor.WHITE,
            )
        )
        black_binding = await db.scalar(
            select(ParticipantMatchSideBinding)
            .join(MatchSide, MatchSide.id == ParticipantMatchSideBinding.match_side_id)
            .where(
                MatchSide.match_id == m.id,
                MatchSide.color == MatchSideColor.BLACK,
            )
        )
        white_p = (
            await db.scalar(select(Participant).where(Participant.participant_id == white_binding.participant_id))
            if white_binding else None
        )
        black_p = (
            await db.scalar(select(Participant).where(Participant.participant_id == black_binding.participant_id))
            if black_binding else None
        )

        results.append({
            "match_id": str(m.id),
            "state": m.state.value if hasattr(m.state, "value") else str(m.state),
            "purpose": m.purpose.value if hasattr(m.purpose, "value") else str(m.purpose),
            "stage": "PRACTICE" if m.purpose == MatchPurpose.PEER_TEST else "OFFICIAL",
            "white_participant_id": white_binding.participant_id if white_binding else None,
            "black_participant_id": black_binding.participant_id if black_binding else None,
            "white_name": white_p.name if white_p else (white_binding.participant_id if white_binding else "White Player"),
            "black_name": black_p.name if black_p else (black_binding.participant_id if black_binding else "Black Player"),
            "white_submission_version_id": str(white_binding.submission_version_id) if white_binding else None,
            "black_submission_version_id": str(black_binding.submission_version_id) if black_binding else None,
            "result_outcome": m.result_outcome.value if hasattr(m.result_outcome, "value") else m.result_outcome,
            "result_reason": m.result_reason.value if hasattr(m.result_reason, "value") else m.result_reason,
            "result_source": m.result_source.value if hasattr(m.result_source, "value") else m.result_source,
            "failure_reason": m.failure_reason.value if hasattr(m.failure_reason, "value") else m.failure_reason,
            "initial_clock_ms": m.initial_clock_ms,
            "increment_ms": m.increment_ms,
            "started_at": m.started_at,
            "completed_at": m.completed_at,
            "created_at": m.created_at,
        })
    return results


@router.post("/matches", status_code=status.HTTP_201_CREATED)
async def create_manual_match(
    payload: CreateManualMatchRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Create a manual match under organizer authority with full validation, idempotency, and audit logging."""
    white_id = payload.white_participant_id.strip()
    black_id = payload.black_participant_id.strip()

    # 1. Validate distinct participants
    if white_id == black_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SELF_MATCH_NOT_ALLOWED: White and Black participants must be distinct.",
        )

    # 2. Validate participant existence and active status
    white_p = await db.scalar(select(Participant).where(Participant.participant_id == white_id))
    black_p = await db.scalar(select(Participant).where(Participant.participant_id == black_id))
    if white_p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"PARTICIPANT_NOT_FOUND: {white_id}")
    if black_p is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"PARTICIPANT_NOT_FOUND: {black_id}")

    if white_p.status != ParticipantStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PARTICIPANT_INELIGIBLE: White participant {white_id} status is {white_p.status.value}.",
        )
    if black_p.status != ParticipantStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PARTICIPANT_INELIGIBLE: Black participant {black_id} status is {black_p.status.value}.",
        )

    # 3. Validate submission versions
    white_ver = await db.get(SubmissionVersion, payload.white_submission_version_id)
    black_ver = await db.get(SubmissionVersion, payload.black_submission_version_id)
    if white_ver is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"SUBMISSION_VERSION_NOT_FOUND: {payload.white_submission_version_id}")
    if black_ver is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"SUBMISSION_VERSION_NOT_FOUND: {payload.black_submission_version_id}")

    if white_ver.participant_id != white_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"SUBMISSION_OWNER_MISMATCH: Submission version {payload.white_submission_version_id} does not belong to {white_id}.",
        )
    if black_ver.participant_id != black_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"SUBMISSION_OWNER_MISMATCH: Submission version {payload.black_submission_version_id} does not belong to {black_id}.",
        )

    if white_ver.state != ArtifactState.ELIGIBLE or black_ver.state != ArtifactState.ELIGIBLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SUBMISSION_VERSION_NOT_ELIGIBLE: Both submission versions must be approved as ELIGIBLE for match play.",
        )

    # 4. Determine purpose from stage
    stage_upper = payload.stage.strip().upper()
    if stage_upper in {"PRACTICE", "PEER_TEST", "DEVELOPMENT"}:
        purpose = MatchPurpose.PEER_TEST
    else:
        purpose = MatchPurpose.OFFICIAL

    # 5. Check conflicting active matches & idempotency
    active_matches = (
        await db.scalars(
            select(Match)
            .join(MatchSide, MatchSide.match_id == Match.id)
            .join(ParticipantMatchSideBinding, ParticipantMatchSideBinding.match_side_id == MatchSide.id)
            .where(
                ParticipantMatchSideBinding.participant_id.in_([white_id, black_id]),
                Match.state.in_([MatchState.QUEUED, MatchState.STARTING, MatchState.RUNNING]),
                Match.purpose == purpose,
            )
        )
    ).all()

    # Check for duplicate exact pairing (idempotent retry)
    for active_m in active_matches:
        bindings = (await db.scalars(
            select(ParticipantMatchSideBinding)
            .join(MatchSide, MatchSide.id == ParticipantMatchSideBinding.match_side_id)
            .where(MatchSide.match_id == active_m.id)
        )).all()
        w_b = next((b for b in bindings if b.participant_id == white_id), None)
        b_b = next((b for b in bindings if b.participant_id == black_id), None)
        if (
            w_b and b_b
            and w_b.submission_version_id == payload.white_submission_version_id
            and b_b.submission_version_id == payload.black_submission_version_id
        ):
            return {
                "status": active_m.state.value if hasattr(active_m.state, "value") else str(active_m.state),
                "match_id": str(active_m.id),
                "purpose": active_m.purpose.value,
                "stage": payload.stage,
                "white_participant_id": white_id,
                "black_participant_id": black_id,
                "white_submission_version_id": str(payload.white_submission_version_id),
                "black_submission_version_id": str(payload.black_submission_version_id),
                "created_at": active_m.created_at,
                "idempotent_retry": True,
            }

    if active_matches:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="CONFLICTING_ACTIVE_MATCH: One or both participants already have an active match in progress.",
        )

    # 6. Create match using existing match service
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
        match, sides = await create_match(
            db,
            white_submission_version_id=payload.white_submission_version_id,
            black_submission_version_id=payload.black_submission_version_id,
            configuration=config,
            purpose=purpose,
        )
    except MatchServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.code,
        )

    # 7. Record organizer audit event
    seq = (
        await db.scalar(
            select(func.max(CompetitionEvent.aggregate_sequence)).where(
                CompetitionEvent.aggregate_type == "match",
                CompetitionEvent.aggregate_id == match.id,
            )
        )
    ) or 0

    audit_event = CompetitionEvent(
        event_type="MATCH_MANUALLY_CREATED",
        actor_kind="organizer",
        actor_id=_actor,
        severity="info",
        aggregate_type="match",
        aggregate_id=match.id,
        aggregate_sequence=seq + 1,
        schema_version=1,
        metadata_json={
            "match_id": str(match.id),
            "stage": payload.stage,
            "purpose": purpose.value,
            "white_participant_id": white_id,
            "black_participant_id": black_id,
            "white_submission_version_id": str(payload.white_submission_version_id),
            "black_submission_version_id": str(payload.black_submission_version_id),
            "dispatch_jobs": payload.dispatch_jobs,
            "idempotency_key": idempotency_key,
            "created_at": match.created_at.isoformat(),
        },
        recorded_at=match.created_at,
    )
    db.add(audit_event)

    # 8. Optional job dispatch
    job_ids = []
    if payload.dispatch_jobs:
        from app.workers.service import dispatch_participant_match_jobs
        j1, j2 = await dispatch_participant_match_jobs(
            db, match_id=match.id, execution_mode=payload.execution_mode
        )
        job_ids = [str(j1.execution_id), str(j2.execution_id)]

    await db.commit()

    return {
        "status": match.state.value if hasattr(match.state, "value") else str(match.state),
        "match_id": str(match.id),
        "purpose": match.purpose.value,
        "stage": payload.stage,
        "white_participant_id": white_id,
        "black_participant_id": black_id,
        "white_submission_version_id": str(payload.white_submission_version_id),
        "black_submission_version_id": str(payload.black_submission_version_id),
        "white_side_id": str(sides[0].id),
        "black_side_id": str(sides[1].id),
        "job_ids": job_ids,
        "created_at": match.created_at,
        "idempotent_retry": False,
    }


@router.post("/matches/{match_id}/cancel")
@router.post("/tournaments/matches/{match_id}/cancel")
async def admin_cancel_match(
    match_id: uuid.UUID,
    payload: CancelMatchRequest = CancelMatchRequest(),
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Safely abort/cancel a match with organizer authorization, required reason, and audit logging."""
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="REASON_REQUIRED: A valid reason must be provided for match cancellation.",
        )

    match = await db.scalar(
        select(Match).where(Match.id == match_id).with_for_update()
    )
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MATCH_NOT_FOUND",
        )

    if match.state == MatchState.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="CANNOT_CANCEL_COMPLETED_MATCH: Match is already completed and its result cannot be overturned by cancellation.",
        )

    if match.state == MatchState.ABORTED:
        return {
            "status": "ABORTED",
            "match_id": str(match.id),
            "reason": match.failure_reason.value if hasattr(match.failure_reason, "value") else str(match.failure_reason),
            "completed_at": match.completed_at,
            "idempotent_retry": True,
        }

    now = datetime.now(timezone.utc)
    prev_state = match.state.value if hasattr(match.state, "value") else str(match.state)

    match.state = MatchState.ABORTED
    match.failure_reason = MatchFailure.ABORTED
    match.result_outcome = None
    match.result_reason = None
    match.result_source = None
    match.completed_at = now

    seq = (
        await db.scalar(
            select(func.max(CompetitionEvent.aggregate_sequence)).where(
                CompetitionEvent.aggregate_type == "match",
                CompetitionEvent.aggregate_id == match.id,
            )
        )
    ) or 0

    audit_event = CompetitionEvent(
        event_type="MATCH_CANCELLED",
        actor_kind="organizer",
        actor_id=_actor,
        severity="warning",
        aggregate_type="match",
        aggregate_id=match.id,
        aggregate_sequence=seq + 1,
        schema_version=1,
        metadata_json={
            "match_id": str(match.id),
            "reason": reason,
            "previous_state": prev_state,
            "cancelled_at": now.isoformat(),
        },
        recorded_at=now,
    )
    db.add(audit_event)
    await db.commit()

    return {
        "status": "ABORTED",
        "match_id": str(match.id),
        "reason": reason,
        "completed_at": match.completed_at,
        "idempotent_retry": False,
    }


class ScheduleSwissRoundPayload(BaseModel):
    round_number: int
    tournament_id: str = "official-v1"
    games_per_pairing: Optional[int] = None


class PlayoffActionPayload(BaseModel):
    tournament_id: str = "official-v1"


class FinalsActionPayload(BaseModel):
    tournament_id: str = "official-v1"
    allow_cutoff_fallback: bool = False
    games_per_matchup: int = 1


@router.post("/tournaments/swiss/schedule-round")
async def admin_schedule_swiss_round(
    payload: ScheduleSwissRoundPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Schedule round N of the Swiss-system tournament (qualification 1-game or legacy 2-game)."""
    try:
        round_record = await schedule_swiss_round(
            db,
            round_number=payload.round_number,
            tournament_id=payload.tournament_id,
            games_per_pairing=payload.games_per_pairing,
        )
        return {
            "round_id": str(round_record.id),
            "stage": round_record.stage,
            "round_number": round_record.round_number,
            "round_name": round_record.round_name,
            "status": round_record.status,
            "created_at": round_record.created_at,
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        # Log the internal error server-side; never echo exception text to the client.
        logger.exception("Tournament admin operation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_OPERATION_FAILED",
        ) from exc


@router.post("/tournaments/finals/initialize")
async def admin_initialize_finals(
    payload: FinalsActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Initialize the 6-player Finals bracket (QF: 3v6, 4v5; SF byes: Seeds 1 & 2)."""
    try:
        round_record = await initialize_six_player_finals(
            db,
            tournament_id=payload.tournament_id,
            allow_cutoff_fallback=payload.allow_cutoff_fallback,
            games_per_matchup=payload.games_per_matchup,
        )
        return {
            "round_id": str(round_record.id),
            "stage": round_record.stage,
            "round_number": round_record.round_number,
            "round_name": round_record.round_name,
            "status": round_record.status,
            "created_at": round_record.created_at,
        }
    except CutoffTieError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        # Log the internal error server-side; never echo exception text to the client.
        logger.exception("Tournament admin operation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_OPERATION_FAILED",
        ) from exc


@router.post("/tournaments/finals/advance")
async def admin_advance_finals(
    payload: FinalsActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Advance the 6-player Finals stage (QF -> SF -> Grand Final + Third-Place Playoff -> Champion & 3rd Place)."""
    try:
        res = await advance_six_player_finals(
            db,
            tournament_id=payload.tournament_id,
            games_per_matchup=payload.games_per_matchup,
        )
        if res is None:
            return {"status": "COMPLETED", "detail": "Finals tournament has completed"}
        return {
            "round_id": str(res.id),
            "stage": res.stage,
            "round_number": res.round_number,
            "round_name": res.round_name,
            "status": res.status,
        }
    except Exception as exc:
        # Log the internal error server-side; never echo exception text to the client.
        logger.exception("Tournament admin operation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_OPERATION_FAILED",
        ) from exc


@router.post("/tournaments/playoffs/initialize")
async def admin_initialize_playoffs(
    payload: PlayoffActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Initialize the 6-player Finals bracket (top six qualifiers, two games per pairing)."""
    try:
        round_record = await initialize_playoff_bracket(
            db,
            tournament_id=payload.tournament_id,
        )
        return {
            "round_id": str(round_record.id),
            "stage": round_record.stage,
            "round_number": round_record.round_number,
            "round_name": round_record.round_name,
            "status": round_record.status,
            "created_at": round_record.created_at,
        }
    except CutoffTieError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        # Log the internal error server-side; never echo exception text to the client.
        logger.exception("Tournament admin operation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_OPERATION_FAILED",
        ) from exc


@router.post("/tournaments/playoffs/advance")
async def admin_advance_playoffs(
    payload: PlayoffActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Advance the current active playoff stage, triggering sudden death if tied or moving to next round."""
    try:
        res = await advance_playoff_stage(
            db,
            tournament_id=payload.tournament_id,
        )
        if res is None:
            return {"status": "COMPLETED", "detail": "Playoffs have completed"}
        return {
            "round_id": str(res.id),
            "stage": res.stage,
            "round_number": res.round_number,
            "round_name": res.round_name,
            "status": res.status,
        }
    except Exception as exc:
        # Log the internal error server-side; never echo exception text to the client.
        logger.exception("Tournament admin operation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_OPERATION_FAILED",
        ) from exc


class ScheduleCategoryRoundRobinPayload(BaseModel):
    category: str
    round_number: int
    tournament_id: str = "official-v1"
    use_house_ai_filler: bool = True


@router.post("/tournaments/category-round-robin/schedule-round")
async def admin_schedule_category_round_robin(
    payload: ScheduleCategoryRoundRobinPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Schedule round N of the Category A or B round-robin stage with 2 games per pairing (reversed colors)."""
    try:
        round_record = await schedule_category_round_robin_round(
            db,
            category=payload.category,
            round_number=payload.round_number,
            tournament_id=payload.tournament_id,
            use_house_ai_filler=payload.use_house_ai_filler,
        )
        return {
            "round_id": str(round_record.id),
            "stage": round_record.stage,
            "round_number": round_record.round_number,
            "round_name": round_record.round_name,
            "status": round_record.status,
            "created_at": round_record.created_at,
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{type(exc).__name__}: {str(exc)}")


@router.post("/tournaments/final-clash/initialize")
async def admin_initialize_final_clash(
    payload: PlayoffActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Initialize the Final Clash 6-player playoff bracket with A1 and B1 Semifinal byes."""
    try:
        round_record = await initialize_final_clash(
            db,
            tournament_id=payload.tournament_id,
        )
        return {
            "round_id": str(round_record.id),
            "stage": round_record.stage,
            "round_number": round_record.round_number,
            "round_name": round_record.round_name,
            "status": round_record.status,
            "created_at": round_record.created_at,
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        # Log the internal error server-side; never echo exception text to the client.
        logger.exception("Tournament admin operation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_OPERATION_FAILED",
        ) from exc


@router.post("/tournaments/final-clash/advance")
async def admin_advance_final_clash(
    payload: PlayoffActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Advance the Final Clash stage (Quarterfinals -> Semifinals -> Finals -> Champion)."""
    try:
        res = await advance_final_clash_stage(
            db,
            tournament_id=payload.tournament_id,
        )
        if res is None:
            return {"status": "COMPLETED", "detail": "Final Clash has completed"}
        return {
            "round_id": str(res.id),
            "stage": res.stage,
            "round_number": res.round_number,
            "round_name": res.round_name,
            "status": res.status,
        }
    except Exception as exc:
        # Log the internal error server-side; never echo exception text to the client.
        logger.exception("Tournament admin operation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_OPERATION_FAILED",
        ) from exc


VALID_MATCH_OUTCOMES = {"WHITE_WIN", "BLACK_WIN", "DRAW"}
VALID_MATCH_REASONS = {
    "CHECKMATE",
    "STALEMATE",
    "DEAD_POSITION",
    "FIVEFOLD_REPETITION",
    "SEVENTY_FIVE_MOVE_RULE",
    "THREEFOLD_REPETITION",
    "FIFTY_MOVE_RULE",
}


class RecordOfficialMatchResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_outcome: str
    result_reason: str = "CHECKMATE"


@router.post("/tournaments/matches/{match_id}/record-result")
async def admin_record_match_result(
    match_id: uuid.UUID,
    payload: RecordOfficialMatchResultRequest,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Record verified official chess match result and update tournament standings."""
    outcome = payload.result_outcome.strip().upper()
    if outcome not in VALID_MATCH_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"INVALID_RESULT_OUTCOME: Must be one of {sorted(VALID_MATCH_OUTCOMES)}",
        )

    reason = payload.result_reason.strip().upper()
    if reason not in VALID_MATCH_REASONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"INVALID_RESULT_REASON: Must be one of {sorted(VALID_MATCH_REASONS)}",
        )

    match = await db.scalar(
        select(Match).where(Match.id == match_id).with_for_update()
    )
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MATCH_NOT_FOUND",
        )

    if match.purpose != MatchPurpose.OFFICIAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="OFFICIAL_MATCH_REQUIRED: Only official tournament matches can be recorded through this endpoint.",
        )

    # Idempotency and conflict handling
    if match.state == MatchState.COMPLETED:
        current_outcome = match.result_outcome.value if hasattr(match.result_outcome, "value") else str(match.result_outcome)
        current_reason = match.result_reason.value if hasattr(match.result_reason, "value") else str(match.result_reason)
        if current_outcome == outcome and current_reason == reason:
            return {
                "status": "COMPLETED",
                "match_id": str(match.id),
                "result_outcome": current_outcome,
                "result_reason": current_reason,
                "result_source": match.result_source.value if hasattr(match.result_source, "value") else (match.result_source or MatchResultSource.UNKNOWN.value),
                "started_at": match.started_at,
                "completed_at": match.completed_at,
                "idempotent_retry": True,
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"CONFLICTING_RESULT: Match is already completed with outcome {current_outcome} ({current_reason}).",
        )

    if match.state == MatchState.ABORTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MATCH_ABORTED: Match was aborted and cannot record a completed result.",
        )

    now = datetime.now(timezone.utc)
    prev_state = match.state.value if hasattr(match.state, "value") else str(match.state)

    try:
        # Follow state machine transitions: QUEUED -> STARTING -> RUNNING -> COMPLETED
        if match.state == MatchState.QUEUED:
            match.state = MatchState.STARTING
            await db.flush()
            match.state = MatchState.RUNNING
            match.started_at = match.started_at or now
            await db.flush()
        elif match.state == MatchState.STARTING:
            match.state = MatchState.RUNNING
            match.started_at = match.started_at or now
            await db.flush()
        elif match.state == MatchState.RUNNING:
            if match.started_at is None:
                match.started_at = now
            await db.flush()

        match.state = MatchState.COMPLETED
        match.result_outcome = outcome
        match.result_reason = reason
        match.result_source = MatchResultSource.MANUAL
        match.completed_at = now
        await db.flush()

        # Emit auditable CompetitionEvent
        seq = await db.scalar(
            select(func.max(CompetitionEvent.aggregate_sequence)).where(
                CompetitionEvent.aggregate_type == "match",
                CompetitionEvent.aggregate_id == match.id,
            )
        )
        next_seq = (seq or 0) + 1

        audit_event = CompetitionEvent(
            event_type="MATCH_RESULT_RECORDED",
            actor_kind="organizer",
            actor_id=_actor,
            severity="info",
            aggregate_type="match",
            aggregate_id=match.id,
            aggregate_sequence=next_seq,
            schema_version=1,
            metadata_json={
                "match_id": str(match.id),
                "result_outcome": outcome,
                "result_reason": reason,
                "result_source": MatchResultSource.MANUAL.value,
                "previous_state": prev_state,
                "recorded_at": now.isoformat(),
            },
            recorded_at=now,
        )
        db.add(audit_event)

        # Synchronize associated TournamentPairingRecord if present
        pairing = await db.scalar(
            select(TournamentPairingRecord).where(
                or_(
                    TournamentPairingRecord.game1_match_id == match.id,
                    TournamentPairingRecord.game2_match_id == match.id,
                )
            ).with_for_update()
        )
        if pairing is None:
            # Check sudden death in playoff pairings
            pairings = (await db.execute(select(TournamentPairingRecord))).scalars().all()
            for p in pairings:
                if p.sudden_death_match_ids and str(match.id) in p.sudden_death_match_ids:
                    pairing = p
                    break

        if pairing is not None:
            m1 = await db.get(Match, pairing.game1_match_id) if pairing.game1_match_id else None
            m2 = await db.get(Match, pairing.game2_match_id) if pairing.game2_match_id else None

            m1_out = m1.result_outcome.value if (m1 and hasattr(m1.result_outcome, "value")) else (m1.result_outcome if m1 else None)
            m2_out = m2.result_outcome.value if (m2 and hasattr(m2.result_outcome, "value")) else (m2.result_outcome if m2 else None)

            if m1 and m1.state != MatchState.COMPLETED:
                m1_out = None
            if m2 and m2.state != MatchState.COMPLETED:
                m2_out = None

            sd_outcomes = []
            if pairing.sudden_death_match_ids:
                for sd_id_str in pairing.sudden_death_match_ids:
                    try:
                        sd_m = await db.get(Match, uuid.UUID(sd_id_str))
                        if sd_m and sd_m.state == MatchState.COMPLETED and sd_m.result_outcome:
                            sd_val = sd_m.result_outcome.value if hasattr(sd_m.result_outcome, "value") else str(sd_m.result_outcome)
                            sd_outcomes.append(sd_val)
                    except (ValueError, TypeError):
                        pass

            if m1_out and m2_out:
                p1_pts, p2_pts, winner_code, needs_sd = evaluate_pairing_two_games(m1_out, m2_out, sd_outcomes)
                pairing.p1_points = p1_pts
                pairing.p2_points = p2_pts
                if winner_code:
                    pairing.winner_id = pairing.participant1_id if winner_code == "P1" else pairing.participant2_id
                    pairing.status = "COMPLETED"
                elif needs_sd:
                    pairing.status = "IN_PROGRESS"
                else:
                    pairing.status = "COMPLETED"
            else:
                pairing.status = "IN_PROGRESS"
            await db.flush()

        await db.commit()
        await db.refresh(match)
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to record organizer match result")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="RECORD_RESULT_FAILED",
        ) from exc

    return {
        "status": "COMPLETED",
        "match_id": str(match.id),
        "result_outcome": match.result_outcome,
        "result_reason": match.result_reason,
        "result_source": match.result_source.value if hasattr(match.result_source, "value") else match.result_source,
        "started_at": match.started_at,
        "completed_at": match.completed_at,
        "idempotent_retry": False,
    }


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


@router.get("/allowance-requests")
async def list_admin_allowance_requests(
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> list[dict]:
    """List participant quota allowance requests for organizer review."""
    stmt = (
        select(
            SubmissionAllowanceRequest,
            Participant.name.label("participant_name"),
            Participant.team_name,
        )
        .join(Participant, Participant.participant_id == SubmissionAllowanceRequest.participant_id)
        .order_by(SubmissionAllowanceRequest.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(SubmissionAllowanceRequest.status == status_filter.upper())
    rows = (await db.execute(stmt)).all()

    results = []
    for req, name, team in rows:
        allowance = await get_or_create_allowance(db, req.participant_id)
        accepted = await count_accepted_submissions(db, req.participant_id)
        results.append({
            "id": str(req.id),
            "participant_id": req.participant_id,
            "participant_name": name,
            "team_name": team,
            "current_limit": allowance.submission_limit,
            "accepted_count": accepted,
            "requested_limit": req.requested_limit,
            "reason": req.reason,
            "status": req.status,
            "created_at": req.created_at,
            "reviewed_by": req.reviewed_by,
            "reviewed_at": req.reviewed_at,
            "review_notes": req.review_notes,
        })
    return results


@router.post("/allowance-requests/{request_id}/decision")
async def decide_admin_allowance_request(
    request_id: uuid.UUID,
    payload: DecideAllowanceRequestPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Approve or reject a participant's submission allowance request."""
    try:
        req = await decide_allowance_request(
            db,
            request_id=request_id,
            decision=payload.decision,
            actor_id=payload.actor_id or _actor,
            notes=payload.notes,
            granted_limit=payload.granted_limit,
        )
        return {
            "id": str(req.id),
            "participant_id": req.participant_id,
            "status": req.status,
            "reviewed_by": req.reviewed_by,
            "reviewed_at": req.reviewed_at,
            "review_notes": req.review_notes,
        }
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/participants/{participant_id}/allowance")
async def update_participant_submission_allowance(
    participant_id: str,
    payload: UpdateAllowancePayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Directly set a participant's submission quota limit with durable audit record."""
    participant = await db.scalar(
        select(Participant).where(Participant.participant_id == participant_id)
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PARTICIPANT_NOT_FOUND",
        )
    try:
        allowance = await update_participant_allowance(
            db,
            participant_id=participant_id,
            new_limit=payload.submission_limit,
            actor_id=payload.actor_id or _actor,
            reason=payload.reason,
        )
        return {
            "participant_id": allowance.participant_id,
            "submission_limit": allowance.submission_limit,
            "granted_by": allowance.granted_by,
            "granted_at": allowance.granted_at,
            "reason": allowance.reason,
            "updated_at": allowance.updated_at,
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def _record_participant_audit(
    db: AsyncSession,
    participant: Participant,
    event_type: str,
    auth_event_type: Optional[AuthEventType],
    actor_id: str,
    severity: str,
    reason: str,
    extra_meta: Optional[dict] = None,
) -> None:
    now = datetime.now(timezone.utc)
    agg_id = uuid.uuid5(uuid.NAMESPACE_DNS, participant.participant_id)
    seq = (
        await db.scalar(
            select(func.max(CompetitionEvent.aggregate_sequence)).where(
                CompetitionEvent.aggregate_type == "participant",
                CompetitionEvent.aggregate_id == agg_id,
            )
        )
    ) or 0
    meta = {
        "participant_id": participant.participant_id,
        "name": participant.name,
        "reason": reason,
        "actor_id": actor_id,
    }
    if extra_meta:
        meta.update(extra_meta)
    db.add(
        CompetitionEvent(
            event_type=event_type,
            actor_kind="organizer",
            actor_id=actor_id,
            severity=severity,
            aggregate_type="participant",
            aggregate_id=agg_id,
            aggregate_sequence=seq + 1,
            schema_version=1,
            metadata_json=meta,
            recorded_at=now,
        )
    )
    if auth_event_type:
        db.add(
            AuthEvent(
                participant_id=participant.participant_id,
                event_type=auth_event_type,
                details=f"{event_type}: {reason}",
            )
        )


@router.post(
    "/participants/register",
    response_model=ParticipantRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new participant (Organizer only)",
    description="Assigns a server-generated Participant ID and issues an Access Key (displayed once). Requires organizer authorization.",
)
async def register_participant_admin(
    req: ParticipantRegisterRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> ParticipantRegisterResponse:
    """Register a new participant under organizer authority and record audit trail."""
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("User-Agent")
    request_id = getattr(request.state, "request_id", None)

    payload_hash: str | None = None
    if idempotency_key is not None:
        if not idempotency_key or len(idempotency_key.encode("utf-8")) > 128:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid Idempotency-Key header length (must be 1-128 bytes).",
            )
        if re.search(r"[\x00-\x1f\x7f]", idempotency_key):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Idempotency-Key header contains invalid characters.",
            )
        payload_bytes = json.dumps(req.model_dump(), sort_keys=True).encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        cached_data, mismatch = await _get_cached_registration(idempotency_key, payload_hash)
        if mismatch:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key was previously used with a different request payload.",
            )
        if cached_data:
            return ParticipantRegisterResponse(**cached_data)

    try:
        participant, raw_access_key = await AuthService.register_participant(
            db=db,
            req=req,
            ip_address=client_ip,
            user_agent=user_agent,
            request_id=request_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    # Record organizer audit event
    await _record_participant_audit(
        db,
        participant=participant,
        event_type="participant_registered",
        auth_event_type=AuthEventType.REGISTRATION_SUCCESS,
        actor_id=_actor,
        severity="info",
        reason="Organizer issued participant registration",
        extra_meta={
            "team_name": participant.team_name,
            "email": participant.email,
        },
    )
    await db.commit()

    created_at = participant.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)

    res = ParticipantRegisterResponse(
        participant_id=participant.participant_id,
        name=participant.name,
        email=participant.email,
        team_name=participant.team_name,
        status=participant.status.value,
        access_key=raw_access_key,
        created_at=created_at,
    )

    if idempotency_key is not None and payload_hash is not None:
        await _save_cached_registration(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            data=res.model_dump(mode="json"),
            ttl=300,
        )

    return res


@router.post("/participants/{participant_id}/suspend")
async def suspend_participant(
    participant_id: str,
    payload: ParticipantActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Suspend a participant account, revoke active sessions, and record audit logs."""
    participant = await db.scalar(
        select(Participant).where(Participant.participant_id == participant_id)
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PARTICIPANT_NOT_FOUND",
        )
    actor = payload.actor_id or _actor
    now = datetime.now(timezone.utc)
    participant.status = ParticipantStatus.SUSPENDED

    # Invalidate active sessions immediately
    revoked_res = await db.execute(
        update(ParticipantSession)
        .where(
            ParticipantSession.participant_id == participant.id,
            ParticipantSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    revoked_count = revoked_res.rowcount or 0

    await _record_participant_audit(
        db,
        participant=participant,
        event_type="participant.suspended",
        auth_event_type=AuthEventType.STATUS_CHANGED,
        actor_id=actor,
        severity="warning",
        reason=payload.reason,
        extra_meta={"revoked_sessions": revoked_count},
    )
    await db.commit()

    return {
        "participant_id": participant.participant_id,
        "status": participant.status.value,
        "revoked_sessions": revoked_count,
        "audited": True,
    }


@router.post("/participants/{participant_id}/reinstate")
async def reinstate_participant(
    participant_id: str,
    payload: ParticipantActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Reinstate a suspended participant back to ACTIVE status with audit tracking."""
    participant = await db.scalar(
        select(Participant).where(Participant.participant_id == participant_id)
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PARTICIPANT_NOT_FOUND",
        )
    actor = payload.actor_id or _actor
    participant.status = ParticipantStatus.ACTIVE

    await _record_participant_audit(
        db,
        participant=participant,
        event_type="participant.reinstated",
        auth_event_type=AuthEventType.STATUS_CHANGED,
        actor_id=actor,
        severity="info",
        reason=payload.reason,
    )
    await db.commit()

    return {
        "participant_id": participant.participant_id,
        "status": participant.status.value,
        "audited": True,
    }


@router.post("/participants/{participant_id}/revoke-sessions")
async def revoke_participant_sessions(
    participant_id: str,
    payload: ParticipantActionPayload,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Revoke all active session tokens for a participant."""
    participant = await db.scalar(
        select(Participant).where(Participant.participant_id == participant_id)
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PARTICIPANT_NOT_FOUND",
        )
    actor = payload.actor_id or _actor
    now = datetime.now(timezone.utc)

    revoked_res = await db.execute(
        update(ParticipantSession)
        .where(
            ParticipantSession.participant_id == participant.id,
            ParticipantSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    revoked_count = revoked_res.rowcount or 0

    await _record_participant_audit(
        db,
        participant=participant,
        event_type="participant.sessions_revoked",
        auth_event_type=AuthEventType.SESSION_REVOKED,
        actor_id=actor,
        severity="info",
        reason=payload.reason,
        extra_meta={"revoked_sessions": revoked_count},
    )
    await db.commit()

    return {
        "participant_id": participant.participant_id,
        "revoked_sessions": revoked_count,
        "audited": True,
    }


@router.delete("/participants/{participant_id}")
async def remove_participant(
    participant_id: str,
    reason: str = "Administrative cleanup",
    actor_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> dict:
    """Safely remove an unused participant account while strictly safeguarding tournament records."""
    participant = await db.scalar(
        select(Participant).where(Participant.participant_id == participant_id)
    )
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PARTICIPANT_NOT_FOUND",
        )
    actor = actor_id or _actor

    # 1. Enforce safety checks against tournament and competition records
    sub_count = await db.scalar(
        select(func.count(Submission.id)).where(Submission.participant_id == participant_id)
    )
    snap_count = await db.scalar(
        select(func.count(TournamentSnapshot.id)).where(TournamentSnapshot.participant_id == participant_id)
    )
    pairing_count = await db.scalar(
        select(func.count(TournamentPairingRecord.id)).where(
            or_(
                TournamentPairingRecord.participant1_id == participant_id,
                TournamentPairingRecord.participant2_id == participant_id,
            )
        )
    )
    sel_count = await db.scalar(
        select(func.count(OfficialSubmissionSelection.participant_id)).where(
            OfficialSubmissionSelection.participant_id == participant_id
        )
    )

    if (sub_count or 0) > 0 or (snap_count or 0) > 0 or (pairing_count or 0) > 0 or (sel_count or 0) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="CANNOT_REMOVE_PARTICIPANT_WITH_TOURNAMENT_RECORDS: Participant has existing submissions, pairings, or tournament snapshots. Suspend the participant instead to preserve tournament audit integrity.",
        )

    # 2. Record audit log before removal
    await _record_participant_audit(
        db,
        participant=participant,
        event_type="participant.removed",
        auth_event_type=None,
        actor_id=actor,
        severity="warning",
        reason=reason,
    )

    # 3. Clean up child records without tournament history
    is_postgres = False
    try:
        bind = db.get_bind()
        is_postgres = "postgres" in str(bind.dialect.name).lower()
    except Exception:
        pass

    if is_postgres:
        await db.execute(text("ALTER TABLE build_sessions DISABLE TRIGGER build_sessions_immutable;"))

    try:
        await db.execute(
            delete(BuildSession).where(BuildSession.participant_id == participant_id)
        )
        await db.execute(
            delete(SubmissionAllowanceRequest).where(SubmissionAllowanceRequest.participant_id == participant_id)
        )
        await db.execute(
            delete(ParticipantSubmissionAllowance).where(ParticipantSubmissionAllowance.participant_id == participant_id)
        )
        await db.execute(
            delete(ParticipantSession).where(ParticipantSession.participant_id == participant.id)
        )
        await db.delete(participant)
        if is_postgres:
            await db.execute(text("ALTER TABLE build_sessions ENABLE TRIGGER build_sessions_immutable;"))
        await db.commit()
    except Exception:
        await db.rollback()
        if is_postgres:
            try:
                await db.execute(text("ALTER TABLE build_sessions ENABLE TRIGGER build_sessions_immutable;"))
                await db.commit()
            except Exception:
                pass
        raise

    return {
        "participant_id": participant_id,
        "removed": True,
        "audited": True,
    }


SUPPORTED_TRUSTED_HOUSE_AIS = {
    "house-ai-benchmark-baseline",
    "house-ai-maximum-difficulty",
    "AIK-HOUSE-001",
}


class AdminTrustedHouseMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: Optional[str] = Field(
        default=None,
        description="Optional active participant ID to own development match idempotency",
    )
    white_house_ai_id: str = Field(
        default="house-ai-benchmark-baseline",
        description="Trusted House AI for white side",
    )
    black_house_ai_id: str = Field(
        default="AIK-HOUSE-001",
        description="Trusted House AI for black side",
    )
    execute_now: bool = Field(
        default=False,
        description="If True, immediately runs the trusted match in-process via MatchCoordinator",
    )


async def _ensure_trusted_runner_participant(
    db: AsyncSession, participant_id: Optional[str] = None
) -> str:
    if participant_id:
        p = await db.scalar(
            select(Participant).where(Participant.participant_id == participant_id)
        )
        if p is None or p.status != ParticipantStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="PARTICIPANT_NOT_ACTIVE",
            )
        return p.participant_id

    # Default to the dedicated system runner. Borrowing an arbitrary real participant
    # would show the organizer's duel in that participant's practice-match list and
    # block their own practice requests while the duel is active.
    runner_id = "system-trusted-runner"
    runner = await db.scalar(
        select(Participant).where(Participant.participant_id == runner_id)
    )
    if runner is None:
        runner = Participant(
            participant_id=runner_id,
            name="System Trusted Runner",
            status=ParticipantStatus.ACTIVE,
            access_key_hash=hashlib.sha256(b"system-trusted-runner").hexdigest(),
            created_at=datetime.now(timezone.utc),
        )
        db.add(runner)
        await db.flush()
    return runner.participant_id


@router.post("/development-matches/house-duel", status_code=status.HTTP_201_CREATED)
async def admin_create_trusted_house_match(
    body: AdminTrustedHouseMatchRequest,
    response: Response,
    _actor: str = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Organizer-only endpoint to queue or execute a trusted House AI vs House AI match."""
    if body.white_house_ai_id not in SUPPORTED_TRUSTED_HOUSE_AIS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"UNSUPPORTED_HOUSE_AI_ENGINE: {body.white_house_ai_id}",
        )
    if body.black_house_ai_id not in SUPPORTED_TRUSTED_HOUSE_AIS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"UNSUPPORTED_HOUSE_AI_ENGINE: {body.black_house_ai_id}",
        )
    if body.white_house_ai_id == body.black_house_ai_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="DUPLICATE_HOUSE_AI_SIDE",
        )

    runner_participant_id = await _ensure_trusted_runner_participant(db, body.participant_id)
    resolved_idempotency_key = idempotency_key or f"admin-house-duel-{uuid.uuid4().hex}"

    try:
        match, created = await request_trusted_house_development_match(
            db,
            participant_id=runner_participant_id,
            idempotency_key=resolved_idempotency_key,
            white_house_ai_id=body.white_house_ai_id,
            black_house_ai_id=body.black_house_ai_id,
        )
    except DevelopmentMatchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

    if not created:
        response.status_code = status.HTTP_200_OK

    executed = False
    if body.execute_now:
        if match.state in (MatchState.QUEUED, MatchState.STARTING):
            match = await run_trusted_development_match(db, match_id=match.id, max_plies=100)
            executed = True
        execution_note = "Match executed to completion in-process via trusted coordinator."
    else:
        execution_note = "Match queued. Out-of-process worker execution remains unwired unless execute_now is set."

    serialized = await serialize_participant_development_match(db, match)
    serialized["executed"] = executed
    serialized["execution_note"] = execution_note
    serialized["idempotent_replay"] = not created
    return serialized


@router.get("/development-matches/{match_id}")
async def admin_inspect_development_match(
    match_id: uuid.UUID,
    _actor: str = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Organizer-only endpoint to inspect a development match by ID."""
    match = await db.get(Match, match_id)
    if match is None or match.purpose != MatchPurpose.DEVELOPMENT:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MATCH_NOT_FOUND")
    try:
        return await serialize_participant_development_match(db, match)
    except DevelopmentMatchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc


