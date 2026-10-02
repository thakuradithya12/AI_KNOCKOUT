"""Transactional participant-owned submission, quarantine and validation services."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import (
    ArtifactState, ArtifactStateTransition, CompetitionEvent,
    Submission, SubmissionVersion, ValidationAttempt, ValidationOutcome,
)
from app.services.submission_validation import (
    PackageLimits, PackageRejected, POLICY_VERSION, VALIDATOR_VERSION,
    validate_submission_package,
)
from app.services.quarantine_storage import quarantine_storage


class StorageIntegrityError(RuntimeError):
    """A digest-addressed quarantine object does not match its immutable identity."""


@dataclass(frozen=True)
class UploadResult:
    version: SubmissionVersion
    validation: ValidationAttempt
    duplicate: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


async def accept_upload(
    db: AsyncSession,
    *,
    participant_id: str,
    submission_id: uuid.UUID,
    archive_bytes: bytes,
    correlation_id: str | None,
    limits: PackageLimits,
) -> UploadResult:
    # Lock the owner row to serialize version numbering on PostgreSQL.
    submission = await get_owned_submission(db, participant_id, submission_id, lock=True)
    if submission is None:
        raise LookupError("SUBMISSION_NOT_FOUND")

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

    started_at = _now()
    rejection: PackageRejected | None = None
    try:
        package = validate_submission_package(archive_bytes, limits)
        manifest: dict | None = package.manifest
    except PackageRejected as exc:
        rejection = exc
        manifest = _safe_manifest_snapshot(exc.manifest)

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
    version = SubmissionVersion(
        id=uuid.uuid4(),
        submission_id=submission_id,
        participant_id=participant_id,
        version=(last_version or 0) + 1,
        artifact_sha256=digest,
        artifact_size=len(archive_bytes),
        storage_key=storage_key,
        manifest=manifest,
        contract_version=_manifest_field(manifest, "contract_version"),
        protocol_version=_manifest_field(manifest, "protocol_version"),
        runtime_profile=_manifest_field(manifest, "runtime"),
        state=ArtifactState.QUARANTINED,
        rejection_code=None,
        created_at=_now(),
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
