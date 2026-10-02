"""Submission intake records; no table in this module contains executable state."""

from __future__ import annotations

import enum
import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, ForeignKeyConstraint,
    Integer, JSON, LargeBinary, String, UniqueConstraint, Uuid, event, func,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base


class ArtifactState(str, enum.Enum):
    QUARANTINED = "QUARANTINED"
    INSPECTING = "INSPECTING"
    VALIDATING = "VALIDATING"
    APPROVED = "APPROVED"
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    DEPRECATED = "DEPRECATED"
    REVOKED = "REVOKED"


class ValidationOutcome(str, enum.Enum):
    PASSED = "PASSED"
    REJECTED = "REJECTED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"


ALLOWED_TRANSITIONS: dict[ArtifactState, set[ArtifactState]] = {
    ArtifactState.QUARANTINED: {ArtifactState.INSPECTING, ArtifactState.REVOKED},
    ArtifactState.INSPECTING: {ArtifactState.VALIDATING, ArtifactState.REJECTED,
                               ArtifactState.RETRYABLE_FAILURE, ArtifactState.REVOKED},
    ArtifactState.VALIDATING: {ArtifactState.APPROVED, ArtifactState.REJECTED,
                               ArtifactState.RETRYABLE_FAILURE, ArtifactState.REVOKED},
    ArtifactState.RETRYABLE_FAILURE: {ArtifactState.INSPECTING, ArtifactState.REVOKED},
    ArtifactState.APPROVED: {ArtifactState.ELIGIBLE, ArtifactState.DEPRECATED, ArtifactState.REVOKED},
    ArtifactState.ELIGIBLE: {ArtifactState.APPROVED, ArtifactState.DEPRECATED, ArtifactState.REVOKED},
    ArtifactState.DEPRECATED: {ArtifactState.REVOKED},
    ArtifactState.REJECTED: set(),
    ArtifactState.REVOKED: set(),
}


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("id", "participant_id", name="uq_submissions_id_owner"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("participants.participant_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )


class QuarantineObject(Base):
    """Exact raw tar bytes, transactionally kept in a digest-addressed quarantine table."""

    __tablename__ = "quarantine_objects"
    __table_args__ = (
        UniqueConstraint("artifact_sha256", "artifact_size", name="uq_quarantine_digest_size"),
        CheckConstraint("artifact_size >= 0", name="ck_quarantine_size_nonnegative"),
    )

    artifact_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    archive_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )


class SubmissionVersion(Base):
    __tablename__ = "submission_versions"
    __table_args__ = (
        UniqueConstraint("submission_id", "version", name="uq_submission_version_number"),
        ForeignKeyConstraint(
            ["submission_id", "participant_id"], ["submissions.id", "submissions.participant_id"],
            ondelete="RESTRICT", name="fk_submission_version_owner",
        ),
        ForeignKeyConstraint(
            ["artifact_sha256", "artifact_size"],
            ["quarantine_objects.artifact_sha256", "quarantine_objects.artifact_size"],
            ondelete="RESTRICT", name="fk_submission_version_object",
        ),
        CheckConstraint("version > 0", name="ck_submission_version_positive"),
        CheckConstraint("artifact_size >= 0", name="ck_submission_version_size_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(80), nullable=False)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    contract_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    protocol_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[ArtifactState] = mapped_column(
        Enum(ArtifactState, name="artifact_state", native_enum=False, length=32), nullable=False
    )
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )


class ValidationAttempt(Base):
    __tablename__ = "validation_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["submission_version_id"], ["submission_versions.id"],
            ondelete="RESTRICT", name="fk_validation_attempt_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[ValidationOutcome] = mapped_column(
        Enum(ValidationOutcome, name="validation_outcome", native_enum=False, length=32), nullable=False
    )
    report: Mapped[dict] = mapped_column(JSON, nullable=False)
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ArtifactStateTransition(Base):
    __tablename__ = "artifact_state_transitions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["submission_version_id"], ["submission_versions.id"],
            ondelete="RESTRICT", name="fk_artifact_transition_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[ArtifactState] = mapped_column(
        Enum(ArtifactState, name="artifact_transition_state", native_enum=False, length=32), nullable=False
    )
    actor_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )


class CompetitionEvent(Base):
    """Append-only submission lifecycle event/outbox; distinct from AuthEvent."""

    __tablename__ = "competition_events"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_type", "aggregate_id", "aggregate_sequence",
            name="uq_competition_event_aggregate_sequence",
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    aggregate_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )


_IMMUTABLE_MODELS = (Submission, QuarantineObject, ValidationAttempt,
                     ArtifactStateTransition, CompetitionEvent)
_VERSION_IMMUTABLE_FIELDS = (
    "id", "submission_id", "participant_id", "version", "artifact_sha256",
    "artifact_size", "storage_key", "manifest", "contract_version",
    "protocol_version", "runtime_profile", "created_at",
)


@event.listens_for(Session, "before_flush")
def enforce_submission_immutability(session: Session, _flush_context, _instances) -> None:
    """Portable ORM guard complementing PostgreSQL triggers in the production migration."""
    for obj in session.new:
        if isinstance(obj, QuarantineObject):
            if obj.artifact_size != len(obj.archive_bytes) or hashlib.sha256(obj.archive_bytes).hexdigest() != obj.artifact_sha256:
                raise ValueError("ARTIFACT_DIGEST_MISMATCH")
        if isinstance(obj, SubmissionVersion):
            if obj.state != ArtifactState.QUARANTINED or obj.rejection_code is not None:
                raise ValueError("ARTIFACT_MUST_START_QUARANTINED")
    for obj in session.dirty:
        if isinstance(obj, _IMMUTABLE_MODELS) and session.is_modified(obj, include_collections=False):
            raise ValueError("APPEND_ONLY_RECORD_IMMUTABLE")
        if isinstance(obj, SubmissionVersion) and session.is_modified(obj, include_collections=False):
            from sqlalchemy import inspect

            state = inspect(obj)
            changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
            if changed - {"state", "rejection_code"}:
                raise ValueError("ARTIFACT_IDENTITY_IMMUTABLE")
            if "rejection_code" in changed and "state" not in changed:
                raise ValueError("REJECTION_CODE_REQUIRES_STATE_TRANSITION")
            if "state" in changed:
                old_state = state.attrs.state.history.deleted[0]
                new_state = obj.state
                if new_state not in ALLOWED_TRANSITIONS.get(old_state, set()):
                    raise ValueError("INVALID_ARTIFACT_STATE_TRANSITION")
                transition_exists = any(
                    isinstance(transition, ArtifactStateTransition)
                    and transition.submission_version_id == obj.id
                    and transition.from_state == old_state.value
                    and transition.to_state == new_state
                    for transition in [*session.new, *session.identity_map.values()]
                )
                if not transition_exists:
                    raise ValueError("STATE_TRANSITION_RECORD_REQUIRED")
                if new_state == ArtifactState.REJECTED:
                    matching = [
                        transition for transition in [*session.new, *session.identity_map.values()]
                        if isinstance(transition, ArtifactStateTransition)
                        and transition.submission_version_id == obj.id
                        and transition.from_state == old_state.value
                        and transition.to_state == new_state
                    ]
                    if not matching or obj.rejection_code != matching[-1].reason_code:
                        raise ValueError("REJECTION_CODE_TRANSITION_MISMATCH")
    for obj in session.deleted:
        if isinstance(obj, (*_IMMUTABLE_MODELS, SubmissionVersion)):
            raise ValueError("APPEND_ONLY_RECORD_CANNOT_BE_DELETED")
