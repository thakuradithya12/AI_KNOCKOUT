"""Participant registration and profile endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_participant, get_db
from app.core.config import get_settings
from app.models.participant import Participant
from app.schemas.participant import (
    ParticipantRegisterRequest,
    ParticipantRegisterResponse,
    ParticipantResponse,
)
from app.services.auth import AuthService
from app.services.rate_limiter import RateLimiter

router = APIRouter(prefix="/participants", tags=["participants"])


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
        created_at=participant.created_at,
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
        created_at=current_participant.created_at,
    )
