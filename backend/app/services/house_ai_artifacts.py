"""Trusted resolver for immutable, digest-addressed organizer House AI bytes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.house_ai import (
    MAX_HOUSE_AI_ARCHIVE_BYTES,
    HouseAIArtifact,
    HouseAIVersion,
)


class HouseAIArtifactError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VerifiedHouseAIArtifact:
    house_ai_id: str
    version: int
    digest: str
    size: int
    storage_ref: str
    archive_bytes: bytes = field(repr=False)


async def resolve_house_ai_artifact(
    db: AsyncSession, *, house_ai_id: str, version: int, expected_digest: str
) -> VerifiedHouseAIArtifact:
    """Resolve only the internal content-addressed DB ref; never open a caller path."""
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest or "") is None:
        raise HouseAIArtifactError("HOUSE_AI_DIGEST_INVALID")
    identity = await db.scalar(
        select(HouseAIVersion).where(
            HouseAIVersion.house_ai_id == house_ai_id,
            HouseAIVersion.version == version,
            HouseAIVersion.artifact_sha256 == expected_digest,
            HouseAIVersion.frozen_at.is_not(None),
        )
    )
    if identity is None:
        raise HouseAIArtifactError("HOUSE_AI_VERSION_NOT_FOUND_OR_UNFROZEN")
    artifact = await db.get(HouseAIArtifact, expected_digest)
    if artifact is None:
        raise HouseAIArtifactError("HOUSE_AI_ARTIFACT_MISSING")
    expected_ref = f"house-ai://sha256/{expected_digest}"
    data = artifact.archive_bytes
    if (
        artifact.storage_ref != expected_ref
        or artifact.artifact_sha256 != expected_digest
        or artifact.artifact_size != identity.artifact_size
        or artifact.artifact_size > MAX_HOUSE_AI_ARCHIVE_BYTES
        or data is None
        or len(data) != artifact.artifact_size
        or hashlib.sha256(data).hexdigest() != expected_digest
    ):
        raise HouseAIArtifactError("HOUSE_AI_ARTIFACT_INTEGRITY_FAILURE")
    return VerifiedHouseAIArtifact(
        house_ai_id=house_ai_id,
        version=version,
        digest=expected_digest,
        size=len(data),
        storage_ref=expected_ref,
        archive_bytes=bytes(data),
    )
