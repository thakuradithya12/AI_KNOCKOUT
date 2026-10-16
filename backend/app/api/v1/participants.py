"""Participant registration and profile endpoints."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_participant, get_db, require_organizer
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
from app.services.redis import get_redis

router = APIRouter(prefix="/participants", tags=["participants"])

# In-memory idempotency cache fallback: key -> (payload_hash, response_dict, expires_at)
_reg_idempotency_store: dict[str, tuple[str, dict, float]] = {}


async def _get_cached_registration(idempotency_key: str, payload_hash: str) -> tuple[dict | None, bool]:
    """Retrieve cached registration if exists. Returns (cached_data, hash_mismatch)."""
    redis_key = f"aik:idemp:register:{idempotency_key}"
    try:
        redis_client = get_redis()
        cached = await redis_client.get(redis_key)
        if cached:
            if isinstance(cached, bytes):
                cached = cached.decode("utf-8")
            entry = json.loads(cached)
            if entry.get("hash") != payload_hash:
                return None, True
            return entry.get("data"), False
    except Exception:
        pass

    now = time.monotonic()
    expired = [k for k, (_, _, exp) in _reg_idempotency_store.items() if exp <= now]
    for k in expired:
        _reg_idempotency_store.pop(k, None)

    if idempotency_key in _reg_idempotency_store:
        stored_hash, data, exp = _reg_idempotency_store[idempotency_key]
        if now < exp:
            if stored_hash != payload_hash:
                return None, True
            return data, False
        else:
            _reg_idempotency_store.pop(idempotency_key, None)
    return None, False


async def _save_cached_registration(idempotency_key: str, payload_hash: str, data: dict, ttl: int = 300) -> None:
    """Cache registration response under idempotency key."""
    redis_key = f"aik:idemp:register:{idempotency_key}"
    try:
        redis_client = get_redis()
        payload = json.dumps({"hash": payload_hash, "data": data})
        await redis_client.set(redis_key, payload, ex=ttl)
    except Exception:
        pass
    now = time.monotonic()
    _reg_idempotency_store[idempotency_key] = (payload_hash, data, now + ttl)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@router.post(
    "/register",
    response_model=ParticipantRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new participant or team (Organizer only)",
    description="Assigns a server-generated Participant ID and issues an Access Key (displayed once). Requires organizer authorization.",
)
async def register_participant(
    req: ParticipantRegisterRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    _actor: str = Depends(require_organizer),
) -> ParticipantRegisterResponse:
    """Register a new participant with rate limiting, idempotency, and uniqueness validation."""
    settings = get_settings()
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

    res = ParticipantRegisterResponse(
        participant_id=participant.participant_id,
        name=participant.name,
        email=participant.email,
        team_name=participant.team_name,
        status=participant.status.value,
        access_key=raw_access_key,
        created_at=_utc(participant.created_at),
    )

    if idempotency_key is not None and payload_hash is not None:
        await _save_cached_registration(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            data=res.model_dump(mode="json"),
            ttl=300,
        )

    return res


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
