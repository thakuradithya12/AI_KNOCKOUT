"""Server-owned event lifecycle state."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    func,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base


class BuildSession(Base):
    """One immutable server-timestamped build start for each participant."""

    __tablename__ = "build_sessions"

    participant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("participants.participant_id", ondelete="RESTRICT"), primary_key=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_source: Mapped[str] = mapped_column(String(32), nullable=False)


class EventControl(Base):
    """Singleton event control record; a freeze is one-way and operator invoked."""

    __tablename__ = "event_control"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_event_control_singleton"),
        CheckConstraint(
            "(official_freeze_at IS NULL AND official_freeze_actor_id IS NULL "
            "AND official_freeze_reason IS NULL) OR "
            "(official_freeze_at IS NOT NULL AND official_freeze_actor_id IS NOT NULL "
            "AND official_freeze_reason IS NOT NULL)",
            name="ck_event_control_freeze_audit",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    official_freeze_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    official_freeze_actor_id: Mapped[str | None] = mapped_column(String(64))
    official_freeze_reason: Mapped[str | None] = mapped_column(String(64))


class DevelopmentMatchPolicy(Base):
    """Immutable, content-digested policy snapshot used by development matches."""

    __tablename__ = "development_match_policies"
    __table_args__ = (
        CheckConstraint("length(policy_digest) = 64", name="ck_development_policy_digest"),
        UniqueConstraint(
            "policy_id", "version", "policy_digest",
            name="uq_development_policy_digest_identity",
        ),
    )

    policy_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    policy_document: Mapped[dict] = mapped_column(JSON, nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(Session, "before_flush")
def enforce_development_policy_immutability(session, _flush_context, _instances) -> None:
    for row in session.new:
        if isinstance(row, DevelopmentMatchPolicy):
            canonical = json.dumps(
                row.policy_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            if hashlib.sha256(canonical).hexdigest() != row.policy_digest:
                raise ValueError("DEVELOPMENT_POLICY_DIGEST_INVALID")
    if any(isinstance(row, DevelopmentMatchPolicy) for row in session.dirty):
        raise ValueError("DEVELOPMENT_POLICY_IMMUTABLE")
    if any(isinstance(row, DevelopmentMatchPolicy) for row in session.deleted):
        raise ValueError("DEVELOPMENT_POLICY_CANNOT_BE_DELETED")


class OfficialSubmissionSelection(Base):
    """Participant's current intended official version, mutable only before freeze."""

    __tablename__ = "official_submission_selections"
    __table_args__ = (
        UniqueConstraint("participant_id", name="uq_official_selection_participant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("participants.participant_id", ondelete="RESTRICT"),
        nullable=False,
    )
    submission_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submission_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ParticipantSubmissionAllowance(Base):
    """Participant submission quota and organizer exception allowance."""

    __tablename__ = "participant_submission_allowances"
    __table_args__ = (
        CheckConstraint("submission_limit >= 1", name="ck_allowance_limit_positive"),
    )

    participant_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("participants.participant_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    submission_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    granted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class SubmissionAllowanceRequest(Base):
    """Participant request for an increase to their submission quota."""

    __tablename__ = "submission_allowance_requests"
    __table_args__ = (
        CheckConstraint("requested_limit >= 1", name="ck_request_limit_positive"),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED')", name="ck_request_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    participant_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("participants.participant_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", server_default="PENDING"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TournamentSnapshot(Base):
    """Immutable snapshot of eligible participants frozen at official tournament cutoff."""

    __tablename__ = "tournament_snapshots"
    __table_args__ = (
        UniqueConstraint("tournament_id", "participant_id", name="uq_snapshot_tournament_participant"),
        UniqueConstraint("tournament_id", "seed", name="uq_snapshot_tournament_seed"),
        CheckConstraint("seed >= 1", name="ck_snapshot_seed_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tournament_id: Mapped[str] = mapped_column(String(64), nullable=False, default="official-v1")
    participant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("participants.participant_id", ondelete="RESTRICT"), nullable=False
    )
    submission_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("submission_versions.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), nullable=False
    )


class TournamentRoundRecord(Base):
    """Durable tournament round in Swiss or Playoff stage."""

    __tablename__ = "tournament_rounds"
    __table_args__ = (
        UniqueConstraint("tournament_id", "stage", "round_number", name="uq_round_stage_number"),
        CheckConstraint("round_number >= 1", name="ck_round_number_positive"),
        CheckConstraint("stage IN ('SWISS', 'PLAYOFF')", name="ck_round_stage"),
        CheckConstraint("status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED')", name="ck_round_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tournament_id: Mapped[str] = mapped_column(String(64), nullable=False, default="official-v1")
    stage: Mapped[str] = mapped_column(String(32), nullable=False)  # SWISS or PLAYOFF
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    round_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", server_default="PENDING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TournamentPairingRecord(Base):
    """Durable tournament pairing consisting of two games with reversed colors (or a bye), plus sudden-death games."""

    __tablename__ = "tournament_pairings"
    __table_args__ = (
        UniqueConstraint("round_id", "pairing_index", name="uq_pairing_round_index"),
        CheckConstraint("pairing_index >= 1", name="ck_pairing_index_positive"),
        CheckConstraint("status IN ('QUEUED', 'IN_PROGRESS', 'COMPLETED')", name="ck_pairing_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    round_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tournament_rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pairing_index: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_name: Mapped[str] = mapped_column(String(64), nullable=False, default="SWISS")  # SWISS, QUARTERFINAL, SEMIFINAL, FINAL
    participant1_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("participants.participant_id", ondelete="RESTRICT"), nullable=False
    )
    participant2_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("participants.participant_id", ondelete="RESTRICT"), nullable=True
    )
    is_bye: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    game1_match_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("matches.id", ondelete="SET NULL"), nullable=True
    )
    game2_match_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("matches.id", ondelete="SET NULL"), nullable=True
    )
    sudden_death_match_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED", server_default="QUEUED")
    winner_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    p1_points: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0.0")
    p2_points: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0.0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )

