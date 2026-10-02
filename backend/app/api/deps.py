"""FastAPI dependencies for authentication, authorization, and database access."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.participant import Participant, ParticipantStatus
from app.services.auth import AuthService


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
