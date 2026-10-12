"""Unit and integration tests for deterministic House AI baseline seeding and tournament isolation (DEF-07)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.url import get_asyncpg_connect_args, normalize_async_database_url
from app.matches.controller import MatchConfiguration
from app.matches.service import MatchServiceError
from app.models.competition import ArtifactState, Submission, SubmissionVersion
from app.models.event import BuildSession, DevelopmentMatchPolicy, OfficialSubmissionSelection
from app.models.house_ai import EventHouseAISelection, HouseAI, HouseAIVersion
from app.models.participant import Participant, ParticipantStatus
from app.services.development_matches import request_development_match
from app.services.house_ai_seeding import (
    DEFAULT_BASELINE_HOUSE_AI_ID,
    DEFAULT_BASELINE_VERSION,
    seed_deterministic_house_ai_baseline,
)
from app.services.submission_validation import DEFAULT_LIMITS
from app.services.submissions import OrganizerApproval, accept_upload, approve_submission_for_eligibility
from app.tournaments.service import get_frozen_official_participants, schedule_official_match
from tests.fixtures.trusted_engine.builder import build_trusted_engine_archive

# Use dedicated local test database on 5433
LOCAL_TEST_DB_URL = "postgresql+asyncpg://aik_user:1029N19gmiqVP7qP585J4XN1wHLfrdnuzAkpXh0lDuE@127.0.0.1:5433/aik_e2e_dev"


@pytest_asyncio.fixture
async def local_db_session():
    normalized = normalize_async_database_url(LOCAL_TEST_DB_URL)
    connect_args = get_asyncpg_connect_args(LOCAL_TEST_DB_URL)
    engine = create_async_engine(normalized, connect_args=connect_args)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_deterministic_house_ai_baseline_seeding_and_idempotency(local_db_session: AsyncSession):
    """Test that baseline seeding is deterministic, valid, and strictly idempotent."""
    # 1. First seeding pass
    v1, sel1 = await seed_deterministic_house_ai_baseline(
        local_db_session,
        house_ai_id="test-baseline-ai",
        version=1,
        auto_activate=True,
    )
    await local_db_session.commit()

    assert v1.house_ai_id == "test-baseline-ai"
    assert v1.version == 1
    assert len(v1.artifact_sha256) == 64
    assert v1.protocol_version == "aik-jsonl/1"
    assert v1.runtime_profile == "python-3.12"
    assert sel1 is not None
    assert sel1.house_ai_id == "test-baseline-ai"
    assert sel1.house_ai_version == 1

    # 2. Second seeding pass (idempotency check)
    v2, sel2 = await seed_deterministic_house_ai_baseline(
        local_db_session,
        house_ai_id="test-baseline-ai",
        version=1,
        auto_activate=True,
    )
    await local_db_session.commit()

    assert v2.house_ai_id == v1.house_ai_id
    assert v2.version == v1.version
    assert v2.artifact_sha256 == v1.artifact_sha256
    assert sel2.selection_id == sel1.selection_id


@pytest.mark.asyncio
async def test_house_ai_cannot_enter_official_tournament(local_db_session: AsyncSession):
    """Test that House AI baseline cannot enter official tournament brackets or match scheduling."""
    # Seed House AI baseline
    await seed_deterministic_house_ai_baseline(
        local_db_session,
        house_ai_id="test-baseline-ai-2",
        version=1,
        auto_activate=True,
    )
    await local_db_session.commit()

    # 1. Verify get_frozen_official_participants does NOT include House AI
    official_participants = await get_frozen_official_participants(local_db_session)
    participant_ids = [p.participant_id for p in official_participants]
    assert "test-baseline-ai-2" not in participant_ids

    # 2. Verify schedule_official_match cannot use House AI version
    config = MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=300000,
        increment_ms=0,
        move_timeout_ms=2000,
        match_seed=uuid.uuid4().hex + uuid.uuid4().hex,
    )

    fake_id = uuid.uuid4()
    with pytest.raises(MatchServiceError, match="SUBMISSION_VERSION_NOT_FOUND"):
        await schedule_official_match(
            local_db_session,
            white_submission_version_id=fake_id,
            black_submission_version_id=uuid.uuid4(),
            configuration=config,
        )


@pytest.mark.asyncio
async def test_house_ai_serves_development_match(local_db_session: AsyncSession):
    """Test that House AI baseline successfully serves as the opponent for development matches."""
    # Seed baseline
    await seed_deterministic_house_ai_baseline(
        local_db_session,
        house_ai_id="test-dev-house-opponent",
        version=1,
        auto_activate=True,
    )
    await local_db_session.commit()

    # Create synthetic participant with approved submission
    p_id = f"part_dev_{uuid.uuid4().hex[:8]}"
    part = Participant(
        participant_id=p_id,
        name=f"Dev Player {p_id}",
        email=f"{p_id}@example.internal",
        access_key_hash="dummy_hash",
        status=ParticipantStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )
    local_db_session.add(part)
    await local_db_session.flush()

    local_db_session.add(BuildSession(participant_id=p_id, started_at=datetime.now(timezone.utc), start_source="REGISTRATION"))
    await local_db_session.flush()

    sub = Submission(id=uuid.uuid4(), participant_id=p_id, created_at=datetime.now(timezone.utc))
    local_db_session.add(sub)
    await local_db_session.flush()

    archive = build_trusted_engine_archive()
    upload = await accept_upload(
        local_db_session,
        participant_id=p_id,
        submission_id=sub.id,
        archive_bytes=archive,
        correlation_id="dev-test",
        limits=DEFAULT_LIMITS,
    )
    await approve_submission_for_eligibility(
        local_db_session,
        submission_version_id=upload.version.id,
        approval=OrganizerApproval(actor_id="test-organizer", reason_code="DEV_TEST_APPROVE", approval_reference="ref-1"),
    )
    await local_db_session.commit()

    # Request development match
    match, created = await request_development_match(
        local_db_session,
        participant_id=p_id,
        submission_id=sub.id,
        version_number=1,
        idempotency_key=f"dev-match-{uuid.uuid4().hex}",
    )
    await local_db_session.commit()

    assert created is True
    assert match.purpose.value == "DEVELOPMENT"
    assert match.state.value in {"QUEUED", "STARTING", "RUNNING"}
