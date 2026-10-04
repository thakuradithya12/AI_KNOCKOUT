"""Server-owned event lifecycle state."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

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
