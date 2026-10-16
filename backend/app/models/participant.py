"""Participant identity, session, and audit database models.

Phase 2:
- Participant identity with immutable Participant ID and hashed Access Key
- Server-side revocable session tokens (hashed in DB)
- Append-only security audit log
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class ParticipantStatus(str, enum.Enum):
    """Participant lifecycle state."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class Participant(TimestampMixin, Base):
    """Registered participant or team identity."""

    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Server-generated, immutable public identifier (e.g. 'part_a1b2c3d4e5f60718')
    participant_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)

    # Human-readable participant or team display name
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)

    # Optional contact/email, normalized lowercase, unique when provided
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)

    # Optional team name
    team_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Cryptographic hash of the Access Key (RFC 7914 scrypt) — NEVER stores plaintext
    access_key_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Lifecycle status
    status: Mapped[ParticipantStatus] = mapped_column(
        Enum(ParticipantStatus, name="participant_status_enum", native_enum=False),
        nullable=False,
        default=ParticipantStatus.ACTIVE,
        server_default=ParticipantStatus.ACTIVE.value,
        index=True,
    )

    # Relationships
    sessions: Mapped[list[ParticipantSession]] = relationship(
        "ParticipantSession", back_populates="participant", cascade="all, delete-orphan"
    )


class ParticipantSession(TimestampMixin, Base):
    """Active or historic participant session token."""

    __tablename__ = "participant_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    participant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # SHA-256 hash of the bearer token — raw token is never persisted
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    # Expiration timestamp in UTC
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Explicit revocation timestamp (NULL if active)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Audit telemetry (IP and user agent from session creation)
    created_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    participant: Mapped[Participant] = relationship("Participant", back_populates="sessions")

    @property
    def is_active(self) -> bool:
        """Check if session is currently valid and unexpired."""
        now = datetime.now(timezone.utc)
        expires_at = self.expires_at
        if expires_at.tzinfo is None:  # SQLite returns naive UTC datetimes
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return self.revoked_at is None and expires_at > now


class AuthEventType(str, enum.Enum):
    """Classification of authentication and identity audit events."""

    REGISTRATION_SUCCESS = "REGISTRATION_SUCCESS"
    REGISTRATION_FAILURE = "REGISTRATION_FAILURE"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    LOGOUT = "LOGOUT"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_REVOKED = "SESSION_REVOKED"
    RATE_LIMITED = "RATE_LIMITED"
    STATUS_CHANGED = "STATUS_CHANGED"


class AuthEvent(Base):
    """Append-only audit log for security and identity operations.

    NEVER logs credentials, passwords, access keys, or bearer tokens.
    """

    __tablename__ = "auth_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Public participant_id string if known, NULL if unauthenticated or invalid
    participant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    event_type: Mapped[AuthEventType] = mapped_column(
        Enum(AuthEventType, name="auth_event_type_enum", native_enum=False),
        nullable=False,
        index=True,
    )

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Safe contextual details (e.g. failure reason) — strictly sanitised
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
