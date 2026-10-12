"""Gate 4 Tournament Simulation Test: Multi-participant tournament execution, match outcomes, tie-breakers, and standings."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.matches.controller import MatchConfiguration
from app.matches.service import create_match
from app.models.competition import ArtifactState
from app.models.match import (
    Match,
    MatchOutcome,
    MatchPurpose,
    MatchState,
)
from app.tournaments.service import (
    get_official_tournament_matches,
    get_tournament_bracket,
    get_tournament_leaderboard,
)
from tests.unit.test_submission_validation import _valid_tar


async def _setup_participant(api_client: httpx.AsyncClient, name: str) -> dict:
    reg = await api_client.post("/api/v1/participants/register", json={"name": name})
    assert reg.status_code == 201
    pid, key = reg.json()["participant_id"], reg.json()["access_key"]

    login = await api_client.post("/api/v1/auth/login", json={"participant_id": pid, "access_key": key})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    sub = await api_client.post("/api/v1/submissions", headers=headers)
    assert sub.status_code == 201
    sid = sub.json()["submission_id"]

    archive = _valid_tar(source=f"# {name} AI\n".encode())
    upload = await api_client.post(
        f"/api/v1/submissions/{sid}/versions",
        content=archive,
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert upload.status_code == 201
    vid = uuid.UUID(upload.json()["version"]["id"])
    v_num = upload.json()["version"]["version"]

    return {
        "participant_id": pid,
        "name": name,
        "token": token,
        "headers": headers,
        "submission_id": sid,
        "version_id": vid,
        "version_number": v_num,
    }


async def _complete_match(
    session: AsyncSession,
    match: Match,
    outcome: MatchOutcome,
    reason: str,
) -> None:
    now = datetime.now(timezone.utc)
    match.state = MatchState.STARTING
    await session.flush()
    match.state = MatchState.RUNNING
    match.started_at = now
    await session.flush()
    match.state = MatchState.COMPLETED
    match.result_outcome = outcome
    match.result_reason = reason
    match.completed_at = now
    await session.commit()


@pytest.mark.asyncio
async def test_four_participant_tournament_simulation(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # 1. Setup 4 participants
    p_a = await _setup_participant(api_client, "Team_Alpha")
    p_b = await _setup_participant(api_client, "Team_Beta")
    p_c = await _setup_participant(api_client, "Team_Gamma")
    p_d = await _setup_participant(api_client, "Team_Delta")
    participants = [p_a, p_b, p_c, p_d]

    # 2. Admin approves all for eligibility
    for p in participants:
        res = await api_client.post(
            "/api/v1/admin/submissions/approve",
            headers=admin_headers,
            json={"submission_version_id": str(p["version_id"]), "reviewer_id": "tournament-director"},
        )
        assert res.status_code == 200

    # 3. All participants select official submissions
    for p in participants:
        sel = await api_client.put(
            "/api/v1/participants/me/official-submission",
            headers=p["headers"],
            json={"submission_id": p["submission_id"], "version": p["version_number"]},
        )
        assert sel.status_code == 200

    # 4. Freeze official submissions
    frz = await api_client.post(
        "/api/v1/admin/freeze",
        headers=admin_headers,
        json={"reason_code": "TOURNAMENT_STARTED", "actor_id": "admin"},
    )
    assert frz.status_code == 200

    # 5. Schedule Semifinal 1: Alpha (White) vs Beta (Black)
    config = MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=300_000,
        increment_ms=0,
        move_timeout_ms=2_000,
        match_seed="1" * 64,
    )

    m1, sides1 = await create_match(
        db_session,
        white_submission_version_id=p_a["version_id"],
        black_submission_version_id=p_b["version_id"],
        configuration=config,
        purpose=MatchPurpose.OFFICIAL,
    )
    # Simulate match 1 completion: Alpha wins by checkmate
    await _complete_match(db_session, m1, MatchOutcome.WHITE_WIN, "CHECKMATE")

    # 6. Schedule Semifinal 2: Gamma (White) vs Delta (Black)
    m2, sides2 = await create_match(
        db_session,
        white_submission_version_id=p_c["version_id"],
        black_submission_version_id=p_d["version_id"],
        configuration=config,
        purpose=MatchPurpose.OFFICIAL,
    )
    # Simulate match 2 completion: Draw by stalemate
    await _complete_match(db_session, m2, MatchOutcome.DRAW, "STALEMATE")

    # 7. Playoff match between Gamma and Delta: Delta loses, Gamma wins
    m3, sides3 = await create_match(
        db_session,
        white_submission_version_id=p_d["version_id"],
        black_submission_version_id=p_c["version_id"],
        configuration=config,
        purpose=MatchPurpose.OFFICIAL,
    )
    await _complete_match(db_session, m3, MatchOutcome.BLACK_WIN, "CHECKMATE")

    # 8. Finals: Alpha vs Gamma -> Gamma wins the Championship!
    m4, sides4 = await create_match(
        db_session,
        white_submission_version_id=p_a["version_id"],
        black_submission_version_id=p_c["version_id"],
        configuration=config,
        purpose=MatchPurpose.OFFICIAL,
    )
    await _complete_match(db_session, m4, MatchOutcome.BLACK_WIN, "CHECKMATE")

    # 9. Verify Tournament Leaderboard via API
    lb_resp = await api_client.get("/api/v1/tournaments/leaderboard")
    assert lb_resp.status_code == 200
    standings = lb_resp.json()["standings"]

    assert len(standings) == 4
    # Gamma played 3 matches (Draw, Win, Win) = 2.5 points -> Rank 1 (Champion)
    assert standings[0]["participant_id"] == p_c["participant_id"]
    assert standings[0]["points"] == 2.5
    assert standings[0]["wins"] == 2
    assert standings[0]["draws"] == 1
    assert standings[0]["losses"] == 0
    assert standings[0]["status"] == "CHAMPION"

    # Alpha played 2 matches (Win, Loss) = 1.0 point -> Rank 2 (Finalist)
    assert standings[1]["participant_id"] == p_a["participant_id"]
    assert standings[1]["points"] == 1.0
    assert standings[1]["wins"] == 1
    assert standings[1]["losses"] == 1
    assert standings[1]["status"] == "FINALIST"

    # Delta played 2 matches (Draw, Loss) = 0.5 points -> Rank 3
    assert standings[2]["participant_id"] == p_d["participant_id"]
    assert standings[2]["points"] == 0.5

    # Beta played 1 match (Loss) = 0.0 points -> Rank 4
    assert standings[3]["participant_id"] == p_b["participant_id"]
    assert standings[3]["points"] == 0.0

    # 10. Verify Tournament Bracket via API
    bracket_resp = await api_client.get("/api/v1/tournaments/bracket")
    assert bracket_resp.status_code == 200
    bracket_data = bracket_resp.json()
    assert bracket_data["total_participants"] == 4
    assert len(bracket_data["rounds"]) >= 1
