"""FastAPI dependencies for authentication, authorization, and database access."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
import os
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models.organizer import OrganizerSession
from app.models.participant import Participant, ParticipantStatus
from app.services.auth import AuthService
from app.services.organizer_auth import (
    extract_organizer_token,
    hash_session_token,
    verify_csrf_token,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injecting an async database session."""
    async for session in get_session():
        yield session


def get_token_from_request(request: Request) -> str:
    """Extract and validate the Bearer token from Authorization header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization scheme. Bearer token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token string cannot be empty",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


async def get_current_participant(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Participant:
    """Dependency verifying the bearer token and retrieving the active participant."""
    token = get_token_from_request(request)
    participant = await AuthService.get_participant_by_token(db, token)
    if not participant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked session token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Attach participant to request state for middleware/logging
    request.state.participant_id = participant.participant_id
    return participant


async def get_current_active_participant(
    participant: Participant = Depends(get_current_participant),
) -> Participant:
    """Enforce that the authenticated participant is in ACTIVE status."""
    if participant.status != ParticipantStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Participant account is suspended or revoked",
        )
    return participant


async def require_organizer(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_organizer_secret: Optional[str] = Header(None, alias="X-Organizer-Secret"),
    x_csrf_token: Optional[str] = Header(None, alias="X-CSRF-Token"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Authorize organizer requests via server-managed session (preferred) or secret key fallback.

    Enforces:
    1. Session lookup and validation (expiry and idle timeouts).
    2. CSRF verification on mutating methods (POST, PUT, PATCH, DELETE) for session auth.
    3. Audit attribution returning organizer email.
    4. Fallback to X-Organizer-Secret for test fixtures / CLI tools.
    """
    raw_token = extract_organizer_token(request)
    if raw_token:
        token_hash = hash_session_token(raw_token)
        session = await db.scalar(
            select(OrganizerSession).where(OrganizerSession.session_token_hash == token_hash)
        )
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="ORGANIZER_SESSION_INVALID",
            )
        if not session.is_active(settings.organizer_session_idle_minutes):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="ORGANIZER_SESSION_EXPIRED",
            )

        # Mutating requests strictly require matching CSRF token
        if request.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            if not verify_csrf_token(session, x_csrf_token):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="CSRF_VERIFICATION_FAILED",
                )

        # Slide idle expiration tracker
        session.last_active_at = datetime.now(timezone.utc)
        await db.flush()

        return session.organizer_email

    # Fallback to X-Organizer-Secret for local development/test fixtures only
    if x_organizer_secret:
        if settings.is_deployed:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="ORGANIZER_SHARED_SECRET_DISABLED_IN_DEPLOYED_ENV",
            )
        organizer_secret = os.environ.get("ORGANIZER_SECRET_KEY") or settings.secret_key
        if x_organizer_secret == organizer_secret:
            return "organizer-admin"

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="ORGANIZER_AUTHENTICATION_REQUIRED",
    )
