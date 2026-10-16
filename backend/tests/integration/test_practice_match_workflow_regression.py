from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import ArtifactState, SubmissionVersion
from app.models.event import DevelopmentMatchPolicy
from app.models.house_ai import EventHouseAISelection
from app.models.match import Match, MatchPurpose, MatchSideColor, MatchState
from app.services.development_matches import calculate_policy_digest
from app.services.house_ai_configuration import (
    DEVELOPMENT_POLICY_ID,
    DEVELOPMENT_POLICY_VERSION,
    activate_house_ai_version,
    register_and_freeze_house_ai_version,
)
from app.services.house_ai_seeding import MAXIMUM_DIFFICULTY_HOUSE_AI_ID, seed_all_house_ais
from app.services.submissions import OrganizerApproval, approve_submission_for_eligibility
from app.tournaments.service import get_tournament_leaderboard
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


async def _setup_policy_and_house_ai(db: AsyncSession) -> str:
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
    await seed_all_house_ais(db)
    await db.commit()
    return MAXIMUM_DIFFICULTY_HOUSE_AI_ID


async def _create_participant_with_submission(
    client: httpx.AsyncClient, db: AsyncSession, *, name: str | None = None, approve: bool = True
) -> tuple[str, dict[str, str], SubmissionVersion]:
    p_name = name or f"User_{uuid.uuid4().hex[:8]}"
    reg = await client.post("/api/v1/participants/register", json={"name": p_name})
    assert reg.status_code == 201
    creds = reg.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={"participant_id": creds["participant_id"], "access_key": creds["access_key"]},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    sub = await client.post("/api/v1/submissions", headers=headers)
    assert sub.status_code == 201
    sub_id = sub.json()["submission_id"]

    uploaded = await client.post(
        f"/api/v1/submissions/{sub_id}/versions",
        content=_valid_tar(source=b"# test submission\n"),
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert uploaded.status_code == 201
    version_id = uuid.UUID(uploaded.json()["version"]["id"])
    if approve:
        version = await approve_submission_for_eligibility(
            db,
            submission_version_id=version_id,
            approval=OrganizerApproval(
                actor_id="test-reviewer",
                reason_code="MANUAL_REVIEW_PASSED",
                approval_reference=f"test-appr-{uuid.uuid4().hex[:8]}",
            ),
        )
    else:
        version = await db.get(SubmissionVersion, version_id)
        assert version is not None
    return creds["participant_id"], headers, version


@pytest.mark.integration
async def test_practice_match_regression_workflow(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Verifies BUG-PRAC-001 requirements:
    1. Authenticated participant can select submission version and House AI (Maximum Difficulty).
    2. Selected side color (WHITE / BLACK) is respected.
    3. Match is created in QUEUED state (not running) accurately disclosing execution reality.
    4. Participant token isolation: Participant B cannot query or launch Participant A's submission.
    5. Anonymous / unauthorized requests are rejected (401).
    6. Non-existent House AI ID is rejected (404).
    7. Practice / development matches never pollute Swiss tournament leaderboard.
    """
    house_ai_id = await _setup_policy_and_house_ai(db_session)
    p1_id, p1_headers, p1_version = await _create_participant_with_submission(api_client, db_session)
    p2_id, p2_headers, p2_version = await _create_participant_with_submission(api_client, db_session)

    # 1. Unauthenticated request rejected
    anon_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={
            "submission_id": str(p1_version.submission_id),
            "version": p1_version.version,
            "house_ai_id": house_ai_id,
            "side": "WHITE",
        },
    )
    assert anon_resp.status_code == 401

    # 2. Participant B cannot use Participant A's submission
    cross_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={
            "submission_id": str(p1_version.submission_id),
            "version": p1_version.version,
            "house_ai_id": house_ai_id,
            "side": "WHITE",
        },
        headers={**p2_headers, "Idempotency-Key": f"cross-{uuid.uuid4().hex}"},
    )
    assert cross_resp.status_code == 404

    # 3. Non-existent House AI ID is rejected
    invalid_house = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={
            "submission_id": str(p1_version.submission_id),
            "version": p1_version.version,
            "house_ai_id": "non-existent-house-ai",
            "side": "WHITE",
        },
        headers={**p1_headers, "Idempotency-Key": f"invhouse-{uuid.uuid4().hex}"},
    )
    assert invalid_house.status_code == 404

    # 4. Valid creation as WHITE against Maximum Difficulty House AI
    white_match_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={
            "submission_id": str(p1_version.submission_id),
            "version": p1_version.version,
            "house_ai_id": house_ai_id,
            "side": "WHITE",
        },
        headers={**p1_headers, "Idempotency-Key": f"white-{uuid.uuid4().hex}"},
    )
    assert white_match_resp.status_code == 201, white_match_resp.text
    match_data = white_match_resp.json()
    assert match_data["state"] == MatchState.QUEUED.value
    assert match_data["purpose"] == MatchPurpose.DEVELOPMENT.value
    assert match_data["house_ai"]["house_ai_id"] == house_ai_id
    assert match_data["participant"]["color"] == "WHITE"
    assert match_data["house_ai"]["color"] == "BLACK"

    # 5. Participant 1 can query their created match
    detail_resp = await api_client.get(
        f"/api/v1/participants/me/development-matches/{match_data['match_id']}",
        headers=p1_headers,
    )
    assert detail_resp.status_code == 200
    assert detail_resp.json()["state"] == "QUEUED"

    # 6. Participant 2 cannot query Participant 1's match
    p2_query = await api_client.get(
        f"/api/v1/participants/me/development-matches/{match_data['match_id']}",
        headers=p2_headers,
    )
    assert p2_query.status_code == 404

    # 7. Check tournament standings: practice match has 0 effect on leaderboard
    leaderboard = await get_tournament_leaderboard(db_session)
    assert leaderboard.total_matches == 0
    p1_entry = next((e for e in leaderboard.standings if e.participant_id == p1_id), None)
    if p1_entry:
        assert p1_entry.matches_played == 0
        assert p1_entry.points == 0.0
