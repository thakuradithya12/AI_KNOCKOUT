"""Participant-owned development match request and inspection endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_participant, get_db
from app.models.participant import Participant
from app.services.development_matches import (
    DevelopmentMatchError,
    get_participant_development_match,
    list_participant_development_matches,
    request_development_match,
    serialize_participant_development_match,
)

router = APIRouter(prefix="/participants/me/development-matches", tags=["development-matches"])


class DevelopmentMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: uuid.UUID
    version: StrictInt = Field(ge=1)
    side: str | None = Field(default="WHITE", pattern="^(WHITE|BLACK|white|black)$")
    house_ai_id: str | None = Field(default=None, max_length=64)


def _raise_http_error(exc: DevelopmentMatchError) -> None:
    messages = {
        "PARTICIPANT_NOT_ACTIVE": "Participant account is not active.",
        "SUBMISSION_VERSION_NOT_FOUND": "Eligible submission version not found.",
        "SUBMISSION_VERSION_NOT_ELIGIBLE": "Submission version is not eligible.",
        "HOUSE_AI_CONFIGURATION_INVALID": "Development match configuration is unavailable.",
        "DEVELOPMENT_POLICY_UNAVAILABLE": "Development match policy is unavailable.",
        "ACTIVE_DEVELOPMENT_MATCH_EXISTS": "An active development match already exists.",
        "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST": "Idempotency key was already used.",
    }
    raise HTTPException(
        status_code=exc.status_code,
        detail=messages.get(exc.code, exc.code),
    ) from None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_development_match(
    body: DevelopmentMatchRequest,
    response: Response,
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> dict:
    try:
        match, created = await request_development_match(
            db,
            participant_id=participant.participant_id,
            submission_id=body.submission_id,
            version_number=body.version,
            idempotency_key=idempotency_key,
            side=(body.side or "WHITE").upper(),
            house_ai_id=body.house_ai_id.strip() if body.house_ai_id else None,
        )
        result = await serialize_participant_development_match(db, match)
    except DevelopmentMatchError as exc:
        _raise_http_error(exc)
    # A replay returns the existing identity without creating another match.
    result["idempotent_replay"] = not created
    if not created:
        response.status_code = status.HTTP_200_OK
    return result


@router.get("")
async def list_development_matches(
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    matches = await list_participant_development_matches(
        db, participant_id=participant.participant_id
    )
    try:
        items = [await serialize_participant_development_match(db, match) for match in matches]
    except DevelopmentMatchError as exc:
        _raise_http_error(exc)
    return {"items": items}


@router.get("/{match_id}")
async def inspect_development_match(
    match_id: uuid.UUID,
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    match = await get_participant_development_match(
        db, participant_id=participant.participant_id, match_id=match_id
    )
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
    try:
        return await serialize_participant_development_match(db, match)
    except DevelopmentMatchError as exc:
        _raise_http_error(exc)
