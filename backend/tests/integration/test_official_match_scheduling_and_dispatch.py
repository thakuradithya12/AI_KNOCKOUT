"""Integration tests for official match scheduling, worker job dispatching, and spectator inspection.

Verifies:
1. schedule_official_match with dispatch_jobs=True atomically creates the official match and dispatches 2 WorkerExecutionJob rows.
2. Admin endpoint POST /api/v1/admin/tournaments/schedule-match schedules official match and dispatches jobs.
3. Admin endpoint POST /api/v1/admin/matches/{match_id}/dispatch explicitly dispatches jobs for queued matches idempotently.
4. Spectator endpoint GET /api/v1/tournaments/matches/{match_id} correctly retrieves the match state and player identities.
"""

from __future__ import annotations

import uuid
import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.controller import MatchConfiguration
from app.models.match import Match, MatchPurpose, MatchState
from app.models.worker import ExecutionJobStatus, ExecutionKind, WorkerExecutionJob
from app.tournaments.service import schedule_official_match
from tests.integration.test_admin_match_result_recording import _register_and_qualify_participant

ADMIN_HEADERS = {"X-Organizer-Secret": "isolated-test-only-secret-key-0123456789"}


@pytest.mark.asyncio
async def test_schedule_official_match_with_dispatch_jobs(
    db_session: AsyncSession,
    api_client: httpx.AsyncClient,
):
    """schedule_official_match(..., dispatch_jobs=True) creates match and WorkerExecutionJob records."""
    p1 = await _register_and_qualify_participant(api_client, "DispatchAlpha", ADMIN_HEADERS)
    p2 = await _register_and_qualify_participant(api_client, "DispatchBeta", ADMIN_HEADERS)

    import hashlib

    cfg = MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=180_000,
        increment_ms=2_000,
        move_timeout_ms=3_000,
        match_seed=hashlib.sha256(b"dispatch-test-seed-1").hexdigest(),
    )

    match, (white_side, black_side) = await schedule_official_match(
        db_session,
        white_submission_version_id=uuid.UUID(p1["version_id"]),
        black_submission_version_id=uuid.UUID(p2["version_id"]),
        configuration=cfg,
        dispatch_jobs=True,
        execution_mode="MODAL_AI_EXECUTION",
    )

    assert match.state == MatchState.QUEUED
    assert match.purpose == MatchPurpose.OFFICIAL

    # Query WorkerExecutionJob records for this match
    jobs = (
        await db_session.execute(
            select(WorkerExecutionJob).where(WorkerExecutionJob.match_id == match.id)
        )
    ).scalars().all()

    assert len(jobs) == 2
    for job in jobs:
        assert job.status == ExecutionJobStatus.QUEUED
        assert job.execution_mode == "MODAL_AI_EXECUTION"
        assert job.execution_kind == ExecutionKind.PARTICIPANT
        assert job.match_id == match.id
        assert job.participant_id in {p1["participant_id"], p2["participant_id"]}


@pytest.mark.asyncio
async def test_admin_schedule_and_dispatch_endpoints(
    db_session: AsyncSession,
    api_client: httpx.AsyncClient,
):
    """Admin endpoints schedule tournament match with jobs, and dispatch endpoint works idempotently."""
    p1 = await _register_and_qualify_participant(api_client, "AdminSchedAlpha", ADMIN_HEADERS)
    p2 = await _register_and_qualify_participant(api_client, "AdminSchedBeta", ADMIN_HEADERS)

    # 1. Schedule via admin endpoint with dispatch_jobs=True (default)
    resp = await api_client.post(
        "/api/v1/admin/tournaments/schedule-match",
        headers=ADMIN_HEADERS,
        json={
            "white_submission_version_id": p1["version_id"],
            "black_submission_version_id": p2["version_id"],
            "initial_clock_ms": 300_000,
            "increment_ms": 0,
            "move_timeout_ms": 2_000,
            "dispatch_jobs": True,
            "execution_mode": "MODAL_AI_EXECUTION",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "QUEUED"
    match_id = uuid.UUID(data["match_id"])

    jobs = (
        await db_session.execute(
            select(WorkerExecutionJob).where(WorkerExecutionJob.match_id == match_id)
        )
    ).scalars().all()
    assert len(jobs) == 2

    # 2. Call admin dispatch endpoint idempotently on the same match
    disp_resp = await api_client.post(
        f"/api/v1/admin/matches/{match_id}/dispatch",
        headers=ADMIN_HEADERS,
        json={"execution_mode": "MODAL_AI_EXECUTION"},
    )
    assert disp_resp.status_code == 200, disp_resp.text
    disp_data = disp_resp.json()
    assert disp_data["status"] == "DISPATCHED"
    assert len(disp_data["job_ids"]) == 2

    # 3. Verify spectator match detail endpoint returns proper representation
    spec_resp = await api_client.get(f"/api/v1/tournaments/matches/{match_id}")
    assert spec_resp.status_code == 200, spec_resp.text
    spec_data = spec_resp.json()
    assert spec_data["match_id"] == str(match_id)
    assert spec_data["white_name"] == "AdminSchedAlpha"
    assert spec_data["black_name"] == "AdminSchedBeta"
    assert spec_data["state"] == "QUEUED"
    assert spec_data["current_fen"] == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert spec_data["moves"] == []
