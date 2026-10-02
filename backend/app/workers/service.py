"""Durable worker protocol operations. No participant process is launched here."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import ArtifactState, QuarantineObject, SubmissionVersion
from app.models.worker import (
    ExecutionFailureClass,
    ExecutionJobStatus,
    Worker,
    WorkerArtifactCapability,
    WorkerEnrollmentToken,
    WorkerEvidence,
    WorkerExecutionJob,
    WorkerSecurityEvent,
    WorkerStatus,
)
from app.services.quarantine_storage import quarantine_storage
from app.workers.schemas import ResourcePolicy
from app.workers.tls import VerifiedWorkerCertificate

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10
ENROLLMENT_SECONDS = 300
CAPABILITY_SECONDS = 30
MAX_INITIAL_STATE_BYTES = 16 * 1024


class WorkerProtocolError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normal(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit(
    *, event_type: str, actor_id: str, worker_id: str | None = None,
    execution_id: uuid.UUID | None = None, details: dict | None = None, severity: str = "INFO",
) -> WorkerSecurityEvent:
    return WorkerSecurityEvent(
        worker_id=worker_id,
        execution_id=execution_id,
        event_type=event_type,
        severity=severity,
        actor_id=actor_id,
        details=details or {},
        recorded_at=_now(),
    )


async def issue_enrollment_token(
    db: AsyncSession, *, worker_id: str, approved_by: str
) -> tuple[Worker, str]:
    """Operator-only service operation; there is deliberately no public admin route."""
    valid_worker_id = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", worker_id or "")
    if not valid_worker_id or not approved_by or len(approved_by) > 128:
        raise WorkerProtocolError(422, "ENROLLMENT_REQUEST_INVALID")
    existing = await db.get(Worker, worker_id)
    if existing is not None and existing.status not in {
        WorkerStatus.PENDING, WorkerStatus.SUSPENDED
    }:
        raise WorkerProtocolError(409, "WORKER_ID_UNAVAILABLE")
    worker = existing or Worker(
        worker_id=worker_id,
        status=WorkerStatus.PENDING,
        capabilities={},
        security_state="PENDING_APPROVAL",
    )
    if existing is None:
        db.add(worker)
    raw_token = secrets.token_urlsafe(32)
    db.add(
        WorkerEnrollmentToken(
            worker_id=worker_id,
            token_hash=_hash_token(raw_token),
            approved_by=approved_by,
            purpose="ENROLL",
            expires_at=_now() + timedelta(seconds=ENROLLMENT_SECONDS),
        )
    )
    db.add(_audit(
        event_type="WORKER_ENROLLMENT_TOKEN_ISSUED", actor_id=approved_by,
        worker_id=worker_id, details={"expires_in_seconds": ENROLLMENT_SECONDS},
    ))
    await db.commit()
    return worker, raw_token


async def issue_certificate_rotation_token(
    db: AsyncSession, *, worker_id: str, approved_by: str
) -> tuple[Worker, str]:
    """Operator-approved one-time token; existing identity stays active until activation."""
    if not approved_by or len(approved_by) > 128:
        raise WorkerProtocolError(422, "ROTATION_REQUEST_INVALID")
    worker = await db.get(Worker, worker_id, with_for_update=True, populate_existing=True)
    if worker is None or worker.status != WorkerStatus.ACTIVE or not worker.certificate_fingerprint:
        raise WorkerProtocolError(409, "WORKER_NOT_ROTATABLE")
    if worker.pending_certificate_fingerprint is not None:
        raise WorkerProtocolError(409, "WORKER_ROTATION_ALREADY_PENDING")
    raw_token = secrets.token_urlsafe(32)
    db.add(WorkerEnrollmentToken(
        worker_id=worker_id, token_hash=_hash_token(raw_token), approved_by=approved_by,
        purpose="ROTATE", expires_at=_now() + timedelta(seconds=ENROLLMENT_SECONDS),
    ))
    db.add(_audit(
        event_type="WORKER_CERTIFICATE_ROTATION_APPROVED", actor_id=approved_by,
        worker_id=worker_id, details={"expires_in_seconds": ENROLLMENT_SECONDS},
    ))
    await db.commit()
    return worker, raw_token


async def enroll_worker(
    db: AsyncSession,
    *,
    worker_id: str,
    raw_token: str,
    certificate: VerifiedWorkerCertificate,
    capabilities: dict,
    software_version: str,
    runtime_version: str,
) -> Worker:
    token = (
        await db.execute(
            select(WorkerEnrollmentToken)
            .where(WorkerEnrollmentToken.token_hash == _hash_token(raw_token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = _now()
    if (
        token is None
        or token.worker_id != worker_id
        or token.used_at is not None
        or token.revoked_at is not None
        or _normal(token.expires_at) <= now
    ):
        raise WorkerProtocolError(401, "ENROLLMENT_TOKEN_INVALID")
    worker = await db.get(Worker, worker_id, with_for_update=True, populate_existing=True)
    if worker is None:
        raise WorkerProtocolError(409, "WORKER_NOT_ENROLLABLE")
    if certificate.worker_id != worker_id:
        raise WorkerProtocolError(409, "CERTIFICATE_WORKER_ID_MISMATCH")
    duplicate = (
        await db.execute(
            select(Worker.worker_id).where(
                (Worker.certificate_fingerprint == certificate.fingerprint)
                | (Worker.pending_certificate_fingerprint == certificate.fingerprint)
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None and duplicate != worker_id:
        raise WorkerProtocolError(409, "CERTIFICATE_ALREADY_BOUND")
    if token.purpose == "ENROLL" and worker.status not in {
        WorkerStatus.PENDING, WorkerStatus.SUSPENDED
    }:
        raise WorkerProtocolError(409, "WORKER_NOT_ENROLLABLE")
    if token.purpose == "ROTATE" and (
        worker.status != WorkerStatus.ACTIVE
        or worker.certificate_fingerprint is None
        or worker.certificate_fingerprint == certificate.fingerprint
        or worker.pending_certificate_fingerprint is not None
    ):
        raise WorkerProtocolError(409, "WORKER_NOT_ROTATABLE")
    if _normal(certificate.not_after) <= now or _normal(certificate.not_before) > now:
        raise WorkerProtocolError(401, "WORKER_CERTIFICATE_INVALID")

    token.used_at = now
    worker.pending_certificate_fingerprint = certificate.fingerprint
    worker.pending_certificate_identity = certificate.subject
    worker.pending_certificate_serial = certificate.serial
    worker.pending_issued_at = certificate.not_before
    worker.pending_expires_at = certificate.not_after
    worker.pending_capabilities = capabilities
    worker.pending_software_version = software_version
    worker.pending_runtime_version = runtime_version
    db.add(_audit(
        event_type="WORKER_CERTIFICATE_ENROLLED_PENDING_ACTIVATION", actor_id=token.approved_by,
        worker_id=worker_id,
        details={"certificate_fingerprint": certificate.fingerprint, "serial": certificate.serial},
    ))
    await db.commit()
    await db.refresh(worker)
    return worker


async def activate_worker(
    db: AsyncSession, *, worker_id: str, operator_id: str
) -> Worker:
    """Explicitly activate initial enrollment or atomically switch a rotated certificate."""
    if not operator_id or len(operator_id) > 128:
        raise WorkerProtocolError(422, "OPERATOR_ID_INVALID")
    worker = await db.get(Worker, worker_id, with_for_update=True, populate_existing=True)
    if worker is None:
        raise WorkerProtocolError(404, "WORKER_NOT_FOUND")
    if worker.status not in {WorkerStatus.PENDING, WorkerStatus.SUSPENDED, WorkerStatus.ACTIVE}:
        raise WorkerProtocolError(409, "WORKER_NOT_ACTIVATABLE")
    if not worker.pending_certificate_fingerprint or not worker.pending_expires_at:
        raise WorkerProtocolError(409, "WORKER_CERTIFICATE_NOT_PENDING")
    now = _now()
    if _normal(worker.pending_expires_at) <= now:
        raise WorkerProtocolError(409, "WORKER_PENDING_CERTIFICATE_EXPIRED")
    old_fingerprint = worker.certificate_fingerprint
    worker.certificate_fingerprint = worker.pending_certificate_fingerprint
    worker.certificate_identity = worker.pending_certificate_identity
    worker.certificate_serial = worker.pending_certificate_serial
    worker.issued_at = worker.pending_issued_at
    worker.expires_at = worker.pending_expires_at
    worker.capabilities = worker.pending_capabilities
    worker.software_version = worker.pending_software_version
    worker.runtime_version = worker.pending_runtime_version
    worker.pending_certificate_fingerprint = None
    worker.pending_certificate_identity = None
    worker.pending_certificate_serial = None
    worker.pending_issued_at = None
    worker.pending_expires_at = None
    worker.pending_capabilities = {}
    worker.pending_software_version = None
    worker.pending_runtime_version = None
    worker.revoked_at = None
    worker.status = WorkerStatus.ACTIVE
    worker.security_state = "UNREVIEWED"
    db.add(_audit(
        event_type="WORKER_CERTIFICATE_ROTATED" if old_fingerprint else "WORKER_ACTIVATED",
        actor_id=operator_id, worker_id=worker_id,
        details={"previous_fingerprint": old_fingerprint,
                 "active_fingerprint": worker.certificate_fingerprint},
        severity="WARNING" if old_fingerprint else "INFO",
    ))
    await db.commit()
    await db.refresh(worker)
    return worker


async def authenticate_worker(
    db: AsyncSession, certificate: VerifiedWorkerCertificate | None
) -> Worker:
    if certificate is None:
        raise WorkerProtocolError(401, "WORKER_MTLS_REQUIRED")
    worker = (
        await db.execute(
            select(Worker)
            .where(Worker.certificate_fingerprint == certificate.fingerprint)
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = _now()
    if worker is None:
        raise WorkerProtocolError(401, "WORKER_CERTIFICATE_UNKNOWN")
    if worker.status != WorkerStatus.ACTIVE or worker.revoked_at is not None:
        raise WorkerProtocolError(403, "WORKER_NOT_ACTIVE")
    if worker.expires_at is None or _normal(worker.expires_at) <= now:
        raise WorkerProtocolError(401, "WORKER_CERTIFICATE_EXPIRED")
    if (
        worker.certificate_fingerprint != certificate.fingerprint
        or worker.worker_id != certificate.worker_id
    ):
        raise WorkerProtocolError(401, "WORKER_IDENTITY_MISMATCH")
    return worker


async def heartbeat(
    db: AsyncSession, *, worker: Worker, capabilities: dict,
    software_version: str, runtime_version: str, security_state: str,
) -> datetime:
    worker.last_heartbeat_at = _now()
    worker.capabilities = capabilities
    worker.software_version = software_version
    worker.runtime_version = runtime_version
    worker.security_state = security_state
    db.add(_audit(
        event_type="WORKER_HEARTBEAT", actor_id=worker.worker_id,
        worker_id=worker.worker_id, details={"security_state": security_state},
    ))
    await db.commit()
    return worker.last_heartbeat_at


async def revoke_worker(db: AsyncSession, *, worker_id: str, operator_id: str) -> None:
    worker = await db.get(Worker, worker_id, with_for_update=True, populate_existing=True)
    if worker is None:
        raise WorkerProtocolError(404, "WORKER_NOT_FOUND")
    now = _now()
    worker.status = WorkerStatus.REVOKED
    worker.revoked_at = now
    worker.security_state = "REVOKED"
    await db.execute(
        update(WorkerEnrollmentToken)
        .where(WorkerEnrollmentToken.worker_id == worker_id,
               WorkerEnrollmentToken.used_at.is_(None), WorkerEnrollmentToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    active = (
        await db.execute(
            select(WorkerExecutionJob)
            .where(
                WorkerExecutionJob.assigned_worker_id == worker_id,
                WorkerExecutionJob.status.in_([
                    ExecutionJobStatus.LEASED, ExecutionJobStatus.ACKNOWLEDGED
                ]),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalars().all()
    for job in active:
        job.status = ExecutionJobStatus.QUEUED
        job.assigned_worker_id = None
        job.lease_id = None
        job.lease_expires_at = now
        job.fencing_token += 1
    caps = (
        await db.execute(
            select(WorkerArtifactCapability).where(
                WorkerArtifactCapability.worker_id == worker_id,
                WorkerArtifactCapability.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    for cap in caps:
        cap.revoked_at = now
    db.add(_audit(
        event_type="WORKER_REVOKED", actor_id=operator_id, worker_id=worker_id,
        details={"invalidated_leases": len(active)}, severity="WARNING",
    ))
    await db.commit()


async def create_execution_job(
    db: AsyncSession, *, submission_version_id: uuid.UUID, initial_state: dict,
    resource_policy: ResourcePolicy, execution_expires_at: datetime,
    match_id: uuid.UUID | None = None,
) -> WorkerExecutionJob:
    """Trusted control-plane call only; no public participant route creates jobs."""
    version = await db.get(SubmissionVersion, submission_version_id)
    if version is None or version.state != ArtifactState.ELIGIBLE:
        raise WorkerProtocolError(409, "ARTIFACT_NOT_ELIGIBLE")
    try:
        payload = json.dumps(initial_state, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise WorkerProtocolError(422, "INITIAL_STATE_INVALID") from exc
    if len(payload) > MAX_INITIAL_STATE_BYTES:
        raise WorkerProtocolError(422, "INITIAL_STATE_TOO_LARGE")
    expires = _normal(execution_expires_at)
    if expires <= _now():
        raise WorkerProtocolError(422, "EXECUTION_EXPIRY_INVALID")
    job = WorkerExecutionJob(
        execution_id=uuid.uuid4(),
        match_id=match_id,
        participant_id=version.participant_id,
        submission_id=version.submission_id,
        submission_version=version.version,
        submission_version_id=version.id,
        artifact_sha256=version.artifact_sha256,
        immutable_artifact_ref=version.storage_key,
        runtime_profile=version.runtime_profile or "python-3.12",
        protocol_version=version.protocol_version or "aik-jsonl/1",
        execution_mode="MOCK_PROTOCOL_ONLY",
        initial_state=initial_state,
        resource_policy=resource_policy.model_dump(mode="json"),
        execution_expires_at=expires,
        status=ExecutionJobStatus.QUEUED,
        fencing_token=0,
        last_sequence=0,
    )
    db.add(job)
    db.add(_audit(
        event_type="WORKER_EXECUTION_QUEUED", actor_id="control-plane",
        execution_id=job.execution_id,
        details={"artifact_sha256": job.artifact_sha256, "mode": "PROTOCOL_MOCK_ONLY"},
    ))
    await db.commit()
    await db.refresh(job)
    return job


def _lease_valid(job: WorkerExecutionJob, worker: Worker, lease_id: uuid.UUID, fence: int) -> None:
    now = _now()
    if job.assigned_worker_id != worker.worker_id or job.lease_id != lease_id:
        raise WorkerProtocolError(409, "EXECUTION_LEASE_MISMATCH")
    if job.fencing_token != fence:
        raise WorkerProtocolError(409, "STALE_FENCING_TOKEN")
    if job.lease_expires_at is None or _normal(job.lease_expires_at) <= now:
        raise WorkerProtocolError(409, "EXECUTION_LEASE_EXPIRED")
    if _normal(job.execution_expires_at) <= now:
        raise WorkerProtocolError(409, "EXECUTION_EXPIRED")
    if worker.status != WorkerStatus.ACTIVE or worker.revoked_at is not None:
        raise WorkerProtocolError(403, "WORKER_NOT_ACTIVE")


def _next_sequence(job: WorkerExecutionJob, sequence: int, operation: dict) -> None:
    operation_hash = hashlib.sha256(
        json.dumps(operation, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    if sequence <= job.last_sequence:
        if sequence == job.last_sequence and operation_hash == job.last_operation_hash:
            raise WorkerProtocolError(409, "DUPLICATE_OPERATION_ALREADY_ACCEPTED")
        raise WorkerProtocolError(409, "STALE_WORKER_SEQUENCE")
    if sequence != job.last_sequence + 1:
        raise WorkerProtocolError(409, "WORKER_SEQUENCE_GAP")
    job.last_sequence = sequence
    job.last_operation_hash = operation_hash


async def claim_job(
    db: AsyncSession, *, worker: Worker
) -> WorkerExecutionJob | None:
    now = _now()
    candidate = (
        await db.execute(
            select(WorkerExecutionJob)
            .where(
                WorkerExecutionJob.execution_expires_at > now,
                (
                    (WorkerExecutionJob.status == ExecutionJobStatus.QUEUED)
                    | (
                        WorkerExecutionJob.status.in_([
                            ExecutionJobStatus.LEASED, ExecutionJobStatus.ACKNOWLEDGED
                        ])
                        & (WorkerExecutionJob.lease_expires_at < now)
                    )
                ),
            )
            .order_by(WorkerExecutionJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if candidate is None:
        await db.commit()
        return None
    candidate.status = ExecutionJobStatus.LEASED
    candidate.assigned_worker_id = worker.worker_id
    candidate.lease_id = uuid.uuid4()
    candidate.fencing_token += 1
    candidate.lease_expires_at = min(
        _normal(candidate.execution_expires_at), now + timedelta(seconds=LEASE_SECONDS)
    )
    db.add(_audit(
        event_type="WORKER_JOB_LEASED", actor_id=worker.worker_id,
        worker_id=worker.worker_id, execution_id=candidate.execution_id,
        details={"fencing_token": candidate.fencing_token},
    ))
    await db.commit()
    await db.refresh(candidate)
    return candidate


async def _locked_job(
    db: AsyncSession, *, worker: Worker, execution_id: uuid.UUID,
    lease_id: uuid.UUID, fencing_token: int,
) -> WorkerExecutionJob:
    job = (
        await db.execute(
            select(WorkerExecutionJob)
            .where(WorkerExecutionJob.execution_id == execution_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        raise WorkerProtocolError(404, "EXECUTION_NOT_FOUND")
    _lease_valid(job, worker, lease_id, fencing_token)
    return job


async def acknowledge_job(
    db: AsyncSession, *, worker: Worker, execution_id: uuid.UUID,
    lease_id: uuid.UUID, fencing_token: int, sequence: int, accepted: bool,
) -> WorkerExecutionJob:
    job = await _locked_job(
        db, worker=worker, execution_id=execution_id, lease_id=lease_id,
        fencing_token=fencing_token,
    )
    _next_sequence(job, sequence, {"op": "ack", "accepted": accepted})
    if job.status != ExecutionJobStatus.LEASED:
        raise WorkerProtocolError(409, "JOB_NOT_AWAITING_ACK")
    job.status = ExecutionJobStatus.ACKNOWLEDGED if accepted else ExecutionJobStatus.FAILED
    if not accepted:
        db.add(WorkerEvidence(
            execution_id=execution_id, worker_id=worker.worker_id, sequence=sequence,
            event_type="FAILED", failure_class=ExecutionFailureClass.WORKER_FAILURE,
            termination_reason="JOB_DECLINED", resource_usage={}, evidence_json={"mode": "MOCK"},
        ))
    db.add(_audit(
        event_type="WORKER_JOB_ACKNOWLEDGED", actor_id=worker.worker_id,
        worker_id=worker.worker_id, execution_id=execution_id,
        details={"accepted": accepted},
    ))
    await db.commit()
    return job


async def renew_lease(
    db: AsyncSession, *, worker: Worker, execution_id: uuid.UUID,
    lease_id: uuid.UUID, fencing_token: int, sequence: int,
) -> WorkerExecutionJob:
    job = await _locked_job(
        db, worker=worker, execution_id=execution_id, lease_id=lease_id,
        fencing_token=fencing_token,
    )
    _next_sequence(job, sequence, {"op": "renew"})
    if job.status not in {ExecutionJobStatus.LEASED, ExecutionJobStatus.ACKNOWLEDGED}:
        raise WorkerProtocolError(409, "EXECUTION_NOT_LEASABLE")
    job.lease_expires_at = min(
        _normal(job.execution_expires_at), _now() + timedelta(seconds=LEASE_SECONDS)
    )
    await db.commit()
    return job


async def issue_artifact_capability(
    db: AsyncSession, *, worker: Worker, execution_id: uuid.UUID,
    lease_id: uuid.UUID, fencing_token: int, sequence: int,
) -> tuple[str, WorkerArtifactCapability]:
    job = await _locked_job(
        db, worker=worker, execution_id=execution_id, lease_id=lease_id,
        fencing_token=fencing_token,
    )
    _next_sequence(job, sequence, {"op": "artifact_capability"})
    if job.status != ExecutionJobStatus.ACKNOWLEDGED:
        raise WorkerProtocolError(409, "JOB_NOT_ACKNOWLEDGED")
    if not job.immutable_artifact_ref.startswith("sha256:"):
        raise WorkerProtocolError(409, "ARTIFACT_REFERENCE_INVALID")
    record = await db.get(QuarantineObject, job.artifact_sha256)
    if record is None or record.artifact_sha256 != job.artifact_sha256:
        raise WorkerProtocolError(409, "ARTIFACT_UNAVAILABLE")
    raw_capability = secrets.token_urlsafe(32)
    cap = WorkerArtifactCapability(
        token_hash=_hash_token(raw_capability), worker_id=worker.worker_id,
        execution_id=execution_id, lease_id=lease_id, fencing_token=fencing_token,
        artifact_sha256=job.artifact_sha256,
        submission_version_id=job.submission_version_id,
        operation="DOWNLOAD_ARCHIVE",
        expires_at=min(
            _normal(job.execution_expires_at), _now() + timedelta(seconds=CAPABILITY_SECONDS)
        ),
        max_uses=1, use_count=0,
    )
    db.add(cap)
    db.add(_audit(
        event_type="ARTIFACT_CAPABILITY_ISSUED", actor_id=worker.worker_id,
        worker_id=worker.worker_id, execution_id=execution_id,
        details={"artifact_sha256": job.artifact_sha256, "max_uses": 1},
    ))
    await db.commit()
    return raw_capability, cap


async def download_authorized_artifact(
    db: AsyncSession, *, worker: Worker, execution_id: uuid.UUID,
    lease_id: uuid.UUID, fencing_token: int, sequence: int, raw_capability: str,
) -> tuple[bytes, str]:
    job = await _locked_job(
        db, worker=worker, execution_id=execution_id, lease_id=lease_id,
        fencing_token=fencing_token,
    )
    cap = (
        await db.execute(
            select(WorkerArtifactCapability)
            .where(WorkerArtifactCapability.token_hash == _hash_token(raw_capability))
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = _now()
    if cap is None or cap.revoked_at is not None or _normal(cap.expires_at) <= now:
        raise WorkerProtocolError(403, "ARTIFACT_CAPABILITY_INVALID")
    if (
        cap.worker_id != worker.worker_id
        or cap.execution_id != execution_id
        or cap.lease_id != lease_id
        or cap.fencing_token != fencing_token
        or cap.artifact_sha256 != job.artifact_sha256
        or cap.submission_version_id != job.submission_version_id
        or cap.operation != "DOWNLOAD_ARCHIVE"
    ):
        raise WorkerProtocolError(403, "ARTIFACT_CAPABILITY_SCOPE_MISMATCH")
    if cap.use_count >= cap.max_uses:
        raise WorkerProtocolError(403, "ARTIFACT_CAPABILITY_EXHAUSTED")
    if job.status != ExecutionJobStatus.ACKNOWLEDGED:
        raise WorkerProtocolError(409, "JOB_NOT_ACKNOWLEDGED")
    _next_sequence(job, sequence, {"op": "artifact_download", "capability_id": str(cap.id)})
    cap.use_count += 1
    try:
        content = await quarantine_storage.verify(
            db, job.immutable_artifact_ref, job.artifact_sha256,
            (await db.get(SubmissionVersion, job.submission_version_id)).artifact_size,
        )
    except (ValueError, AttributeError) as exc:
        job.status = ExecutionJobStatus.FAILED
        cap.revoked_at = now
        db.add(_audit(
            event_type="WORKER_ARTIFACT_INTEGRITY_FAILURE", actor_id=worker.worker_id,
            worker_id=worker.worker_id, execution_id=execution_id,
            severity="CRITICAL", details={"artifact_sha256": job.artifact_sha256},
        ))
        await db.commit()
        raise WorkerProtocolError(409, "ARTIFACT_INTEGRITY_FAILURE") from exc
    db.add(_audit(
        event_type="WORKER_ARTIFACT_TRANSFERRED", actor_id=worker.worker_id,
        worker_id=worker.worker_id, execution_id=execution_id,
        details={"artifact_sha256": job.artifact_sha256, "size": len(content)},
    ))
    await db.commit()
    return content, job.artifact_sha256


async def submit_evidence(
    db: AsyncSession, *, worker: Worker, execution_id: uuid.UUID,
    lease_id: uuid.UUID, fencing_token: int, evidence,
) -> WorkerEvidence:
    job = await _locked_job(
        db, worker=worker, execution_id=execution_id, lease_id=lease_id,
        fencing_token=fencing_token,
    )
    _next_sequence(job, evidence.sequence, evidence.model_dump(mode="json"))
    if job.status not in {ExecutionJobStatus.ACKNOWLEDGED, ExecutionJobStatus.LEASED}:
        raise WorkerProtocolError(409, "EXECUTION_NOT_ACTIVE")
    if evidence.artifact_digest_verified and job.artifact_sha256 == "":
        raise WorkerProtocolError(409, "ARTIFACT_EVIDENCE_INVALID")
    record = WorkerEvidence(
        execution_id=execution_id, worker_id=worker.worker_id, sequence=evidence.sequence,
        event_type=evidence.event_type.value,
        started_at=evidence.started_at, ended_at=evidence.ended_at,
        exit_code=evidence.exit_code, failure_class=evidence.failure_class,
        termination_reason=evidence.termination_reason,
        resource_usage=evidence.resource_usage,
        artifact_digest_verified=evidence.artifact_digest_verified,
        stdout_bytes=evidence.stdout_bytes, stdout_sha256=evidence.stdout_sha256,
        stderr_bytes=evidence.stderr_bytes, stderr_sha256=evidence.stderr_sha256,
        protocol_violation=evidence.protocol_violation,
        sandbox_violation=evidence.sandbox_violation,
        evidence_json={"source": "worker_report", "authoritative": False},
        recorded_at=_now(),
    )
    db.add(record)
    if evidence.event_type.value in {"PROTOCOL_VIOLATION", "SANDBOX_VIOLATION", "FAILED"}:
        db.add(
            _audit(
                event_type=f"WORKER_EVIDENCE_{evidence.event_type.value}",
                actor_id=worker.worker_id,
                worker_id=worker.worker_id,
                execution_id=execution_id,
                severity="WARNING",
                details={
                    "failure_class": evidence.failure_class.value
                    if evidence.failure_class
                    else None
                },
            )
        )
    await db.commit()
    return record


async def submit_cleanup(
    db: AsyncSession, *, worker: Worker, execution_id: uuid.UUID,
    lease_id: uuid.UUID, fencing_token: int, sequence: int,
    cleanup_verified: bool, final_status: ExecutionJobStatus,
    failure_class: ExecutionFailureClass | None,
) -> WorkerExecutionJob:
    job = await _locked_job(
        db, worker=worker, execution_id=execution_id, lease_id=lease_id,
        fencing_token=fencing_token,
    )
    _next_sequence(job, sequence, {
        "op": "cleanup", "cleanup_verified": cleanup_verified, "status": final_status.value,
    })
    if final_status not in {ExecutionJobStatus.COMPLETED, ExecutionJobStatus.FAILED}:
        raise WorkerProtocolError(422, "FINAL_STATUS_INVALID")
    if not cleanup_verified:
        job.status = ExecutionJobStatus.FAILED
        db.add(_audit(
            event_type="WORKER_CLEANUP_UNVERIFIED", actor_id=worker.worker_id,
            worker_id=worker.worker_id, execution_id=execution_id, severity="CRITICAL",
        ))
        await db.commit()
        raise WorkerProtocolError(409, "CLEANUP_NOT_VERIFIED")
    job.status = final_status
    job.lease_expires_at = _now()
    db.add(WorkerEvidence(
        execution_id=execution_id, worker_id=worker.worker_id, sequence=sequence,
        event_type="CLEANUP", failure_class=failure_class,
        cleanup_verified=True, resource_usage={},
        evidence_json={"source": "worker_report", "authoritative": False},
    ))
    db.add(_audit(
        event_type="WORKER_CLEANUP_REPORTED", actor_id=worker.worker_id,
        worker_id=worker.worker_id, execution_id=execution_id,
        details={"final_status": final_status.value},
    ))
    await db.commit()
    return job
