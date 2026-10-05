from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.controller import MatchExecutionBinding, MatchSideIdentity
from app.models.house_ai import HouseAI, HouseAIArtifact, HouseAIVersion
from app.models.match import (
    HouseAIMatchSideBinding,
    MatchSide,
    MatchSideColor,
    MatchSideKind,
    ParticipantMatchSideBinding,
)
from app.models.worker import ExecutionJobStatus, ExecutionKind, WorkerExecutionJob


def _job(**overrides) -> WorkerExecutionJob:
    values = {
        "id": uuid.uuid4(),
        "execution_id": uuid.uuid4(),
        "artifact_sha256": "a" * 64,
        "immutable_artifact_ref": "artifact-ref",
        "runtime_profile": "runtime-v1",
        "protocol_version": "aik-jsonl/1",
        "execution_mode": "MOCK_PROTOCOL_ONLY",
        "initial_state": {},
        "resource_policy": {},
        "execution_expires_at": datetime.now(timezone.utc) + timedelta(minutes=2),
        "status": ExecutionJobStatus.QUEUED,
        "fencing_token": 0,
        "last_sequence": 0,
    }
    values.update(overrides)
    return WorkerExecutionJob(**values)


@pytest.mark.integration
async def test_house_ai_side_persists_exact_typed_identity(db_session: AsyncSession):
    digest = "a" * 64
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            HouseAI(house_ai_id="organizer-ai", created_at=now),
            HouseAIArtifact(
                artifact_sha256=digest,
                artifact_size=12,
                storage_ref="house-ai-artifact-ref",
                created_at=now,
            ),
            HouseAIVersion(
                house_ai_id="organizer-ai",
                version=1,
                artifact_sha256=digest,
                artifact_size=12,
                protocol_version="aik-jsonl/1",
                runtime_profile="runtime-v1",
                runtime_identity_sha256="b" * 64,
                rules_version="rules-v1",
                policy_version="policy-v1",
                policy_digest="c" * 64,
                created_at=now,
                frozen_at=now,
            ),
        ]
    )
    side = MatchSide(
        id=uuid.uuid4(),
        match_id=uuid.uuid4(),
        color=MatchSideColor.WHITE,
        kind=MatchSideKind.HOUSE_AI,
        protocol_version="aik-jsonl/1",
        runtime_profile="runtime-v1",
        created_at=now,
        house_ai_binding=HouseAIMatchSideBinding(
            house_ai_id="organizer-ai", house_ai_version=1, artifact_sha256=digest
        ),
    )
    db_session.add(side)
    await db_session.flush()
    persisted = await db_session.scalar(select(MatchSide).where(MatchSide.id == side.id))
    assert persisted is not None
    assert persisted.kind == MatchSideKind.HOUSE_AI
    assert persisted.house_ai_binding.house_ai_id == "organizer-ai"
    assert persisted.house_ai_binding.house_ai_version == 1
    assert persisted.artifact_sha256 == digest


@pytest.mark.integration
async def test_match_side_rejects_missing_or_dual_typed_identity(db_session: AsyncSession):
    now = datetime.now(timezone.utc)
    missing = MatchSide(
        id=uuid.uuid4(), match_id=uuid.uuid4(), color=MatchSideColor.WHITE,
        kind=MatchSideKind.PARTICIPANT, protocol_version="aik-jsonl/1",
        runtime_profile="runtime-v1", created_at=now,
    )
    db_session.add(missing)
    with pytest.raises(ValueError, match="MATCH_SIDE_TYPED_IDENTITY_INVALID"):
        await db_session.flush()
    await db_session.rollback()

    side_id = uuid.uuid4()
    dual = MatchSide(
        id=side_id, match_id=uuid.uuid4(), color=MatchSideColor.WHITE,
        kind=MatchSideKind.PARTICIPANT, protocol_version="aik-jsonl/1",
        runtime_profile="runtime-v1", created_at=now,
        participant_binding=ParticipantMatchSideBinding(
            match_side_id=side_id, participant_id="part_fixture",
            submission_id=uuid.uuid4(), submission_version_id=uuid.uuid4(),
            artifact_sha256="a" * 64,
        ),
        house_ai_binding=HouseAIMatchSideBinding(
            match_side_id=side_id, house_ai_id="organizer-ai", house_ai_version=1,
            artifact_sha256="b" * 64,
        ),
    )
    db_session.add(dual)
    with pytest.raises(ValueError, match="MATCH_SIDE_TYPED_IDENTITY_INVALID"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_worker_execution_job_requires_one_kind_matching_subject(db_session: AsyncSession):
    digest = "a" * 64
    house_job = _job(
        execution_kind=ExecutionKind.HOUSE_AI,
        house_ai_id="organizer-ai",
        house_ai_version=1,
    )
    db_session.add(house_job)
    await db_session.flush()
    assert house_job.execution_kind == ExecutionKind.HOUSE_AI
    await db_session.rollback()

    invalid = _job(
        execution_kind=ExecutionKind.HOUSE_AI,
        participant_id="part_fixture",
        submission_id=uuid.uuid4(),
        submission_version=1,
        submission_version_id=uuid.uuid4(),
        house_ai_id="organizer-ai",
        house_ai_version=1,
        artifact_sha256=digest,
    )
    db_session.add(invalid)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


def test_controller_binding_and_side_identity_are_typed():
    match_id, side_id, submission_version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    side = MatchSideIdentity(
        side_id=side_id,
        match_id=match_id,
        color=MatchSideColor.WHITE,
        participant_id=None,
        submission_id=None,
        submission_version_id=None,
        artifact_digest="a" * 64,
        protocol_version="aik-jsonl/1",
        runtime_profile="runtime-v1",
        kind=MatchSideKind.HOUSE_AI,
        house_ai_id="organizer-ai",
        house_ai_version=1,
    )
    binding = MatchExecutionBinding(
        binding_id=uuid.uuid4(),
        match_id=match_id,
        side_id=side_id,
        execution_id=uuid.uuid4(),
        submission_version_id=None,
        artifact_digest="a" * 64,
        worker_id="worker-fixture",
        lease_id=uuid.uuid4(),
        fencing_token=1,
        attempt_number=1,
        kind=MatchSideKind.HOUSE_AI,
        house_ai_id="organizer-ai",
        house_ai_version=1,
    )
    assert binding.matches_side(side)
    with pytest.raises(ValueError, match="EXECUTION_BINDING_IDENTITY_INVALID"):
        MatchExecutionBinding(
            **{**binding.__dict__, "submission_version_id": submission_version_id}
        )
    with pytest.raises(ValueError, match="MATCH_SIDE_IDENTITY_INVALID"):
        MatchSideIdentity(
            side_id=uuid.uuid4(), match_id=match_id, color=MatchSideColor.WHITE,
            participant_id="part_fixture", submission_id=uuid.uuid4(),
            submission_version_id=uuid.uuid4(), artifact_digest="a" * 64,
            protocol_version="aik-jsonl/1", runtime_profile="runtime-v1",
            kind=MatchSideKind.HOUSE_AI, house_ai_id="organizer-ai", house_ai_version=1,
        )


def test_participant_actor_identity_is_distinct_from_submission_version():
    match_id = uuid.uuid4()
    common = {
        "match_id": match_id,
        "participant_id": "part_fixture",
        "artifact_digest": "a" * 64,
        "protocol_version": "aik-jsonl/1",
        "runtime_profile": "runtime-v1",
    }
    first = MatchSideIdentity(
        side_id=uuid.uuid4(), color=MatchSideColor.WHITE,
        submission_id=uuid.uuid4(), submission_version_id=uuid.uuid4(), **common,
    )
    second = MatchSideIdentity(
        side_id=uuid.uuid4(), color=MatchSideColor.BLACK,
        submission_id=uuid.uuid4(), submission_version_id=uuid.uuid4(), **common,
    )
    assert first.actor_identity == second.actor_identity
