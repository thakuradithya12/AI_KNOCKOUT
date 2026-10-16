"""Regressions for organizer-run trusted House AI duels.

* A queued duel whose dispatched jobs expired must still run. ``execution_expires_at``
  is immutable (ORM guard and PostgreSQL trigger), so the runner retires the stale job
  and creates a fresh one instead of extending the old expiry.
* A duel that reaches the ply budget is aborted instead of being left RUNNING, which
  would block every later duel for the same runner.
* Organizer duels never borrow a real participant as their owner.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.match import Match, MatchFailure, MatchPurpose, MatchState
from app.models.participant import Participant, ParticipantStatus
from app.models.worker import ExecutionJobStatus, WorkerExecutionJob
from app.services.development_matches import (
    request_trusted_house_development_match,
    run_trusted_development_match,
)
from app.services.house_ai_seeding import seed_all_house_ais


@pytest.mark.integration
async def test_trusted_duel_runs_after_queued_jobs_expire(db_session: AsyncSession) -> None:
    await seed_all_house_ais(db_session)
    participant = Participant(
        participant_id=f"trusted-runner-{uuid.uuid4().hex[:12]}",
        name="Trusted House AI Runner",
        status=ParticipantStatus.ACTIVE,
        access_key_hash=hashlib.sha256(b"dummy-trusted-key").hexdigest(),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(participant)
    await db_session.commit()

    match, created = await request_trusted_house_development_match(
        db_session,
        participant_id=participant.participant_id,
        idempotency_key=f"expired-duel-{uuid.uuid4().hex}",
        white_house_ai_id="house-ai-benchmark-baseline",
        black_house_ai_id="house-ai-maximum-difficulty",
    )
    assert created is True
    match_id = match.id
    await db_session.commit()

    stale_ids = (
        await db_session.execute(
            select(WorkerExecutionJob.execution_id).where(WorkerExecutionJob.match_id == match_id)
        )
    ).scalars().all()
    assert len(stale_ids) == 2

    # Simulate the dispatch lease window elapsing before the operator runs the duel
    # (bypasses the ORM immutability guard on purpose; SQLite has no triggers).
    await db_session.execute(
        update(WorkerExecutionJob)
        .where(WorkerExecutionJob.execution_id.in_(stale_ids))
        .values(execution_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    db_session.expire_all()

    completed = await run_trusted_development_match(db_session, match_id=match_id, max_plies=50)
    assert completed.state == MatchState.COMPLETED

    jobs = (
        await db_session.execute(
            select(WorkerExecutionJob).where(WorkerExecutionJob.match_id == match_id)
        )
    ).scalars().all()
    by_id = {job.execution_id: job for job in jobs}
    assert all(by_id[old].status == ExecutionJobStatus.EXPIRED for old in stale_ids)
    replacements = [job for job in jobs if job.execution_id not in stale_ids]
    assert len(replacements) == 2


async def _runner(db_session: AsyncSession) -> str:
    participant = Participant(
        participant_id=f"trusted-runner-{uuid.uuid4().hex[:12]}",
        name="Trusted House AI Runner",
        status=ParticipantStatus.ACTIVE,
        access_key_hash=hashlib.sha256(b"dummy-trusted-key").hexdigest(),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(participant)
    await db_session.commit()
    return participant.participant_id


@pytest.mark.integration
async def test_trusted_duel_ply_limit_aborts_instead_of_leaving_match_running(
    db_session: AsyncSession,
) -> None:
    await seed_all_house_ais(db_session)
    runner_id = await _runner(db_session)

    match, _ = await request_trusted_house_development_match(
        db_session,
        participant_id=runner_id,
        idempotency_key=f"ply-cap-{uuid.uuid4().hex}",
        white_house_ai_id="house-ai-maximum-difficulty",
        black_house_ai_id="AIK-HOUSE-001",
    )
    stopped = await run_trusted_development_match(db_session, match_id=match.id, max_plies=3)
    assert stopped.state == MatchState.ABORTED
    assert stopped.failure_reason == MatchFailure.EXECUTION_TIMEOUT.value
    assert stopped.completed_at is not None

    # The runner is free to start the next duel.
    _, created = await request_trusted_house_development_match(
        db_session,
        participant_id=runner_id,
        idempotency_key=f"ply-cap-next-{uuid.uuid4().hex}",
        white_house_ai_id="house-ai-maximum-difficulty",
        black_house_ai_id="AIK-HOUSE-001",
    )
    assert created is True


@pytest.mark.integration
async def test_admin_duel_does_not_borrow_a_real_participant(
    api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await seed_all_house_ais(db_session)
    await db_session.commit()
    real = await api_client.post(
        "/api/v1/participants/register", json={"name": f"Real_{uuid.uuid4().hex[:8]}"}
    )
    assert real.status_code == 201
    real_id = real.json()["participant_id"]

    resp = await api_client.post(
        "/api/v1/admin/development-matches/house-duel",
        json={
            "white_house_ai_id": "house-ai-benchmark-baseline",
            "black_house_ai_id": "AIK-HOUSE-001",
        },
        headers={"X-Organizer-Secret": Settings().secret_key},
    )
    assert resp.status_code == 201, resp.text

    owners = (
        await db_session.execute(
            select(Match.idempotency_participant_id).where(
                Match.purpose == MatchPurpose.DEVELOPMENT
            )
        )
    ).scalars().all()
    assert owners == ["system-trusted-runner"]
    assert real_id not in owners


@pytest.mark.integration
async def test_public_spectator_endpoint_hides_development_matches(
    api_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await seed_all_house_ais(db_session)
    await db_session.commit()
    resp = await api_client.post(
        "/api/v1/admin/development-matches/house-duel",
        json={
            "white_house_ai_id": "house-ai-benchmark-baseline",
            "black_house_ai_id": "AIK-HOUSE-001",
        },
        headers={"X-Organizer-Secret": Settings().secret_key},
    )
    assert resp.status_code == 201, resp.text
    match_id = resp.json()["match_id"]

    public = await api_client.get(f"/api/v1/tournaments/matches/{match_id}")
    assert public.status_code == 404
    assert public.json()["detail"] == "TOURNAMENT_MATCH_NOT_FOUND"
