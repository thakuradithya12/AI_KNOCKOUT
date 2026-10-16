"""Transactional participant-owned submission, quarantine and validation services."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import (
    ArtifactState,
    ArtifactStateTransition,
    CompetitionEvent,
    Submission,
    SubmissionVersion,
    ValidationAttempt,
    ValidationOutcome,
)
from app.models.event import (
    BuildSession,
    EventControl,
    ParticipantSubmissionAllowance,
    SubmissionAllowanceRequest,
)
from app.services.quarantine_storage import quarantine_storage
from app.services.submission_validation import (
    POLICY_VERSION,
    VALIDATOR_VERSION,
    PackageLimits,
    PackageRejected,
    validate_submission_package,
)

DEFAULT_SUBMISSION_LIMIT = 10


class SubmissionLimitExceeded(Exception):
    """Raised when participant has reached or exceeded their submission quota."""

    def __init__(self, limit: int, current: int) -> None:
        self.limit = limit
        self.current = current
        super().__init__(f"Submission quota of {limit} reached ({current} accepted).")


class StorageIntegrityError(RuntimeError):
    """A digest-addressed quarantine object does not match its immutable identity."""


@dataclass(frozen=True)
class UploadResult:
    version: SubmissionVersion
    validation: ValidationAttempt
    duplicate: bool


@dataclass(frozen=True)
class OrganizerApproval:
    """Explicit approval assertion from a trusted organizer/control-plane caller."""

    actor_id: str
    reason_code: str
    approval_reference: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.actor_id or len(self.actor_id) > 64:
            raise ValueError("ORGANIZER_ACTOR_INVALID")
        if not self.reason_code or len(self.reason_code) > 64:
            raise ValueError("ORGANIZER_APPROVAL_REASON_REQUIRED")
        if not self.approval_reference or len(self.approval_reference) > 64:
            raise ValueError("ORGANIZER_APPROVAL_REFERENCE_REQUIRED")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _manifest_field(manifest: dict | None, name: str) -> str | None:
    value = manifest.get(name) if manifest else None
    return value if type(value) is str else None


def _safe_manifest_snapshot(manifest: dict | None) -> dict | None:
    if manifest is None:
        return None
    try:
        serialized = json.dumps(manifest, ensure_ascii=False, allow_nan=False)
        serialized.encode("utf-8", errors="strict")
        if len(serialized.encode("utf-8")) > 16 * 1024:
            return None
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        return None
    return manifest


def _event(
    *, event_type: str, actor_kind: str, actor_id: str, aggregate_type: str,
    aggregate_id: uuid.UUID, sequence: int, metadata: dict, correlation_id: str | None,
) -> CompetitionEvent:
    return CompetitionEvent(
        event_type=event_type,
        actor_kind=actor_kind,
        actor_id=actor_id,
        severity="warning" if event_type.endswith("rejected") else "info",
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_sequence=sequence,
        schema_version=1,
        correlation_id=correlation_id,
        metadata_json=metadata,
        recorded_at=_now(),
    )


def _transition(
    *, version: SubmissionVersion, old: ArtifactState | None, new: ArtifactState,
    actor_kind: str, actor_id: str, reason: str, correlation_id: str | None,
) -> ArtifactStateTransition:
    return ArtifactStateTransition(
        submission_version_id=version.id,
        from_state=old.value if old else None,
        to_state=new,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason_code=reason,
        correlation_id=correlation_id,
        recorded_at=_now(),
    )


async def create_submission(db: AsyncSession, participant_id: str) -> Submission:
    submission = Submission(id=uuid.uuid4(), participant_id=participant_id, created_at=_now())
    db.add(submission)
    db.add(
        _event(
            event_type="submission.created", actor_kind="participant", actor_id=participant_id,
            aggregate_type="submission", aggregate_id=submission.id, sequence=1,
            metadata={"participant_id": participant_id}, correlation_id=None,
        )
    )
    await db.commit()
    await db.refresh(submission)
    return submission


async def get_owned_submission(
    db: AsyncSession, participant_id: str, submission_id: uuid.UUID, *, lock: bool = False
) -> Submission | None:
    stmt = select(Submission).where(
        Submission.id == submission_id, Submission.participant_id == participant_id
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def approve_submission_for_eligibility(
    db: AsyncSession,
    *,
    submission_version_id: uuid.UUID,
    approval: OrganizerApproval,
) -> SubmissionVersion:
    """Record an explicit organizer decision; never promotes automatically.

    This internal domain operation must only be called after the trusted
    organizer-authentication boundary has authorized ``approval.actor_id``.
    The existing immutable version/digest fields are only read and retained.
    """
    version = (
        await db.execute(
            select(SubmissionVersion)
            .where(SubmissionVersion.id == submission_version_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is None:
        raise LookupError("SUBMISSION_VERSION_NOT_FOUND")
    if version.state != ArtifactState.APPROVED:
        raise ValueError("SUBMISSION_VERSION_NOT_AWAITING_ORGANIZER_APPROVAL")

    old_state = version.state
    version.state = ArtifactState.ELIGIBLE
    db.add(_transition(
        version=version, old=old_state, new=ArtifactState.ELIGIBLE,
        actor_kind="organizer", actor_id=approval.actor_id,
        reason=approval.reason_code, correlation_id=approval.correlation_id,
    ))
    sequence = await db.scalar(
        select(func.max(CompetitionEvent.aggregate_sequence)).where(
            CompetitionEvent.aggregate_type == "submission_version",
            CompetitionEvent.aggregate_id == version.id,
        )
    )
    db.add(_event(
        event_type="submission.eligibility_approved", actor_kind="organizer",
        actor_id=approval.actor_id, aggregate_type="submission_version",
        aggregate_id=version.id, sequence=(sequence or 0) + 1,
        metadata={
            "from_state": ArtifactState.APPROVED.value,
            "to_state": ArtifactState.ELIGIBLE.value,
            "reason_code": approval.reason_code,
            "approval_reference": approval.approval_reference,
            "submission_version": version.version,
            "artifact_sha256": version.artifact_sha256,
        },
        correlation_id=approval.correlation_id,
    ))
    await db.commit()
    await db.refresh(version)
    return version


async def get_or_create_allowance(
    db: AsyncSession, participant_id: str, *, lock: bool = False
) -> ParticipantSubmissionAllowance:
    """Retrieve participant submission allowance, creating default record if missing."""
    try:
        stmt = select(ParticipantSubmissionAllowance).where(
            ParticipantSubmissionAllowance.participant_id == participant_id
        )
        if lock:
            stmt = stmt.with_for_update()
        allowance = (await db.execute(stmt)).scalar_one_or_none()
        if allowance is None:
            allowance = ParticipantSubmissionAllowance(
                participant_id=participant_id,
                submission_limit=DEFAULT_SUBMISSION_LIMIT,
            )
            db.add(allowance)
            await db.flush()
        return allowance
    except Exception:
        await db.rollback()
        return ParticipantSubmissionAllowance(
            participant_id=participant_id,
            submission_limit=DEFAULT_SUBMISSION_LIMIT,
        )


async def count_accepted_submissions(db: AsyncSession, participant_id: str) -> int:
    """Count successfully accepted submissions for a participant.
    
    Only versions that passed static validation and entered APPROVED, ELIGIBLE,
    or DEPRECATED states consume quota. REJECTED packages do not consume quota.
    """
    stmt = select(func.count(SubmissionVersion.id)).where(
        SubmissionVersion.participant_id == participant_id,
        SubmissionVersion.state.in_([
            ArtifactState.APPROVED,
            ArtifactState.ELIGIBLE,
            ArtifactState.DEPRECATED,
        ]),
    )
    return (await db.execute(stmt)).scalar_one() or 0


async def get_participant_quota_summary(
    db: AsyncSession, participant_id: str
) -> dict:
    """Summarize participant submission quota, usage, and allowance request status."""
    try:
        allowance = await get_or_create_allowance(db, participant_id)
        accepted_count = await count_accepted_submissions(db, participant_id)
        remaining = max(0, allowance.submission_limit - accepted_count)
        pending_req = None
        try:
            pending_req = await db.scalar(
                select(SubmissionAllowanceRequest).where(
                    SubmissionAllowanceRequest.participant_id == participant_id,
                    SubmissionAllowanceRequest.status == "PENDING",
                ).order_by(SubmissionAllowanceRequest.created_at.desc()).limit(1)
            )
        except Exception:
            await db.rollback()
        return {
            "participant_id": participant_id,
            "submission_limit": allowance.submission_limit,
            "accepted_submissions_count": accepted_count,
            "remaining_submissions": remaining,
            "has_pending_request": pending_req is not None,
            "granted_by": allowance.granted_by,
            "granted_at": allowance.granted_at,
            "reason": allowance.reason,
        }
    except Exception:
        await db.rollback()
        return {
            "participant_id": participant_id,
            "submission_limit": DEFAULT_SUBMISSION_LIMIT,
            "accepted_submissions_count": 0,
            "remaining_submissions": DEFAULT_SUBMISSION_LIMIT,
            "has_pending_request": False,
            "granted_by": None,
            "granted_at": None,
            "reason": None,
        }


async def update_participant_allowance(
    db: AsyncSession,
    *,
    participant_id: str,
    new_limit: int,
    actor_id: str,
    reason: str,
    correlation_id: str | None = None,
) -> ParticipantSubmissionAllowance:
    """Organizer directly adjusts a participant's submission allowance."""
    if new_limit < 1:
        raise ValueError("SUBMISSION_LIMIT_MUST_BE_POSITIVE")
    if not actor_id or len(actor_id) > 64:
        raise ValueError("ORGANIZER_ACTOR_INVALID")
    if not reason or len(reason.strip()) == 0:
        raise ValueError("ALLOWANCE_REASON_REQUIRED")
    if len(reason) > 255:
        raise ValueError("ALLOWANCE_REASON_TOO_LONG")

    allowance = await get_or_create_allowance(db, participant_id, lock=True)
    old_limit = allowance.submission_limit
    allowance.submission_limit = new_limit
    allowance.granted_by = actor_id
    allowance.granted_at = _now()
    allowance.reason = reason.strip()
    allowance.updated_at = _now()

    agg_id = uuid.uuid5(uuid.NAMESPACE_DNS, participant_id)
    seq = await db.scalar(
        select(func.max(CompetitionEvent.aggregate_sequence)).where(
            CompetitionEvent.aggregate_type == "participant_submission_allowance",
            CompetitionEvent.aggregate_id == agg_id,
        )
    )
    db.add(
        _event(
            event_type="submission_allowance.updated",
            actor_kind="organizer",
            actor_id=actor_id,
            aggregate_type="participant_submission_allowance",
            aggregate_id=agg_id,
            sequence=(seq or 0) + 1,
            metadata={
                "participant_id": participant_id,
                "previous_limit": old_limit,
                "new_limit": new_limit,
                "reason": reason.strip(),
            },
            correlation_id=correlation_id,
        )
    )
    await db.commit()
    await db.refresh(allowance)
    return allowance


async def create_allowance_request(
    db: AsyncSession,
    *,
    participant_id: str,
    requested_limit: int,
    reason: str,
    correlation_id: str | None = None,
) -> SubmissionAllowanceRequest:
    """Participant submits a request for increased submission allowance."""
    if requested_limit < 1:
        raise ValueError("REQUESTED_LIMIT_MUST_BE_POSITIVE")
    if not reason or len(reason.strip()) == 0:
        raise ValueError("REQUEST_REASON_REQUIRED")
    if len(reason) > 255:
        raise ValueError("REQUEST_REASON_TOO_LONG")

    allowance = await get_or_create_allowance(db, participant_id, lock=False)
    if requested_limit <= allowance.submission_limit:
        raise ValueError("REQUESTED_LIMIT_MUST_EXCEED_CURRENT_LIMIT")

    existing = (
        await db.execute(
            select(SubmissionAllowanceRequest).where(
                SubmissionAllowanceRequest.participant_id == participant_id,
                SubmissionAllowanceRequest.status == "PENDING",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError("PENDING_REQUEST_ALREADY_EXISTS")

    req = SubmissionAllowanceRequest(
        id=uuid.uuid4(),
        participant_id=participant_id,
        requested_limit=requested_limit,
        reason=reason.strip(),
        status="PENDING",
        created_at=_now(),
    )
    db.add(req)
    db.add(
        _event(
            event_type="submission_allowance.requested",
            actor_kind="participant",
            actor_id=participant_id,
            aggregate_type="submission_allowance_request",
            aggregate_id=req.id,
            sequence=1,
            metadata={
                "participant_id": participant_id,
                "requested_limit": requested_limit,
                "reason": reason.strip(),
            },
            correlation_id=correlation_id,
        )
    )
    await db.commit()
    await db.refresh(req)
    return req


async def decide_allowance_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    decision: str,
    actor_id: str,
    notes: str | None = None,
    granted_limit: int | None = None,
    correlation_id: str | None = None,
) -> SubmissionAllowanceRequest:
    """Organizer decides (approves/rejects) a participant's allowance request."""
    decision = decision.upper()
    if decision not in {"APPROVED", "REJECTED"}:
        raise ValueError("DECISION_MUST_BE_APPROVED_OR_REJECTED")
    if not actor_id or len(actor_id) > 64:
        raise ValueError("ORGANIZER_ACTOR_INVALID")

    req = (
        await db.execute(
            select(SubmissionAllowanceRequest)
            .where(SubmissionAllowanceRequest.id == request_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if req is None:
        raise LookupError("ALLOWANCE_REQUEST_NOT_FOUND")
    if req.status != "PENDING":
        raise ValueError("REQUEST_NOT_PENDING")

    req.status = decision
    req.reviewed_by = actor_id
    req.reviewed_at = _now()
    req.review_notes = notes[:255] if notes else None

    effective_limit = granted_limit or req.requested_limit
    if decision == "APPROVED":
        if effective_limit < 1:
            raise ValueError("GRANTED_LIMIT_MUST_BE_POSITIVE")
        allowance = await get_or_create_allowance(db, req.participant_id, lock=True)
        allowance.submission_limit = effective_limit
        allowance.granted_by = actor_id
        allowance.granted_at = _now()
        allowance.reason = f"Approved request {req.id}: {notes or req.reason}"[:255]
        allowance.updated_at = _now()

    seq = await db.scalar(
        select(func.max(CompetitionEvent.aggregate_sequence)).where(
            CompetitionEvent.aggregate_type == "submission_allowance_request",
            CompetitionEvent.aggregate_id == req.id,
        )
    )
    db.add(
        _event(
            event_type=f"submission_allowance.{decision.lower()}",
            actor_kind="organizer",
            actor_id=actor_id,
            aggregate_type="submission_allowance_request",
            aggregate_id=req.id,
            sequence=(seq or 0) + 1,
            metadata={
                "request_id": str(req.id),
                "participant_id": req.participant_id,
                "decision": decision,
                "granted_limit": effective_limit if decision == "APPROVED" else None,
                "notes": notes,
            },
            correlation_id=correlation_id,
        )
    )
    await db.commit()
    await db.refresh(req)
    return req


_upload_locks: dict[str, asyncio.Lock] = {}


def _get_upload_lock(participant_id: str) -> asyncio.Lock:
    if participant_id not in _upload_locks:
        _upload_locks[participant_id] = asyncio.Lock()
    return _upload_locks[participant_id]


async def accept_upload(
    db: AsyncSession,
    *,
    participant_id: str,
    submission_id: uuid.UUID,
    archive_bytes: bytes,
    correlation_id: str | None,
    limits: PackageLimits,
    bypass_freeze: bool = False,
    content_type: str | None = None,
) -> UploadResult:
    async with _get_upload_lock(participant_id):
        return await _accept_upload_impl(
            db,
            participant_id=participant_id,
            submission_id=submission_id,
            archive_bytes=archive_bytes,
            correlation_id=correlation_id,
            limits=limits,
            bypass_freeze=bypass_freeze,
            content_type=content_type,
        )


async def _accept_upload_impl(
    db: AsyncSession,
    *,
    participant_id: str,
    submission_id: uuid.UUID,
    archive_bytes: bytes,
    correlation_id: str | None,
    limits: PackageLimits,
    bypass_freeze: bool = False,
    content_type: str | None = None,
) -> UploadResult:
    # Lock the owner row to serialize version numbering on PostgreSQL.
    submission = await get_owned_submission(db, participant_id, submission_id, lock=True)
    if submission is None:
        raise LookupError("SUBMISSION_NOT_FOUND")

    control = await db.scalar(select(EventControl).where(EventControl.id == 1))
    if not bypass_freeze and control and control.official_freeze_at is not None:
        raise ValueError("OFFICIAL_SUBMISSIONS_FROZEN")

    build_session = await db.scalar(
        select(BuildSession)
        .where(BuildSession.participant_id == participant_id)
        .with_for_update()
    )
    if build_session is None:
        raise LookupError("BUILD_SESSION_NOT_FOUND")
    submitted_at = _now()

    from app.sandbox.participant_adapter import wrap_submission_if_needed

    rejection: PackageRejected | None = None
    try:
        archive_bytes = wrap_submission_if_needed(archive_bytes, limits, content_type=content_type)
    except PackageRejected as exc:
        rejection = exc

    digest = hashlib.sha256(archive_bytes).hexdigest()
    existing_version = (
        await db.execute(
            select(SubmissionVersion).where(
                SubmissionVersion.submission_id == submission_id,
                SubmissionVersion.participant_id == participant_id,
                SubmissionVersion.artifact_sha256 == digest,
            )
        )
    ).scalar_one_or_none()
    if existing_version is not None:
        try:
            stored_bytes = await quarantine_storage.verify(
                db, existing_version.storage_key, digest, existing_version.artifact_size
            )
        except ValueError as exc:
            raise StorageIntegrityError("quarantine object integrity check failed") from exc
        if stored_bytes != archive_bytes:
            raise StorageIntegrityError("digest collision or altered quarantine object")
        attempt = (
            await db.execute(
                select(ValidationAttempt)
                .where(ValidationAttempt.submission_version_id == existing_version.id)
                .order_by(ValidationAttempt.completed_at.desc())
                .limit(1)
            )
        ).scalar_one()
        return UploadResult(existing_version, attempt, True)

    # Enforce submission quota with row-level lock on participant allowance.
    allowance = await get_or_create_allowance(db, participant_id, lock=True)
    accepted_count = await count_accepted_submissions(db, participant_id)
    if accepted_count >= allowance.submission_limit:
        raise SubmissionLimitExceeded(
            limit=allowance.submission_limit,
            current=accepted_count,
        )

    started_at = _now()
    if rejection is None:
        try:
            package = validate_submission_package(archive_bytes, limits)
            manifest: dict | None = package.manifest
        except PackageRejected as exc:
            rejection = exc
            manifest = _safe_manifest_snapshot(exc.manifest)
    else:
        manifest = None

    # Quarantine bytes and all corresponding metadata/events commit atomically.
    # The logical storage key is backend-neutral; the current zero-budget adapter
    # maps it to this PostgreSQL row rather than a source-tree filesystem path.
    try:
        storage_key = await quarantine_storage.put(db, digest, archive_bytes)
    except ValueError as exc:
        raise StorageIntegrityError("quarantine object integrity check failed") from exc

    last_version = (
        await db.execute(
            select(func.max(SubmissionVersion.version)).where(
                SubmissionVersion.submission_id == submission_id
            )
        )
    ).scalar_one()
    build_duration_seconds = max(
        0, int((submitted_at - _utc(build_session.started_at)).total_seconds())
    )
    version = SubmissionVersion(
        id=uuid.uuid4(),
        submission_id=submission_id,
        participant_id=participant_id,
        version=(last_version or 0) + 1,
        artifact_sha256=digest,
        artifact_size=len(archive_bytes),
        build_duration_seconds=build_duration_seconds,
        storage_key=storage_key,
        manifest=manifest,
        contract_version=_manifest_field(manifest, "contract_version"),
        protocol_version=_manifest_field(manifest, "protocol_version"),
        runtime_profile=_manifest_field(manifest, "runtime"),
        state=ArtifactState.QUARANTINED,
        rejection_code=None,
        created_at=submitted_at,
    )
    db.add(version)
    await db.flush()

    transitions: list[ArtifactStateTransition] = [
        _transition(
            version=version, old=None, new=ArtifactState.QUARANTINED,
            actor_kind="participant", actor_id=participant_id,
            reason="UPLOAD_DURABLY_QUARANTINED", correlation_id=correlation_id,
        )
    ]
    events: list[CompetitionEvent] = [
        _event(
            event_type="submission.quarantined", actor_kind="participant", actor_id=participant_id,
            aggregate_type="submission_version", aggregate_id=version.id, sequence=1,
            metadata={"submission_id": str(submission_id), "version": version.version,
                      "artifact_sha256": digest, "artifact_size": len(archive_bytes)},
            correlation_id=correlation_id,
        ),
    ]
    current = ArtifactState.QUARANTINED
    transitions.append(
        _transition(
            version=version, old=current, new=ArtifactState.INSPECTING,
            actor_kind="validator", actor_id=VALIDATOR_VERSION,
            reason="CANONICAL_ARCHIVE_INSPECTION_STARTED", correlation_id=correlation_id,
        )
    )
    current = ArtifactState.INSPECTING
    if rejection is None or rejection.stage == "VALIDATING":
        transitions.append(
            _transition(
                version=version, old=current, new=ArtifactState.VALIDATING,
                actor_kind="validator", actor_id=VALIDATOR_VERSION,
                reason="MANIFEST_VALIDATION_STARTED", correlation_id=correlation_id,
            )
        )
        current = ArtifactState.VALIDATING

    final_state = ArtifactState.REJECTED if rejection else ArtifactState.APPROVED
    reason_code = rejection.code if rejection else "STATIC_VALIDATION_PASSED"
    transitions.append(
        _transition(
            version=version, old=current, new=final_state,
            actor_kind="validator", actor_id=VALIDATOR_VERSION,
            reason=reason_code, correlation_id=correlation_id,
        )
    )
    validation_event = _event(
        event_type="validation.rejected" if rejection else "validation.passed",
        actor_kind="validator", actor_id=VALIDATOR_VERSION,
        aggregate_type="submission_version", aggregate_id=version.id,
        sequence=0, metadata={"state": final_state.value, "reason_code": reason_code,
                              "validator_version": VALIDATOR_VERSION, "policy_version": POLICY_VERSION},
        correlation_id=correlation_id,
    )
    # Mirror every durable state transition in the outbox. The outbox sequence
    # is contiguous and independent from transition-row identifiers.
    for transition in transitions[1:]:
        events.append(
            _event(
                event_type="submission.state_changed",
                actor_kind=transition.actor_kind,
                actor_id=transition.actor_id,
                aggregate_type="submission_version",
                aggregate_id=version.id,
                sequence=len(events) + 1,
                metadata={
                    "from_state": transition.from_state,
                    "to_state": transition.to_state.value,
                    "reason_code": transition.reason_code,
                },
                correlation_id=correlation_id,
            )
        )
        if transition.to_state == ArtifactState.INSPECTING:
            events.append(
                _event(
                    event_type="validation.started",
                    actor_kind="validator",
                    actor_id=VALIDATOR_VERSION,
                    aggregate_type="submission_version",
                    aggregate_id=version.id,
                    sequence=len(events) + 1,
                    metadata={"validator_version": VALIDATOR_VERSION, "policy_version": POLICY_VERSION},
                    correlation_id=correlation_id,
                )
            )
    validation_event.aggregate_sequence = len(events) + 1
    events.append(validation_event)
    attempt = ValidationAttempt(
        id=uuid.uuid4(),
        submission_version_id=version.id,
        validator_version=VALIDATOR_VERSION,
        policy_version=POLICY_VERSION,
        started_at=started_at,
        completed_at=_now(),
        outcome=ValidationOutcome.REJECTED if rejection else ValidationOutcome.PASSED,
        report={"code": reason_code, "stage": rejection.stage if rejection else "COMPLETE"},
        rejection_code=rejection.code if rejection else None,
    )

    # Record and enforce each state transition in order in this transaction.
    db.add(transitions[0])
    await db.flush()
    for transition in transitions[1:]:
        db.add(transition)
        await db.flush()
        version.state = transition.to_state
        if transition.to_state == ArtifactState.REJECTED:
            version.rejection_code = rejection.code if rejection else None
        await db.flush()
    db.add_all([*events, attempt])
    await db.commit()
    await db.refresh(version)
    await db.refresh(attempt)
    return UploadResult(version=version, validation=attempt, duplicate=False)
