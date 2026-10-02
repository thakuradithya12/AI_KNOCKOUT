"""System infrastructure model.

Minimal model to validate the full database pipeline:
Alembic migration → SQLAlchemy model → API endpoint.

This is NOT a business model. It exists solely to prove the
infrastructure works end-to-end.
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SystemInfo(TimestampMixin, Base):
    """Infrastructure verification record."""

    __tablename__ = "system_info"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
