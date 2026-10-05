"""Organizer-owned immutable House AI identities and artifact references."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base


class HouseAI(Base):
    __tablename__ = "house_ais"

    house_ai_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HouseAIArtifact(Base):
    """Digest-addressed organizer artifact reference; never participant quarantine."""

    __tablename__ = "house_ai_artifacts"
    __table_args__ = (
        CheckConstraint("artifact_size >= 0", name="ck_house_ai_artifact_size"),
        CheckConstraint("length(artifact_sha256) = 64", name="ck_house_ai_artifact_digest"),
        UniqueConstraint(
            "artifact_sha256", "artifact_size", name="uq_house_ai_artifact_digest_size"
        ),
    )

    artifact_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HouseAIVersion(Base):
    __tablename__ = "house_ai_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_house_ai_version_positive"),
        CheckConstraint("artifact_size >= 0", name="ck_house_ai_version_size"),
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


@event.listens_for(Session, "before_flush")
def enforce_house_ai_immutability(session: Session, _flush_context, _instances) -> None:
    import re

    immutable = (HouseAI, HouseAIArtifact, HouseAIVersion)
    for row in session.new:
        if isinstance(row, HouseAIArtifact) and re.fullmatch(
            r"[0-9a-f]{64}", row.artifact_sha256 or ""
        ) is None:
            raise ValueError("HOUSE_AI_ARTIFACT_DIGEST_INVALID")
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
