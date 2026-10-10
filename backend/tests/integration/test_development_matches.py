from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import ArtifactState, SubmissionVersion
from app.models.event import DevelopmentMatchPolicy
from app.models.house_ai import (
    EventHouseAISelection,
    HouseAIArtifact,
)
from app.models.match import (
    HouseAIMatchSideBinding,
    Match,
    MatchPurpose,
    MatchSide,
    MatchSideKind,
    MatchState,
)
from app.models.worker import (
    ExecutionFailureClass,
    ExecutionJobStatus,
    ExecutionKind,
    Worker,
    WorkerExecutionJob,
    WorkerStatus,
)
from app.services.development_matches import calculate_policy_digest
from app.services.house_ai_configuration import (
    DEVELOPMENT_POLICY_ID,
    DEVELOPMENT_POLICY_VERSION,
    activate_house_ai_version,
    register_and_freeze_house_ai_version,
)
from app.services.submissions import OrganizerApproval, approve_submission_for_eligibility
from app.workers.service import (
    acknowledge_job,
    claim_job,
    dispatch_development_match_jobs,
    download_authorized_artifact,
    finalize_execution_job,
    issue_artifact_capability,
    submit_cleanup,
)
from tests.unit.test_submission_validation import _valid_tar


def _policy_document() -> dict:
    return {
        "policy_id": DEVELOPMENT_POLICY_ID,
        "version": DEVELOPMENT_POLICY_VERSION,
        "policy_version": DEVELOPMENT_POLICY_VERSION,
        "protocol_version": "aik-jsonl/1",
        "runtime_profile": "python-3.12",
        "rules_version": "standard-chess/python-chess-1.11.2",
        "initial_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "initial_clock_ms": 300000,
        "increment_ms": 0,
        "move_timeout_ms": 2000,
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


async def _configured_house_ai(db: AsyncSession) -> str:
    document = _policy_document()
    policy_digest = calculate_policy_digest(document)
    db.add(
        DevelopmentMatchPolicy(
            policy_id=DEVELOPMENT_POLICY_ID,
            version=DEVELOPMENT_POLICY_VERSION,
            policy_digest=policy_digest,
            policy_document=document,
            frozen_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()
    house_ai_id = f"house-{uuid.uuid4().hex[:16]}"
    await register_and_freeze_house_ai_version(
        db,
        house_ai_id=house_ai_id,
        version=1,
        archive_bytes=b"trusted inert House AI fixture",
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        runtime_identity_sha256="c" * 64,
        rules_version="standard-chess/python-chess-1.11.2",
        policy_version=DEVELOPMENT_POLICY_VERSION,
        policy_digest=policy_digest,
        actor_id="test-organizer",
        reason_code="TEST_SETUP",
    )
    await activate_house_ai_version(
        db,
        house_ai_id=house_ai_id,
        version=1,
        actor_id="test-organizer",
        reason_code="TEST_SELECTION",
    )
    await db.commit()
    return house_ai_id


async def _eligible_submission(
    client: httpx.AsyncClient,
    db: AsyncSession,
    *,
    name: str | None = None,
    approve: bool = True,
) -> tuple[str, dict[str, str], SubmissionVersion]:
    registered = await client.post(
        "/api/v1/participants/register",
        json={"name": name or f"DevMatch_{uuid.uuid4().hex}"},
    )
    assert registered.status_code == 201
    credentials = registered.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "participant_id": credentials["participant_id"],
            "access_key": credentials["access_key"],
        },
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    submission = await client.post("/api/v1/submissions", headers=headers)
    assert submission.status_code == 201
    uploaded = await client.post(
        f"/api/v1/submissions/{submission.json()['submission_id']}/versions",
        content=_valid_tar(source=b"# inert test-only package\n"),
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert uploaded.status_code == 201
    version_id = uuid.UUID(uploaded.json()["version"]["id"])
    if approve:
        version = await approve_submission_for_eligibility(
            db,
            submission_version_id=version_id,
            approval=OrganizerApproval(
                actor_id="dev-match-test",
                reason_code="MANUAL_REVIEW_PASSED",
                approval_reference=f"dev-match-{uuid.uuid4().hex}",
            ),
        )
    else:
        version = await db.get(SubmissionVersion, version_id)
        assert version is not None
    return credentials["participant_id"], headers, version


@pytest.mark.integration
async def test_development_match_api_persists_exact_typed_identity_and_idempotency(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    house_ai_id = await _configured_house_ai(db_session)
    participant_id, headers, version = await _eligible_submission(api_client, db_session)
    body = {"submission_id": str(version.submission_id), "version": version.version}
    headers = {**headers, "Idempotency-Key": f"development-{uuid.uuid4().hex}"}

    unauthorized = await api_client.post("/api/v1/participants/me/development-matches", json=body)
    assert unauthorized.status_code == 401
    first = await api_client.post(
        "/api/v1/participants/me/development-matches", json=body, headers=headers
    )
    assert first.status_code == 201, first.text
    data = first.json()
    assert data["state"] == MatchState.QUEUED.value
    assert data["purpose"] == MatchPurpose.DEVELOPMENT.value
    assert data["participant"]["submission_version_id"] == str(version.id)
    assert data["participant"]["artifact_sha256"] == version.artifact_sha256
    assert data["house_ai"]["house_ai_id"] == house_ai_id
    assert data["policy"]["id"] == DEVELOPMENT_POLICY_ID
    assert data["initial_fen"] == _policy_document()["initial_fen"]
    assert data["initial_clock_ms"] == 300000 and data["increment_ms"] == 0
    assert len(data["match_seed"]) == 64

    replay = await api_client.post(
        "/api/v1/participants/me/development-matches", json=body, headers=headers
    )
    assert replay.status_code == 200
    assert replay.json()["match_id"] == data["match_id"]
    assert replay.json()["idempotent_replay"] is True
    duplicate_active = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json=body,
        headers={**headers, "Idempotency-Key": f"active-{uuid.uuid4().hex}"},
    )
    assert duplicate_active.status_code == 409

    match = await db_session.get(Match, uuid.UUID(data["match_id"]))
    assert match is not None and match.policy_digest == data["policy"]["digest"]
    sides = (
        await db_session.execute(select(MatchSide).where(MatchSide.match_id == match.id))
    ).scalars().all()
    assert {side.kind for side in sides} == {MatchSideKind.PARTICIPANT, MatchSideKind.HOUSE_AI}
    house_binding = (
        await db_session.execute(
            select(HouseAIMatchSideBinding).where(
                HouseAIMatchSideBinding.match_side_id.in_([side.id for side in sides])
            )
        )
    ).scalar_one()
    assert house_binding.house_ai_id == house_ai_id
    assert await db_session.scalar(select(EventHouseAISelection.selection_id)) is not None

    listed = await api_client.get("/api/v1/participants/me/development-matches", headers=headers)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    inspected = await api_client.get(
        f"/api/v1/participants/me/development-matches/{data['match_id']}", headers=headers
    )
    assert inspected.status_code == 200

    other_id, other_headers, other_version = await _eligible_submission(api_client, db_session)
    assert other_id != participant_id
    cross_submission = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={
            "submission_id": str(other_version.submission_id),
            "version": other_version.version,
        },
        headers={**headers, "Idempotency-Key": f"cross-{uuid.uuid4().hex}"},
    )
    assert cross_submission.status_code == 404
    private = await api_client.get(
        f"/api/v1/participants/me/development-matches/{data['match_id']}",
        headers=other_headers,
    )
    assert private.status_code == 404


@pytest.mark.integration
async def test_development_match_fails_closed_without_explicit_house_selection(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    document = _policy_document()
    db_session.add(
        DevelopmentMatchPolicy(
            policy_id=DEVELOPMENT_POLICY_ID,
            version=DEVELOPMENT_POLICY_VERSION,
            policy_digest=calculate_policy_digest(document),
            policy_document=document,
            frozen_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    _, headers, version = await _eligible_submission(api_client, db_session)
    response = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": str(version.submission_id), "version": version.version},
        headers={**headers, "Idempotency-Key": f"no-house-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 503
    assert await db_session.scalar(select(Match.id)) is None


@pytest.mark.integration
async def test_development_match_rejects_ineligible_submission(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    await _configured_house_ai(db_session)
    _, headers, version = await _eligible_submission(api_client, db_session, approve=False)
    assert version.state != ArtifactState.ELIGIBLE
    response = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": str(version.submission_id), "version": version.version},
        headers={**headers, "Idempotency-Key": f"ineligible-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 409
    assert await db_session.scalar(select(Match.id)) is None


@pytest.mark.integration
async def test_corrupt_house_ai_bytes_and_path_reference_fail_closed(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    house_ai_id = await _configured_house_ai(db_session)
    _, headers, version = await _eligible_submission(api_client, db_session)
    selection = await db_session.scalar(
        select(EventHouseAISelection).where(EventHouseAISelection.deactivated_at.is_(None))
    )
    assert selection is not None and selection.house_ai_id == house_ai_id
    artifact = await db_session.get(HouseAIArtifact, selection.artifact_sha256)
    assert artifact is not None
    # Simulate persisted object corruption inside this disposable test database only.
    await db_session.execute(
        text("UPDATE house_ai_artifacts SET storage_ref=:ref WHERE artifact_sha256=:digest"),
        {"ref": "../../participant-artifact", "digest": artifact.artifact_sha256},
    )
    await db_session.commit()
    response = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": str(version.submission_id), "version": version.version},
        headers={**headers, "Idempotency-Key": f"corrupt-house-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 503
    assert await db_session.scalar(select(Match.id)) is None


@pytest.mark.integration
async def test_typed_worker_jobs_keep_house_ai_bytes_scoped_and_use_distinct_workers(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    house_ai_id = await _configured_house_ai(db_session)
    _, headers, version = await _eligible_submission(api_client, db_session)
    requested = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": str(version.submission_id), "version": version.version},
        headers={**headers, "Idempotency-Key": f"jobs-{uuid.uuid4().hex}"},
    )
    assert requested.status_code == 201
    match = await db_session.get(Match, uuid.UUID(requested.json()["match_id"]))
    assert match is not None
    jobs = (
        await db_session.execute(
            select(WorkerExecutionJob).where(WorkerExecutionJob.match_id == match.id)
        )
    ).scalars().all()
    assert len(jobs) == 2
    participant_job = next(job for job in jobs if job.execution_kind == ExecutionKind.PARTICIPANT)
    house_job = next(job for job in jobs if job.execution_kind == ExecutionKind.HOUSE_AI)
    assert {job.execution_id for job in await dispatch_development_match_jobs(
        db_session, match_id=match.id
    )} == {participant_job.execution_id, house_job.execution_id}
    assert len((await db_session.execute(
        select(WorkerExecutionJob).where(WorkerExecutionJob.match_id == match.id)
    )).scalars().all()) == 2
    assert participant_job.match_id == house_job.match_id == match.id
    assert participant_job.execution_kind == ExecutionKind.PARTICIPANT
    assert participant_job.submission_version_id == version.id
    assert participant_job.artifact_sha256 == version.artifact_sha256
    assert house_job.execution_kind == ExecutionKind.HOUSE_AI
    assert house_job.house_ai_id == house_ai_id and house_job.submission_version_id is None
    house_side_binding = await db_session.scalar(
        select(HouseAIMatchSideBinding)
        .join(MatchSide, MatchSide.id == HouseAIMatchSideBinding.match_side_id)
        .where(MatchSide.match_id == match.id, MatchSide.kind == MatchSideKind.HOUSE_AI)
    )
    assert house_side_binding is not None
    assert house_job.house_ai_version == house_side_binding.house_ai_version
    assert house_job.artifact_sha256 == house_side_binding.artifact_sha256
    assert participant_job.protocol_version == house_job.protocol_version == match.protocol_version
    assert participant_job.runtime_profile == house_job.runtime_profile == match.runtime_profile
    assert participant_job.initial_state == house_job.initial_state
    assert participant_job.resource_policy == house_job.resource_policy
    assert participant_job.execution_mode == house_job.execution_mode == "KATA_AI_EXECUTION"
    assert house_job.immutable_artifact_ref.startswith("house-ai://sha256/")

    worker_a = Worker(
        worker_id=f"worker-{uuid.uuid4().hex[:20]}",
        status=WorkerStatus.ACTIVE,
        capabilities={
            "sandbox_backend": "KATA",
            "protocol_versions": ["aik-jsonl/1"],
            "execution_modes": ["KATA_AI_EXECUTION"],
        },
        security_state="APPROVED",
    )
    worker_b = Worker(
        worker_id=f"worker-{uuid.uuid4().hex[:20]}",
        status=WorkerStatus.ACTIVE,
        capabilities={
            "sandbox_backend": "KATA",
            "protocol_versions": ["aik-jsonl/1"],
            "execution_modes": ["KATA_AI_EXECUTION"],
        },
        security_state="APPROVED",
    )
    db_session.add_all([worker_a, worker_b])
    await db_session.commit()

    async def ack(worker, job):
        await acknowledge_job(
            db_session,
            worker=worker,
            execution_id=job.execution_id,
            lease_id=job.lease_id,
            fencing_token=job.fencing_token,
            sequence=1,
            accepted=True,
        )

    async def verify_house_transfer(worker, job):
        raw_capability, capability = await issue_artifact_capability(
            db_session,
            worker=worker,
            execution_id=job.execution_id,
            lease_id=job.lease_id,
            fencing_token=job.fencing_token,
            sequence=2,
        )
        assert capability.execution_kind == ExecutionKind.HOUSE_AI
        assert capability.house_ai_id == house_ai_id and capability.house_ai_version == 1
        content, digest = await download_authorized_artifact(
            db_session,
            worker=worker,
            execution_id=job.execution_id,
            lease_id=job.lease_id,
            fencing_token=job.fencing_token,
            sequence=3,
            raw_capability=raw_capability,
        )
        assert content == b"trusted inert House AI fixture"
        assert digest == house_job.artifact_sha256

    claimed_a = await claim_job(db_session, worker=worker_a)
    assert claimed_a is not None
    await ack(worker_a, claimed_a)
    if claimed_a.execution_kind == ExecutionKind.HOUSE_AI:
        await verify_house_transfer(worker_a, claimed_a)
        cleanup_sequence = 4
    else:
        cleanup_sequence = 2
    await submit_cleanup(
        db_session,
        worker=worker_a,
        execution_id=claimed_a.execution_id,
        lease_id=claimed_a.lease_id,
        fencing_token=claimed_a.fencing_token,
        sequence=cleanup_sequence,
        cleanup_verified=True,
        final_status=ExecutionJobStatus.FAILED,
        failure_class=ExecutionFailureClass.WORKER_FAILURE,
    )
    await finalize_execution_job(
        db_session,
        execution_id=claimed_a.execution_id,
        final_status=ExecutionJobStatus.FAILED,
        actor_id="test-control-plane",
    )
    # A disposable worker cannot receive the other side of the same match.
    assert await claim_job(db_session, worker=worker_a) is None
    claimed_b = await claim_job(db_session, worker=worker_b)
    assert claimed_b is not None and claimed_b.execution_id != claimed_a.execution_id
    await ack(worker_b, claimed_b)
    if claimed_b.execution_kind == ExecutionKind.HOUSE_AI:
        await verify_house_transfer(worker_b, claimed_b)
    assert {claimed_a.execution_kind, claimed_b.execution_kind} == {
        ExecutionKind.PARTICIPANT,
        ExecutionKind.HOUSE_AI,
    }
