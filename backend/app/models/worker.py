"""Worker identity, enrollment, execution-lease and evidence records.

These records do not grant match authority. No worker private key is stored here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base


class WorkerStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    DECOMMISSIONED = "DECOMMISSIONED"


class ExecutionJobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CLEANUP_REPORTED = "CLEANUP_REPORTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class ExecutionKind(str, enum.Enum):
    PARTICIPANT = "PARTICIPANT"
    HOUSE_AI = "HOUSE_AI"


class ExecutionFailureClass(str, enum.Enum):
    PARTICIPANT_PROCESS_FAILURE = "PARTICIPANT_PROCESS_FAILURE"
    STARTUP_FAILURE = "STARTUP_FAILURE"
    READY_TIMEOUT = "READY_TIMEOUT"
    MOVE_TIMEOUT = "MOVE_TIMEOUT"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
    WORKER_FAILURE = "WORKER_FAILURE"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    STALE_EXECUTION = "STALE_EXECUTION"


class Worker(Base):
    __tablename__ = "workers"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    certificate_fingerprint: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    certificate_identity: Mapped[str | None] = mapped_column(String(512), nullable=True)
    certificate_serial: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending_certificate_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    pending_certificate_identity: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pending_certificate_serial: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending_issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_capabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    pending_software_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending_runtime_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[WorkerStatus] = mapped_column(
        Enum(WorkerStatus, native_enum=False, length=24),
        nullable=False,
        default=WorkerStatus.PENDING,
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    software_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    security_state: Mapped[str] = mapped_column(String(32), nullable=False, default="UNREVIEWED")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkerEnrollmentToken(Base):
    __tablename__ = "worker_enrollment_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("workers.worker_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, default="ENROLL")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkerExecutionJob(Base):
    __tablename__ = "worker_execution_jobs"
    __table_args__ = (
        Index(
            "uq_worker_job_current_match_participant",
            "match_id",
            "participant_id",
            unique=True,
            postgresql_where=text(
                "match_id IS NOT NULL AND participant_id IS NOT NULL AND status IN "
                "('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED')"
            ),
            sqlite_where=text(
                "match_id IS NOT NULL AND participant_id IS NOT NULL AND status IN "
                "('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED')"
            ),
        ),
        Index(
            "uq_worker_job_current_match_house_ai",
            "match_id",
            "house_ai_id",
            unique=True,
            postgresql_where=text(
                "match_id IS NOT NULL AND house_ai_id IS NOT NULL AND status IN "
                "('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED')"
            ),
            sqlite_where=text(
                "match_id IS NOT NULL AND house_ai_id IS NOT NULL AND status IN "
                "('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED')"
            ),
        ),
        UniqueConstraint("execution_id", name="uq_worker_job_execution"),
        CheckConstraint(
            "(execution_kind = 'PARTICIPANT' AND participant_id IS NOT NULL "
            "AND submission_id IS NOT NULL AND submission_version IS NOT NULL "
            "AND submission_version_id IS NOT NULL AND house_ai_id IS NULL "
            "AND house_ai_version IS NULL) OR "
            "(execution_kind = 'HOUSE_AI' AND participant_id IS NULL "
            "AND submission_id IS NULL AND submission_version IS NULL "
            "AND submission_version_id IS NULL AND house_ai_id IS NOT NULL "
            "AND house_ai_version IS NOT NULL)",
            name="ck_worker_job_subject_kind",
        ),
        CheckConstraint(
            "submission_version IS NULL OR submission_version > 0",
            name="ck_worker_job_version_positive",
        ),
        CheckConstraint(
            "execution_mode IN ('MOCK_PROTOCOL_ONLY', 'KATA_AI_EXECUTION')",
            name="ck_worker_job_execution_mode",
        ),
        ForeignKeyConstraint(
            ["submission_version_id", "participant_id", "submission_id", "artifact_sha256"],
            ["submission_versions.id", "submission_versions.participant_id",
             "submission_versions.submission_id", "submission_versions.artifact_sha256"],
            ondelete="RESTRICT",
            name="fk_worker_job_participant_subject",
        ),
        ForeignKeyConstraint(
            ["house_ai_id", "house_ai_version", "artifact_sha256"],
            ["house_ai_versions.house_ai_id", "house_ai_versions.version",
             "house_ai_versions.artifact_sha256"],
            ondelete="RESTRICT",
            name="fk_worker_job_house_ai_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    match_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    execution_kind: Mapped[ExecutionKind] = mapped_column(
        Enum(ExecutionKind, native_enum=False, length=16),
        nullable=False,
        default=ExecutionKind.PARTICIPANT,
    )
    participant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submission_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submission_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    house_ai_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    house_ai_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    immutable_artifact_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="MOCK_PROTOCOL_ONLY"
    )
    initial_state: Mapped[dict] = mapped_column(JSON, nullable=False)
    resource_policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    execution_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ExecutionJobStatus] = mapped_column(
        Enum(ExecutionJobStatus, native_enum=False, length=24), nullable=False,
        default=ExecutionJobStatus.QUEUED,
    )
    assigned_worker_id: Mapped[str | None] = mapped_column(
        ForeignKey("workers.worker_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    lease_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_operation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkerArtifactCapability(Base):
    __tablename__ = "worker_artifact_capabilities"
    __table_args__ = (
        CheckConstraint(
            "(execution_kind = 'PARTICIPANT' AND submission_version_id IS NOT NULL "
            "AND house_ai_id IS NULL AND house_ai_version IS NULL) OR "
            "(execution_kind = 'HOUSE_AI' AND submission_version_id IS NULL "
            "AND house_ai_id IS NOT NULL AND house_ai_version IS NOT NULL)",
            name="ck_worker_capability_subject_kind",
        ),
        ForeignKeyConstraint(
            ["house_ai_id", "house_ai_version", "artifact_sha256"],
            ["house_ai_versions.house_ai_id", "house_ai_versions.version",
             "house_ai_versions.artifact_sha256"],
            ondelete="RESTRICT",
            name="fk_worker_capability_house_ai_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("workers.worker_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("worker_execution_jobs.execution_id", ondelete="RESTRICT"), nullable=False
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    execution_kind: Mapped[ExecutionKind] = mapped_column(
        Enum(ExecutionKind, native_enum=False, length=16), nullable=False,
        default=ExecutionKind.PARTICIPANT,
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    submission_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("submission_versions.id", ondelete="RESTRICT"), nullable=True
    )
    house_ai_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    house_ai_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False, default="DOWNLOAD_ARCHIVE")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkerEvidence(Base):
    __tablename__ = "worker_evidence"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("worker_execution_jobs.execution_id", ondelete="RESTRICT"), nullable=False
    )
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("workers.worker_id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_class: Mapped[ExecutionFailureClass | None] = mapped_column(
        Enum(ExecutionFailureClass, native_enum=False, length=40), nullable=True
    )
    termination_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_usage: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    artifact_digest_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cleanup_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stdout_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stdout_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stderr_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stderr_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    protocol_violation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sandbox_violation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_worker_evidence_sequence"),
    )


class WorkerSecurityEvent(Base):
    __tablename__ = "worker_security_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@event.listens_for(Session, "before_flush")
def _guard_worker_records(session, flush_context, instances) -> None:
    """Portable guard mirroring the PostgreSQL immutable-job/audit triggers."""
    from sqlalchemy import inspect

    for obj in session.dirty:
        if isinstance(obj, WorkerExecutionJob):
            state = inspect(obj)
            immutable = (
                "execution_id", "match_id", "participant_id", "submission_id",
                "submission_version", "submission_version_id", "artifact_sha256",
                "immutable_artifact_ref", "runtime_profile", "protocol_version",
                "execution_mode", "initial_state", "resource_policy", "execution_expires_at",
                "execution_kind", "participant_id", "submission_id", "submission_version",
                "submission_version_id", "house_ai_id", "house_ai_version",
                "artifact_sha256", "immutable_artifact_ref",
            )
            if any(state.attrs[name].history.has_changes() for name in immutable):
                raise ValueError("WORKER_JOB_IDENTITY_IMMUTABLE")
            for name in ("fencing_token", "last_sequence"):
                history = state.attrs[name].history
                if (
                    history.has_changes()
                    and history.deleted
                    and getattr(obj, name) < history.deleted[0]
                ):
                    raise ValueError("WORKER_JOB_FENCE_SEQUENCE_CANNOT_DECREASE")
        elif isinstance(obj, (WorkerEvidence, WorkerSecurityEvent)):
            raise ValueError("WORKER_AUDIT_RECORD_APPEND_ONLY")
    for obj in session.deleted:
        if isinstance(obj, (WorkerEvidence, WorkerSecurityEvent)):
            raise ValueError("WORKER_AUDIT_RECORD_APPEND_ONLY")
