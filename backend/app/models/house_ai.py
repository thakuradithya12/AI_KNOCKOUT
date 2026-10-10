"""Organizer-owned immutable House AI identities and artifact references."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base

MAX_HOUSE_AI_ARCHIVE_BYTES = 16 * 1024 * 1024


class HouseAI(Base):
    __tablename__ = "house_ais"

    house_ai_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HouseAIArtifact(Base):
    """Digest-addressed organizer artifact reference; never participant quarantine."""

    __tablename__ = "house_ai_artifacts"
    __table_args__ = (
        CheckConstraint(
            f"artifact_size BETWEEN 0 AND {MAX_HOUSE_AI_ARCHIVE_BYTES}",
            name="ck_house_ai_artifact_size",
        ),
        CheckConstraint("length(artifact_sha256) = 64", name="ck_house_ai_artifact_digest"),
        UniqueConstraint(
            "artifact_sha256", "artifact_size", name="uq_house_ai_artifact_digest_size"
        ),
    )

    artifact_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    archive_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HouseAIVersion(Base):
    __tablename__ = "house_ai_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_house_ai_version_positive"),
        CheckConstraint(
            f"artifact_size BETWEEN 0 AND {MAX_HOUSE_AI_ARCHIVE_BYTES}",
            name="ck_house_ai_version_size",
        ),
        CheckConstraint("frozen_at IS NOT NULL", name="ck_house_ai_version_frozen"),
        CheckConstraint("length(artifact_sha256) = 64", name="ck_house_ai_version_digest"),
        UniqueConstraint(
            "house_ai_id", "version", "artifact_sha256",
            name="uq_house_ai_version_digest",
        ),
        ForeignKeyConstraint(
            ["artifact_sha256", "artifact_size"],
            ["house_ai_artifacts.artifact_sha256", "house_ai_artifacts.artifact_size"],
            ondelete="RESTRICT",
            name="fk_house_ai_version_artifact",
        ),
    )

    house_ai_id: Mapped[str] = mapped_column(
        ForeignKey("house_ais.house_ai_id", ondelete="RESTRICT"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_size: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventHouseAISelection(Base):
    """Auditable event-level activation of one exact frozen House AI version."""

    __tablename__ = "event_house_ai_selections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["house_ai_id", "house_ai_version", "artifact_sha256"],
            ["house_ai_versions.house_ai_id", "house_ai_versions.version",
             "house_ai_versions.artifact_sha256"],
            ondelete="RESTRICT",
            name="fk_event_house_ai_exact_version",
        ),
        CheckConstraint("length(artifact_sha256) = 64", name="ck_event_house_ai_digest"),
        CheckConstraint(
            "(deactivated_at IS NULL AND deactivated_by IS NULL "
            "AND deactivation_reason IS NULL) OR "
            "(deactivated_at IS NOT NULL AND deactivated_by IS NOT NULL "
            "AND deactivation_reason IS NOT NULL)",
            name="ck_event_house_ai_deactivation_audit",
        ),
        Index(
            "uq_event_house_ai_one_active",
            text("(1)"),
            unique=True,
            postgresql_where=text("deactivated_at IS NULL"),
            sqlite_where=text("deactivated_at IS NULL"),
        ),
    )

    selection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    house_ai_id: Mapped[str] = mapped_column(String(64), nullable=False)
    house_ai_version: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_identity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_by: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deactivated_by: Mapped[str | None] = mapped_column(String(64))
    deactivation_reason: Mapped[str | None] = mapped_column(String(64))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventHouseAISelectionControl(Base):
    """Singleton row used to serialize event House AI selection changes."""

    __tablename__ = "event_house_ai_selection_control"
    __table_args__ = (CheckConstraint("id = 1", name="ck_house_ai_selection_control_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


@event.listens_for(Session, "before_flush")
def enforce_house_ai_immutability(session: Session, _flush_context, _instances) -> None:
    import re

    immutable = (HouseAI, HouseAIArtifact, HouseAIVersion)
    for row in session.new:
        if isinstance(row, HouseAIArtifact) and re.fullmatch(
            r"[0-9a-f]{64}", row.artifact_sha256 or ""
        ) is None:
            raise ValueError("HOUSE_AI_ARTIFACT_DIGEST_INVALID")
        if isinstance(row, HouseAIArtifact) and row.archive_bytes is not None and (
            len(row.archive_bytes) != row.artifact_size
            or len(row.archive_bytes) > MAX_HOUSE_AI_ARCHIVE_BYTES
            or hashlib.sha256(row.archive_bytes).hexdigest() != row.artifact_sha256
            or row.storage_ref != f"house-ai://sha256/{row.artifact_sha256}"
        ):
            raise ValueError("HOUSE_AI_ARTIFACT_BYTES_INVALID")
        if isinstance(row, HouseAIVersion) and any(
            re.fullmatch(r"[0-9a-f]{64}", value or "") is None
            for value in (
                row.artifact_sha256,
                row.runtime_identity_sha256,
                row.policy_digest,
            )
        ):
            raise ValueError("HOUSE_AI_VERSION_DIGEST_INVALID")
    if any(
        isinstance(row, immutable) and session.is_modified(row, include_collections=False)
        for row in session.dirty
    ):
        raise ValueError("HOUSE_AI_IDENTITY_IMMUTABLE")
    if any(isinstance(row, immutable) for row in session.deleted):
        raise ValueError("HOUSE_AI_IDENTITY_CANNOT_BE_DELETED")
    for row in session.dirty:
        if isinstance(row, EventHouseAISelection):
            from sqlalchemy import inspect

            state = inspect(row)
            changed = {
                attribute.key
                for attribute in state.attrs
                if attribute.history.has_changes()
            }
            if changed != {"deactivated_at", "deactivated_by", "deactivation_reason"} or (
                state.attrs.deactivated_at.history.deleted
                and state.attrs.deactivated_at.history.deleted[0] is not None
            ):
                raise ValueError("HOUSE_AI_SELECTION_IDENTITY_IMMUTABLE")
    if any(isinstance(row, EventHouseAISelection) for row in session.deleted):
        raise ValueError("HOUSE_AI_SELECTION_CANNOT_BE_DELETED")
