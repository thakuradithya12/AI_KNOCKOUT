"""Backend-neutral digest object interface and the current PostgreSQL quarantine adapter."""

from __future__ import annotations

import hashlib
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import QuarantineObject


class QuarantineStorage(Protocol):
    async def put(self, db: AsyncSession, digest: str, content: bytes) -> str: ...

    async def verify(
        self, db: AsyncSession, storage_key: str, digest: str, expected_size: int
    ) -> bytes: ...


class PostgresQuarantineStorage:
    """Persist exact raw archive bytes as an immutable digest-keyed BYTEA row."""

    async def put(self, db: AsyncSession, digest: str, content: bytes) -> str:
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("ARTIFACT_DIGEST_MISMATCH")
        existing = await db.get(QuarantineObject, digest)
        if existing is None:
            db.add(
                QuarantineObject(
                    artifact_sha256=digest,
                    artifact_size=len(content),
                    archive_bytes=content,
                )
            )
            await db.flush()
            return f"sha256:{digest}"
        if (
            existing.artifact_size != len(content)
            or existing.archive_bytes != content
            or hashlib.sha256(existing.archive_bytes).hexdigest() != digest
        ):
            raise ValueError("ARTIFACT_STORAGE_INTEGRITY_FAILURE")
        return f"sha256:{digest}"

    async def verify(
        self, db: AsyncSession, storage_key: str, digest: str, expected_size: int
    ) -> bytes:
        if storage_key != f"sha256:{digest}":
            raise ValueError("ARTIFACT_STORAGE_INTEGRITY_FAILURE")
        record = await db.get(QuarantineObject, digest)
        if record is None or record.artifact_size != expected_size:
            raise ValueError("ARTIFACT_STORAGE_INTEGRITY_FAILURE")
        if hashlib.sha256(record.archive_bytes).hexdigest() != digest:
            raise ValueError("ARTIFACT_STORAGE_INTEGRITY_FAILURE")
        return record.archive_bytes


quarantine_storage: QuarantineStorage = PostgresQuarantineStorage()
