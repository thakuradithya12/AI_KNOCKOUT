"""Organizer session database model."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class OrganizerAccount(TimestampMixin, Base):
    """Registered organizer account with role-based authorization."""

    __tablename__ = "organizer_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING_ORGANIZER")
    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrganizerSession(TimestampMixin, Base):
    """Server-managed organizer session."""

    __tablename__ = "organizer_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # SHA-256 hash of the bearer/cookie session token — raw token is never persisted
    session_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    # Verified organizer email from OIDC provider
    organizer_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Display name from OIDC token
    organizer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Cryptographically random CSRF token issued with the session
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)

    # Hard session expiration timestamp in UTC
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Inactivity sliding expiration tracker in UTC
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Explicit revocation timestamp (NULL if active)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # Audit telemetry (IP and user agent from session creation)
    created_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def is_active(self, idle_minutes: int = 60) -> bool:
        """Check if session is currently valid, unexpired, and not idle timed out."""
        now = datetime.now(timezone.utc)
        if self.revoked_at is not None:
            return False
        # Normalize expires_at timezone if naive
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return False
        # Normalize last_active_at timezone if naive
        last_active = self.last_active_at
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)
        if (now - last_active).total_seconds() > idle_minutes * 60:
            return False
        return True
