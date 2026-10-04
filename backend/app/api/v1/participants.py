"""Participant registration and profile endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_participant, get_db
from app.core.config import get_settings
from app.models.competition import SubmissionVersion
from app.models.event import EventControl
from app.models.participant import Participant
from app.schemas.event import ParticipantEventStatus, SelectOfficialSubmissionRequest
from app.schemas.participant import (
    ParticipantRegisterRequest,
    ParticipantRegisterResponse,
    ParticipantResponse,
)
from app.services.auth import AuthService
from app.services.event import get_participant_event_status, select_official_submission
from app.services.rate_limiter import RateLimiter

router = APIRouter(prefix="/participants", tags=["participants"])


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@router.post(
    "/register",
    response_model=ParticipantRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new participant or team",
    description="Assigns a server-generated Participant ID and issues an Access Key (displayed once).",
)
async def register_participant(
    req: ParticipantRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ParticipantRegisterResponse:
    """Register a new participant with rate limiting and uniqueness validation."""
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("User-Agent")
    request_id = getattr(request.state, "request_id", None)

    # Rate limiting on registration by IP
    allowed, retry_after = await RateLimiter.check_rate_limit(
        key_prefix="register",
        identifier=client_ip,
        max_attempts=settings.rate_limit_register_attempts,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Registration rate limit exceeded. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

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

    return ParticipantRegisterResponse(
        participant_id=participant.participant_id,
        name=participant.name,
        email=participant.email,
        team_name=participant.team_name,
        status=participant.status.value,
        access_key=raw_access_key,
        created_at=_utc(participant.created_at),
    )


@router.get(
    "/me",
    response_model=ParticipantResponse,
    summary="Get current participant profile",
    description="Returns the profile of the currently authenticated participant. Never returns secrets.",
)
async def get_my_profile(
    current_participant: Participant = Depends(get_current_active_participant),
) -> ParticipantResponse:
    """Retrieve the authenticated participant's own identity context."""
    return ParticipantResponse(
        participant_id=current_participant.participant_id,
        name=current_participant.name,
        email=current_participant.email,
        team_name=current_participant.team_name,
        status=current_participant.status.value,
        created_at=_utc(current_participant.created_at),
    )


@router.get(
    "/me/event-status",
    response_model=ParticipantEventStatus,
    summary="Get server-authoritative build and event state",
)
async def get_my_event_status(
    current_participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await get_participant_event_status(db, current_participant.participant_id)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Participant event clock is unavailable.",
        ) from None


@router.put(
    "/me/official-submission",
    summary="Select an eligible official submission version before freeze",
)
async def put_official_submission(
    body: SelectOfficialSubmissionRequest,
    current_participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        selection = await select_official_submission(
            db,
            participant_id=current_participant.participant_id,
            submission_id=body.submission_id,
            version_number=body.version,
        )
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Submission version not found.",
        ) from None
    except ValueError as exc:
        status_code = (
            status.HTTP_409_CONFLICT
            if str(exc) == "OFFICIAL_SUBMISSION_FROZEN"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from None
    version = await db.get(SubmissionVersion, selection.submission_version_id)
    control = await db.get(EventControl, 1)
    return {
        "version": version.version,
        "selected_at": selection.selected_at,
        "official_frozen": bool(control and control.official_freeze_at),
        "official_frozen_at": control.official_freeze_at if control else None,
    }
