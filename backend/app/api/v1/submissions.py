"""Authenticated participant submission intake and metadata endpoints."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_participant, get_db
from app.core.config import get_settings
from app.models.competition import Submission, SubmissionVersion, ValidationAttempt
from app.models.event import SubmissionAllowanceRequest
from app.models.participant import Participant
from app.services.submission_validation import PackageLimits
from app.services.submissions import (
    StorageIntegrityError,
    SubmissionLimitExceeded,
    accept_upload,
    create_allowance_request,
    create_submission,
    get_owned_submission,
    get_participant_quota_summary,
)

logger = logging.getLogger("ai-knockout.submissions")
router = APIRouter(prefix="/submissions", tags=["submissions"])


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _version_json(version: SubmissionVersion, *, duplicate: bool = False) -> dict:
    return {
        "id": str(version.id),
        "submission_id": str(version.submission_id),
        "version": version.version,
        "participant_id": version.participant_id,
        "artifact_sha256": version.artifact_sha256,
        "artifact_size": version.artifact_size,
        "submitted_at": _utc(version.created_at),
        "build_duration_seconds": version.build_duration_seconds,
        "manifest": version.manifest,
        "contract_version": version.contract_version,
        "protocol_version": version.protocol_version,
        "runtime_profile": version.runtime_profile,
        "state": version.state.value,
        "rejection_code": version.rejection_code,
        "created_at": _utc(version.created_at),
        "duplicate": duplicate,
    }


def _not_found(code: str = "SUBMISSION_NOT_FOUND") -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": code})


async def _owned_or_none(
    db: AsyncSession, participant_id: str, submission_id: uuid.UUID
) -> Submission | None:
    return await get_owned_submission(db, participant_id, submission_id)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a participant submission")
async def post_submission(
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    submission = await create_submission(db, participant.participant_id)
    return {
        "submission_id": str(submission.id),
        "participant_id": submission.participant_id,
        "created_at": submission.created_at,
    }


@router.get("", summary="List the authenticated participant's submissions")
async def list_submissions(
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(Submission)
            .where(Submission.participant_id == participant.participant_id)
            .order_by(Submission.created_at.desc())
        )
    ).scalars().all()
    return {
        "items": [
            {"submission_id": str(row.id), "created_at": row.created_at}
            for row in rows
        ]
    }


class AllowanceRequestCreate(BaseModel):
    requested_limit: int
    reason: str


@router.get("/quota", summary="Get the authenticated participant's submission quota and usage")
async def get_my_quota(
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await get_participant_quota_summary(db, participant.participant_id)


@router.post(
    "/allowance-requests",
    status_code=status.HTTP_201_CREATED,
    summary="Request an increase to submission quota",
)
async def request_allowance_increase(
    payload: AllowanceRequestCreate,
    request: Request,
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    try:
        req = await create_allowance_request(
            db,
            participant_id=participant.participant_id,
            requested_limit=payload.requested_limit,
            reason=payload.reason,
            correlation_id=request_id,
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=jsonable_encoder({
                "id": str(req.id),
                "participant_id": req.participant_id,
                "requested_limit": req.requested_limit,
                "reason": req.reason,
                "status": req.status,
                "created_at": req.created_at,
            }),
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": str(exc)},
        )


@router.get("/allowance-requests", summary="List participant's allowance requests")
async def list_my_allowance_requests(
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        rows = (
            await db.execute(
                select(SubmissionAllowanceRequest)
                .where(SubmissionAllowanceRequest.participant_id == participant.participant_id)
                .order_by(SubmissionAllowanceRequest.created_at.desc())
            )
        ).scalars().all()
        return {
            "items": [
                {
                    "id": str(r.id),
                    "requested_limit": r.requested_limit,
                    "reason": r.reason,
                    "status": r.status,
                    "created_at": r.created_at,
                    "reviewed_by": r.reviewed_by,
                    "reviewed_at": r.reviewed_at,
                    "review_notes": r.review_notes,
                }
                for r in rows
            ]
        }
    except Exception:
        await db.rollback()
        return {"items": []}


@router.get("/{submission_id}", summary="Get an owned submission", response_model=None)
async def get_submission(
    submission_id: uuid.UUID,
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> Any:
    submission = await _owned_or_none(db, participant.participant_id, submission_id)
    if submission is None:
        return _not_found()
    versions = (
        await db.execute(
            select(SubmissionVersion)
            .where(SubmissionVersion.submission_id == submission_id)
            .order_by(SubmissionVersion.version.desc())
        )
    ).scalars().all()
    return {
        "submission_id": str(submission.id),
        "participant_id": submission.participant_id,
        "created_at": submission.created_at,
        "latest_version": _version_json(versions[0]) if versions else None,
    }


@router.post(
    "/{submission_id}/versions",
    summary="Upload a canonical uncompressed USTAR package",
    response_model=None,
    responses={
        201: {"description": "Quarantined artifact statically validated"},
        200: {"description": "Identical digest already exists for this submission"},
        413: {"description": "Configured raw upload limit exceeded before persistence"},
        422: {"description": "Artifact bytes quarantined and permanently rejected"},
    },
)
async def upload_version(
    submission_id: uuid.UUID,
    request: Request,
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    settings = get_settings()
    if await _owned_or_none(db, participant.participant_id, submission_id) is None:
        return _not_found()

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    allowed_types = {
        "application/x-tar",
        "application/octet-stream",
        "text/x-python",
        "text/plain",
        "text/x-script.python",
    }
    if content_type not in allowed_types:
        return JSONResponse(
            status_code=415,
            content={"error": "UNSUPPORTED_MEDIA_TYPE", "detail": "Upload a raw uncompressed USTAR archive or a Python intelligence.py file."},
        )
    length_header = request.headers.get("content-length")
    if length_header is not None:
        try:
            declared_length = int(length_header)
            if declared_length < 0:
                raise ValueError
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "CONTENT_LENGTH_INVALID"})
        if declared_length > settings.submission_max_archive_bytes:
            return JSONResponse(
                status_code=413,
                content={"error": "PACKAGE_TOO_LARGE", "detail": "Configured raw upload limit exceeded."},
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > settings.submission_max_archive_bytes:
            return JSONResponse(
                status_code=413,
                content={"error": "PACKAGE_TOO_LARGE", "detail": "Configured raw upload limit exceeded."},
            )
        body.extend(chunk)

    request_id = getattr(request.state, "request_id", None)
    limits = PackageLimits(
        max_archive_bytes=settings.submission_max_archive_bytes,
        max_members=settings.submission_max_members,
        max_member_bytes=settings.submission_max_member_bytes,
        max_total_bytes=settings.submission_max_total_bytes,
        max_manifest_bytes=settings.submission_max_manifest_bytes,
    )
    try:
        result = await accept_upload(
            db,
            participant_id=participant.participant_id,
            submission_id=submission_id,
            archive_bytes=bytes(body),
            correlation_id=request_id,
            limits=limits,
            content_type=content_type,
        )
    except LookupError:
        return _not_found()
    except SubmissionLimitExceeded as exc:
        return JSONResponse(
            status_code=403,
            content={
                "error": "SUBMISSION_LIMIT_EXCEEDED",
                "detail": f"Submission quota exceeded. Limit is {exc.limit}, current accepted count is {exc.current}.",
                "limit": exc.limit,
                "current": exc.current,
                "accepted": exc.current,
            },
        )
    except ValueError as exc:
        if str(exc) == "OFFICIAL_SUBMISSIONS_FROZEN":
            return JSONResponse(
                status_code=409,
                content={
                    "error": "OFFICIAL_SUBMISSIONS_FROZEN",
                    "detail": "Official tournament submissions are frozen. No new packages may be submitted.",
                },
            )
        await db.rollback()
        raise
    except StorageIntegrityError:
        logger.error("Quarantine object failed digest verification", extra={"request_id": request_id})
        return JSONResponse(status_code=503, content={"error": "ARTIFACT_STORAGE_INTEGRITY_FAILURE"})
    except Exception:
        await db.rollback()
        logger.exception("Submission intake persistence failed", extra={"request_id": request_id})
        return JSONResponse(status_code=503, content={"error": "SUBMISSION_STORAGE_UNAVAILABLE"})

    payload = {
        "version": _version_json(result.version, duplicate=result.duplicate),
        "validation": {
            "attempt_id": str(result.validation.id),
            "outcome": result.validation.outcome.value,
            "validator_version": result.validation.validator_version,
            "policy_version": result.validation.policy_version,
            "started_at": result.validation.started_at,
            "completed_at": result.validation.completed_at,
            "report": result.validation.report,
        },
    }
    if result.version.state.value == "REJECTED":
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"error": result.version.rejection_code, **payload}),
        )
    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content=jsonable_encoder(payload),
    )


@router.get("/{submission_id}/versions", summary="List version metadata", response_model=None)
async def list_versions(
    submission_id: uuid.UUID,
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> Any:
    if await _owned_or_none(db, participant.participant_id, submission_id) is None:
        return _not_found()
    rows = (
        await db.execute(
            select(SubmissionVersion)
            .where(SubmissionVersion.submission_id == submission_id)
            .order_by(SubmissionVersion.version.desc())
        )
    ).scalars().all()
    return {"items": [_version_json(row) for row in rows]}


async def _owned_version(
    db: AsyncSession, participant_id: str, submission_id: uuid.UUID, version_number: int
) -> SubmissionVersion | None:
    if await _owned_or_none(db, participant_id, submission_id) is None:
        return None
    return (
        await db.execute(
            select(SubmissionVersion).where(
                SubmissionVersion.submission_id == submission_id,
                SubmissionVersion.participant_id == participant_id,
                SubmissionVersion.version == version_number,
            )
        )
    ).scalar_one_or_none()


@router.get("/{submission_id}/versions/{version_number}", summary="Get version metadata", response_model=None)
async def get_version(
    submission_id: uuid.UUID,
    version_number: int,
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> Any:
    version = await _owned_version(db, participant.participant_id, submission_id, version_number)
    if version is None:
        return _not_found("SUBMISSION_VERSION_NOT_FOUND")
    return _version_json(version)


@router.get(
    "/{submission_id}/versions/{version_number}/validation",
    summary="Get append-only static validation results",
    response_model=None,
)
async def get_validation_results(
    submission_id: uuid.UUID,
    version_number: int,
    participant: Participant = Depends(get_current_active_participant),
    db: AsyncSession = Depends(get_db),
) -> Any:
    version = await _owned_version(db, participant.participant_id, submission_id, version_number)
    if version is None:
        return _not_found("SUBMISSION_VERSION_NOT_FOUND")
    rows = (
        await db.execute(
            select(ValidationAttempt)
            .where(ValidationAttempt.submission_version_id == version.id)
            .order_by(ValidationAttempt.completed_at.asc())
        )
    ).scalars().all()
    return {
        "submission_id": str(submission_id),
        "version": version_number,
        "state": version.state.value,
        "attempts": [
            {
                "attempt_id": str(row.id),
                "validator_version": row.validator_version,
                "policy_version": row.policy_version,
                "started_at": row.started_at,
                "completed_at": row.completed_at,
                "outcome": row.outcome.value,
                "report": row.report,
                "rejection_code": row.rejection_code,
            }
            for row in rows
        ],
    }

