"""Authentication endpoints (login, session token issuance, and logout)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_token_from_request
from app.core.config import get_settings
from app.models.participant import AuthEventType
from app.schemas.participant import (
    LoginRequest,
    ParticipantResponse,
    TokenResponse,
)
from app.services.auth import AuthService
from app.services.rate_limiter import RateLimiter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate participant and issue bearer token",
    description=(
        "Validates Participant ID and Access Key; returns an opaque, revocable bearer session token."
    ),
)
async def login(
    req: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate with rate limiting and constant-time password hash verification."""
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("User-Agent")
    request_id = getattr(request.state, "request_id", None)

    # 1. Rate limiting by IP
    allowed_ip, retry_after_ip = await RateLimiter.check_rate_limit(
        key_prefix="login_ip",
        identifier=client_ip,
        max_attempts=settings.rate_limit_login_attempts,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not allowed_ip:
        await AuthService.record_audit_event(
            db,
            AuthEventType.RATE_LIMITED,
            participant_id=req.participant_id,
            ip_address=client_ip,
            user_agent=user_agent,
            request_id=request_id,
            details=f"IP rate limit exceeded: {client_ip}",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Rate limit exceeded: too many failed login attempts from this IP. "
                f"Retry in {retry_after_ip} seconds."
            ),
            headers={"Retry-After": str(retry_after_ip)},
        )

    # 2. Rate limiting by Participant ID
    allowed_part, retry_after_part = await RateLimiter.check_rate_limit(
        key_prefix="login_part",
        identifier=req.participant_id,
        max_attempts=settings.rate_limit_login_attempts,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not allowed_part:
        await AuthService.record_audit_event(
            db,
            AuthEventType.RATE_LIMITED,
            participant_id=req.participant_id,
            ip_address=client_ip,
            user_agent=user_agent,
            request_id=request_id,
            details=f"Participant ID rate limit exceeded: {req.participant_id}",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Rate limit exceeded: too many failed login attempts for this participant ID. "
                f"Retry in {retry_after_part} seconds."
            ),
            headers={"Retry-After": str(retry_after_part)},
        )

    # 3. Authenticate
    try:
        participant, token, expires_in = await AuthService.authenticate_participant(
            db=db,
            participant_id=req.participant_id,
            access_key=req.access_key,
            ip_address=client_ip,
            user_agent=user_agent,
            request_id=request_id,
        )
    except ValueError as e:
        # Generic error message to prevent account enumeration
        err_msg = str(e)
        if "suspended or revoked" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Participant account is suspended or revoked.",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 4. Clear rate limit counters upon successful authentication
    await RateLimiter.reset("login_ip", client_ip)
    await RateLimiter.reset("login_part", req.participant_id)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        participant=ParticipantResponse(
            participant_id=participant.participant_id,
            name=participant.name,
            email=participant.email,
            team_name=participant.team_name,
            status=participant.status.value,
            created_at=participant.created_at,
        ),
    )


@router.post(
    "/logout",
    status_code=status.HTTP_200_OK,
    summary="Revoke current session bearer token",
    description="Explicitly revokes the bearer token so it can no longer be used.",
)
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Revoke session token."""
    token = get_token_from_request(request)
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("User-Agent")
    request_id = getattr(request.state, "request_id", None)

    revoked = await AuthService.revoke_token(
        db=db,
        token=token,
        ip_address=client_ip,
        user_agent=user_agent,
        request_id=request_id,
    )
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or unrecognized session token.",
        )

    return {"status": "ok", "message": "Successfully logged out."}


from app.api.v1.organizer_auth import router as organizer_auth_router

router.include_router(organizer_auth_router, prefix="/organizer")
