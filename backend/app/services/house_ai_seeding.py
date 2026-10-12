"""Deterministic House AI baseline seeding and registration service (DEF-07).

Ensures a verified, trusted House AI opponent is deterministically registered
and activated for development matches, strictly isolated from official tournament
selections and brackets.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.url import get_asyncpg_connect_args, normalize_async_database_url
from app.models.event import DevelopmentMatchPolicy
from app.models.house_ai import (
    EventHouseAISelection,
    HouseAI,
    HouseAIVersion,
)
from app.services.development_matches import (
    DEVELOPMENT_POLICY_ID,
    DEVELOPMENT_POLICY_VERSION,
    calculate_policy_digest,
)
from app.services.house_ai_configuration import (
    activate_house_ai_version,
    register_and_freeze_house_ai_version,
)
from app.services.submission_validation import validate_submission_package
from tests.fixtures.trusted_engine.builder import build_trusted_engine_archive

DEFAULT_BASELINE_HOUSE_AI_ID = "house-ai-benchmark-baseline"
DEFAULT_BASELINE_VERSION = 1
DEFAULT_BASELINE_ACTOR = "system-baseline-seeder"
DEFAULT_BASELINE_REASON = "DEVELOPMENT_BASELINE_SEED"


def get_default_development_policy_document() -> dict:
    """Canonical development match resource and protocol policy."""
    return {
        "policy_id": DEVELOPMENT_POLICY_ID,
        "version": DEVELOPMENT_POLICY_VERSION,
        "policy_version": DEVELOPMENT_POLICY_VERSION,
        "protocol_version": "aik-jsonl/1",
        "runtime_profile": "python-3.12",
        "rules_version": "standard-chess/python-chess-1.11.2",
        "initial_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "initial_clock_ms": 300_000,
        "increment_ms": 0,
        "move_timeout_ms": 2_000,
        "resources": {
            "cpu_millicores": 1000,
            "memory_bytes": 268435456,
            "pid_limit": 16,
            "tmpfs_bytes": 33554432,
            "inode_limit": 128,
            "nofile_limit": 64,
            "stdout_line_bytes": 65536,
            "stdout_total_bytes": 1048576,
            "stderr_total_bytes": 262144,
            "startup_timeout_ms": 5000,
            "ready_timeout_ms": 2000,
            "move_timeout_ms": 2000,
            "process_lifetime_ms": 900000,
            "concurrency_per_worker": 1,
            "network_mode": "DENY_ALL",
        },
    }


async def ensure_development_policy(db: AsyncSession) -> DevelopmentMatchPolicy:
    """Ensure the baseline development policy exists and matches canonical digest."""
    policy = await db.get(DevelopmentMatchPolicy, (DEVELOPMENT_POLICY_ID, DEVELOPMENT_POLICY_VERSION))
    if policy is None:
        doc = get_default_development_policy_document()
        digest = calculate_policy_digest(doc)
        policy = DevelopmentMatchPolicy(
            policy_id=DEVELOPMENT_POLICY_ID,
            version=DEVELOPMENT_POLICY_VERSION,
            policy_digest=digest,
            policy_document=doc,
            frozen_at=datetime.now(timezone.utc),
        )
        db.add(policy)
        await db.flush()
    return policy


async def seed_deterministic_house_ai_baseline(
    db: AsyncSession,
    *,
    house_ai_id: str = DEFAULT_BASELINE_HOUSE_AI_ID,
    version: int = DEFAULT_BASELINE_VERSION,
    archive_bytes: bytes | None = None,
    actor_id: str = DEFAULT_BASELINE_ACTOR,
    reason_code: str = DEFAULT_BASELINE_REASON,
    auto_activate: bool = True,
) -> tuple[HouseAIVersion, Optional[EventHouseAISelection]]:
    """Register and activate a deterministic trusted House AI baseline for development matches.

    Guarantees:
    - Re-entrant and idempotent: will not duplicate registrations.
    - Validates canonical USTAR packaging and manifest.
    - Strictly bound to DEVELOPMENT policy; cannot enter official tournament selection.
    """
    if archive_bytes is None:
        archive_bytes = build_trusted_engine_archive()

    # 1. Validate package structure & protocol
    validated = validate_submission_package(archive_bytes)
    digest = hashlib.sha256(archive_bytes).hexdigest()

    # 2. Ensure Development Policy
    policy = await ensure_development_policy(db)

    # 3. Check existing version
    existing_version = await db.scalar(
        select(HouseAIVersion).where(
            HouseAIVersion.house_ai_id == house_ai_id,
            HouseAIVersion.version == version,
        )
    )

    if existing_version is None:
        version_row = await register_and_freeze_house_ai_version(
            db,
            house_ai_id=house_ai_id,
            version=version,
            archive_bytes=archive_bytes,
            protocol_version=validated.manifest["protocol_version"],
            runtime_profile=validated.manifest["runtime"],
            runtime_identity_sha256=digest,
            rules_version=policy.policy_document.get("rules_version", "standard-chess/python-chess-1.11.2"),
            policy_version=policy.version,
            policy_digest=policy.policy_digest,
            actor_id=actor_id,
            reason_code=reason_code,
        )
    else:
        version_row = existing_version

    # 4. Activate selection if requested and not already active
    selection_row = None
    if auto_activate:
        active_selection = await db.scalar(
            select(EventHouseAISelection).where(
                EventHouseAISelection.deactivated_at.is_(None)
            )
        )
        if (
            active_selection is None
            or active_selection.house_ai_id != house_ai_id
            or active_selection.house_ai_version != version
        ):
            selection_row = await activate_house_ai_version(
                db,
                house_ai_id=house_ai_id,
                version=version,
                actor_id=actor_id,
                reason_code=f"{reason_code}_ACTIVE",
            )
        else:
            selection_row = active_selection

    return version_row, selection_row


async def _cli_main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic House AI Baseline Seeder (DEF-07)")
    parser.add_argument("--db-url", type=str, default=None, help="Target PostgreSQL database URL (defaults to env)")
    parser.add_argument("--house-ai-id", type=str, default=DEFAULT_BASELINE_HOUSE_AI_ID, help="House AI ID")
    parser.add_argument("--version", type=int, default=DEFAULT_BASELINE_VERSION, help="House AI version")
    parser.add_argument("--no-activate", action="store_true", help="Do not activate the baseline selection")
    args = parser.parse_args()

    import os
    db_url = args.db_url or os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: No database URL provided or found in environment.", file=sys.stderr)
        sys.exit(1)

    normalized = normalize_async_database_url(db_url)
    connect_args = get_asyncpg_connect_args(db_url)
    engine = create_async_engine(normalized, connect_args=connect_args)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        version_row, selection_row = await seed_deterministic_house_ai_baseline(
            session,
            house_ai_id=args.house_ai_id,
            version=args.version,
            auto_activate=not args.no_activate,
        )
        await session.commit()
        print(f"SUCCESS: Seeded House AI {version_row.house_ai_id} v{version_row.version} (digest: {version_row.artifact_sha256[:16]}...)")
        if selection_row:
            print(f"ACTIVE SELECTION: {selection_row.house_ai_id} v{selection_row.house_ai_version} (selection_id: {selection_row.selection_id})")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_cli_main())
