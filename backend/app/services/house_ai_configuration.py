"""Organizer-only House AI ingestion, freezing, and event selection operations."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import CompetitionEvent
from app.models.event import DevelopmentMatchPolicy
from app.models.house_ai import (
    MAX_HOUSE_AI_ARCHIVE_BYTES,
    EventHouseAISelection,
    EventHouseAISelectionControl,
    HouseAI,
    HouseAIArtifact,
    HouseAIVersion,
)
from app.services.house_ai_artifacts import (
    HouseAIArtifactError,
    resolve_house_ai_artifact,
)

DEVELOPMENT_POLICY_ID = "DEVELOPMENT"
DEVELOPMENT_POLICY_VERSION = "development-v1"


class HouseAIConfigurationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest_value(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value or "") is not None


def _event_id(house_ai_id: str, version: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aik-house-ai:{house_ai_id}:{version}")


async def get_development_policy(db: AsyncSession) -> DevelopmentMatchPolicy:
    policy = await db.get(
        DevelopmentMatchPolicy, (DEVELOPMENT_POLICY_ID, DEVELOPMENT_POLICY_VERSION)
    )
    if policy is None:
        raise HouseAIConfigurationError("DEVELOPMENT_POLICY_UNAVAILABLE")
    from app.services.development_matches import calculate_policy_digest

    if calculate_policy_digest(policy.policy_document) != policy.policy_digest:
        raise HouseAIConfigurationError("DEVELOPMENT_POLICY_INTEGRITY_FAILURE")
    return policy


async def register_and_freeze_house_ai_version(
    db: AsyncSession,
    *,
    house_ai_id: str,
    version: int,
    archive_bytes: bytes,
    protocol_version: str,
    runtime_profile: str,
    runtime_identity_sha256: str,
    rules_version: str,
    policy_version: str,
    policy_digest: str,
    actor_id: str,
    reason_code: str,
) -> HouseAIVersion:
    """Trusted internal operation; no participant/admin HTTP route exposes it."""
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", house_ai_id or "") is None
        or type(version) is not int
        or version < 1
        or not isinstance(archive_bytes, bytes)
        or not 1 <= len(archive_bytes) <= MAX_HOUSE_AI_ARCHIVE_BYTES
        or not actor_id
        or len(actor_id) > 64
        or not reason_code
        or len(reason_code) > 64
        or not protocol_version
        or len(protocol_version) > 64
        or not runtime_profile
        or len(runtime_profile) > 64
        or not rules_version
        or len(rules_version) > 64
        or not policy_version
        or len(policy_version) > 64
        or not _digest_value(runtime_identity_sha256)
        or not _digest_value(policy_digest)
    ):
        raise HouseAIConfigurationError("HOUSE_AI_VERSION_INPUT_INVALID")
    policy = await get_development_policy(db)
    document = policy.policy_document
    if (
        protocol_version != document["protocol_version"]
        or runtime_profile != document["runtime_profile"]
        or rules_version != document["rules_version"]
        or policy_version != policy.version
        or policy_digest != policy.policy_digest
    ):
        raise HouseAIConfigurationError("HOUSE_AI_VERSION_POLICY_INCOMPATIBLE")
    digest = hashlib.sha256(archive_bytes).hexdigest()
    now = _now()
    existing = await db.get(HouseAIVersion, (house_ai_id, version))
    if existing is not None:
        if (
            existing.artifact_sha256 != digest
            or existing.protocol_version != protocol_version
            or existing.runtime_profile != runtime_profile
            or existing.runtime_identity_sha256 != runtime_identity_sha256
            or existing.rules_version != rules_version
            or existing.policy_version != policy_version
            or existing.policy_digest != policy_digest
        ):
            raise HouseAIConfigurationError("HOUSE_AI_VERSION_ALREADY_FROZEN")
        await resolve_house_ai_artifact(
            db, house_ai_id=house_ai_id, version=version, expected_digest=digest
        )
        return existing

    identity = await db.get(HouseAI, house_ai_id)
    if identity is None:
        db.add(HouseAI(house_ai_id=house_ai_id, created_at=now))
        await db.flush()
    artifact = await db.get(HouseAIArtifact, digest)
    if artifact is None:
        db.add(
            HouseAIArtifact(
                artifact_sha256=digest,
                artifact_size=len(archive_bytes),
                storage_ref=f"house-ai://sha256/{digest}",
                archive_bytes=bytes(archive_bytes),
                created_at=now,
            )
        )
        await db.flush()
    elif (
        artifact.archive_bytes != archive_bytes
        or artifact.artifact_size != len(archive_bytes)
        or artifact.storage_ref != f"house-ai://sha256/{digest}"
    ):
        raise HouseAIConfigurationError("HOUSE_AI_ARTIFACT_IMMUTABLE_CONFLICT")

    row = HouseAIVersion(
        house_ai_id=house_ai_id,
        version=version,
        artifact_sha256=digest,
        artifact_size=len(archive_bytes),
        protocol_version=protocol_version,
        runtime_profile=runtime_profile,
        runtime_identity_sha256=runtime_identity_sha256,
        rules_version=rules_version,
        policy_version=policy_version,
        policy_digest=policy_digest,
        created_at=now,
        frozen_at=now,
    )
    db.add(row)
    await db.flush()
    await resolve_house_ai_artifact(
        db, house_ai_id=house_ai_id, version=version, expected_digest=digest
    )
    db.add(
        CompetitionEvent(
            event_type="HOUSE_AI_VERSION_FROZEN",
            actor_kind="organizer",
            actor_id=actor_id,
            severity="info",
            aggregate_type="house_ai_version",
            aggregate_id=_event_id(house_ai_id, version),
            aggregate_sequence=1,
            schema_version=1,
            metadata_json={
                "house_ai_id": house_ai_id,
                "version": version,
                "artifact_sha256": digest,
                "protocol_version": protocol_version,
                "runtime_profile": runtime_profile,
                "rules_version": rules_version,
                "reason_code": reason_code,
            },
            recorded_at=now,
        )
    )
    return row


async def activate_house_ai_version(
    db: AsyncSession,
    *,
    house_ai_id: str,
    version: int,
    actor_id: str,
    reason_code: str,
) -> EventHouseAISelection:
    """Select the exact frozen version; activation/deactivation is audited and serialized."""
    if not actor_id or len(actor_id) > 64 or not reason_code or len(reason_code) > 64:
        raise HouseAIConfigurationError("HOUSE_AI_SELECTION_AUDIT_REQUIRED")
    control = await db.scalar(
        select(EventHouseAISelectionControl)
        .where(EventHouseAISelectionControl.id == 1)
        .with_for_update()
    )
    if control is None:
        control = EventHouseAISelectionControl(id=1)
        db.add(control)
        await db.flush()
    policy = await get_development_policy(db)
    version_row = await db.scalar(
        select(HouseAIVersion)
        .where(HouseAIVersion.house_ai_id == house_ai_id, HouseAIVersion.version == version)
        .with_for_update(read=True)
    )
    if version_row is None or version_row.frozen_at is None:
        raise HouseAIConfigurationError("HOUSE_AI_VERSION_NOT_FROZEN")
    document = policy.policy_document
    if (
        version_row.protocol_version != document["protocol_version"]
        or version_row.runtime_profile != document["runtime_profile"]
        or version_row.rules_version != document["rules_version"]
        or version_row.policy_version != policy.version
        or version_row.policy_digest != policy.policy_digest
    ):
        raise HouseAIConfigurationError("HOUSE_AI_VERSION_POLICY_INCOMPATIBLE")
    try:
        await resolve_house_ai_artifact(
            db,
            house_ai_id=house_ai_id,
            version=version,
            expected_digest=version_row.artifact_sha256,
        )
    except HouseAIArtifactError as exc:
        raise HouseAIConfigurationError(exc.code) from exc

    active_rows = (
        await db.execute(
            select(EventHouseAISelection)
            .where(EventHouseAISelection.deactivated_at.is_(None))
            .with_for_update()
        )
    ).scalars().all()
    for active in active_rows:
        if (
            active.house_ai_id == house_ai_id
            and active.house_ai_version == version
            and active.artifact_sha256 == version_row.artifact_sha256
        ):
            return active
    now = _now()
    for active in active_rows:
        active.deactivated_at = now
        active.deactivated_by = actor_id
        active.deactivation_reason = reason_code
        db.add(
            CompetitionEvent(
                event_type="HOUSE_AI_SELECTION_DEACTIVATED",
                actor_kind="organizer",
                actor_id=actor_id,
                severity="info",
                aggregate_type="house_ai_selection",
                aggregate_id=active.selection_id,
                aggregate_sequence=2,
                schema_version=1,
                metadata_json={"reason_code": reason_code},
                recorded_at=now,
            )
        )
    selection = EventHouseAISelection(
        selection_id=uuid.uuid4(),
        house_ai_id=house_ai_id,
        house_ai_version=version,
        artifact_sha256=version_row.artifact_sha256,
        protocol_version=version_row.protocol_version,
        runtime_profile=version_row.runtime_profile,
        runtime_identity_sha256=version_row.runtime_identity_sha256,
        rules_version=version_row.rules_version,
        policy_version=version_row.policy_version,
        policy_digest=version_row.policy_digest,
        selected_by=actor_id,
        selection_reason=reason_code,
        selected_at=now,
    )
    db.add(selection)
    db.add(
        CompetitionEvent(
            event_type="HOUSE_AI_VERSION_SELECTED",
            actor_kind="organizer",
            actor_id=actor_id,
            severity="info",
            aggregate_type="house_ai_selection",
            aggregate_id=selection.selection_id,
            aggregate_sequence=1,
            schema_version=1,
            metadata_json={
                "house_ai_id": house_ai_id,
                "version": version,
                "artifact_sha256": version_row.artifact_sha256,
                "policy_digest": version_row.policy_digest,
                "reason_code": reason_code,
            },
            recorded_at=now,
        )
    )
    await db.flush()
    return selection


async def get_active_house_ai_selection(db: AsyncSession) -> EventHouseAISelection:
    control = await db.scalar(
        select(EventHouseAISelectionControl)
        .where(EventHouseAISelectionControl.id == 1)
        .with_for_update(read=True)
    )
    if control is None:
        raise HouseAIConfigurationError("HOUSE_AI_SELECTION_UNAVAILABLE")
    rows = (
        await db.execute(
            select(EventHouseAISelection).where(EventHouseAISelection.deactivated_at.is_(None))
        )
    ).scalars().all()
    if len(rows) != 1:
        raise HouseAIConfigurationError("HOUSE_AI_SELECTION_UNAVAILABLE")
    row = rows[0]
    version = await db.get(HouseAIVersion, (row.house_ai_id, row.house_ai_version))
    if (
        version is None
        or version.frozen_at is None
        or version.artifact_sha256 != row.artifact_sha256
        or version.protocol_version != row.protocol_version
        or version.runtime_profile != row.runtime_profile
        or version.runtime_identity_sha256 != row.runtime_identity_sha256
        or version.rules_version != row.rules_version
        or version.policy_version != row.policy_version
        or version.policy_digest != row.policy_digest
        or row.policy_version != DEVELOPMENT_POLICY_VERSION
    ):
        raise HouseAIConfigurationError("HOUSE_AI_SELECTION_INTEGRITY_FAILURE")
    try:
        await resolve_house_ai_artifact(
            db,
            house_ai_id=row.house_ai_id,
            version=row.house_ai_version,
            expected_digest=row.artifact_sha256,
        )
    except HouseAIArtifactError as exc:
        raise HouseAIConfigurationError(exc.code) from exc
    return row
