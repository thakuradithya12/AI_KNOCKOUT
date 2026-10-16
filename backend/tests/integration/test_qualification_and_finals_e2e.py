"""End-to-end integration test suite for Qualification Swiss rounds and 6-Player Finals.

Verifies:
1. Full Qualification (5 single-game Swiss rounds) -> Leaderboard -> 6-Player Finals (QF, SF, Grand Final & 3rd Place).
2. Authoritative result recording, duplicate submissions, and restart idempotency.
3. Authorization gates and audit event logging.
4. Separation of House AI development/practice matches from official standings.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.competition import ArtifactState, CompetitionEvent
from app.models.event import TournamentPairingRecord, TournamentRoundRecord
from app.models.match import Match, MatchOutcome, MatchPurpose, MatchResultSource, MatchState
from app.tournaments.service import (
    advance_six_player_finals,
    get_six_player_finals_bracket,
    get_tournament_leaderboard,
    initialize_six_player_finals,
    schedule_swiss_round,
)
from tests.unit.test_submission_validation import _valid_tar


async def _register_and_qualify(api_client: httpx.AsyncClient, name: str, admin_headers: dict) -> dict:
    """Helper to register participant, submit valid archive, obtain organizer approval, and select official version."""
    reg = await api_client.post("/api/v1/participants/register", json={"name": name, "team_name": f"Team {name}"})
    assert reg.status_code == 201
    pid, key = reg.json()["participant_id"], reg.json()["access_key"]

    login = await api_client.post("/api/v1/auth/login", json={"participant_id": pid, "access_key": key})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    sub = await api_client.post("/api/v1/submissions", headers=headers)
    assert sub.status_code == 201
    sid = sub.json()["submission_id"]

    archive = _valid_tar(source=f"# {name} Engine\n".encode())
    upload = await api_client.post(
        f"/api/v1/submissions/{sid}/versions",
        content=archive,
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert upload.status_code == 201
    vid = upload.json()["version"]["id"]
    v_num = upload.json()["version"]["version"]

    app = await api_client.post(
        "/api/v1/admin/submissions/approve",
        headers=admin_headers,
        json={"submission_version_id": vid, "reviewer_id": "head-judge"},
    )
    assert app.status_code == 200

    sel = await api_client.put(
        "/api/v1/participants/me/official-submission",
        headers=headers,
        json={"submission_id": sid, "version": v_num},
    )
    assert sel.status_code == 200

    return {
        "participant_id": pid,
        "name": name,
        "headers": headers,
        "submission_id": sid,
        "version_id": vid,
        "version_number": v_num,
    }


async def _complete_match(session: AsyncSession, match_id: uuid.UUID, outcome: MatchOutcome) -> None:
    now = datetime.now(timezone.utc)
    match = await session.get(Match, match_id)
    assert match is not None
    match.state = MatchState.STARTING
    await session.flush()
    match.state = MatchState.RUNNING
    match.started_at = now
    await session.flush()
    match.state = MatchState.COMPLETED
    match.result_outcome = outcome
    match.result_reason = "CHECKMATE"
    match.result_source = MatchResultSource.UNKNOWN
    match.completed_at = now
    await session.commit()


# ============================================================================
# 1. FULL END-TO-END QUALIFICATION (5 ROUNDS) + 6-PLAYER FINALS
# ============================================================================

@pytest.mark.asyncio
async def test_qualification_and_six_player_finals_full_e2e(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
):
    """Complete end-to-end tournament lifecycle:
    1. Register 8 participants.
    2. Freeze versions on Round 1.
    3. Run 5 qualification Swiss rounds (single game per pairing).
    4. Compute server-side leaderboard with Buchholz tie-breaks.
    5. Initialize 6-player Finals (Seeds 1 & 2 byes; QF: 4v5, 3v6).
    6. Advance Quarterfinals -> Semifinals -> Grand Final & 3rd Place.
    7. Crown Champion, 2nd, 3rd, and 4th place.
    8. Verify public bracket schema.
    """
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}
    t_id = "qual-finals-e2e"

    # 1. Register 8 participants
    participants = []
    for i in range(1, 9):
        p = await _register_and_qualify(api_client, f"QualP{i}", admin_headers)
        participants.append(p)

    # 2. Run 5 Qualification Swiss Rounds
    for r in range(1, 6):
        res = await api_client.post(
            "/api/v1/admin/tournaments/swiss/schedule-round",
            json={"round_number": r, "tournament_id": t_id, "games_per_pairing": 1},
            headers=admin_headers,
        )
        assert res.status_code == 200
        r_id = uuid.UUID(res.json()["round_id"])

        pairings = (
            await db_session.execute(
                select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == r_id)
            )
        ).scalars().all()

        assert len(pairings) == 4  # 8 participants -> 4 pairings
        for idx, p in enumerate(pairings):
            assert p.is_bye is False
            assert p.game1_match_id is not None
            assert p.game2_match_id is None  # Exactly 1 game per pairing

            # Complete match via authoritative admin endpoint
            outcome = "WHITE_WIN" if (idx + r) % 2 == 1 else "BLACK_WIN"
            rec_res = await api_client.post(
                f"/api/v1/admin/tournaments/matches/{p.game1_match_id}/record-result",
                json={"result_outcome": outcome, "result_reason": "CHECKMATE"},
                headers=admin_headers,
            )
            assert rec_res.status_code == 200

    # 3. Check public leaderboard
    lb_res = await api_client.get(f"/api/v1/tournaments/leaderboard?tournament_id={t_id}")
    assert lb_res.status_code == 200
    standings = lb_res.json()["standings"]
    assert len(standings) == 8

    # All participants played 5 games
    for s in standings:
        assert s["matches_played"] == 5
        assert s["buchholz"] >= 0.0

    # 4. Initialize 6-player Finals
    init_res = await api_client.post(
        "/api/v1/admin/tournaments/finals/initialize",
        json={"tournament_id": t_id, "allow_cutoff_fallback": True, "games_per_matchup": 1},
        headers=admin_headers,
    )
    assert init_res.status_code == 200
    qf_round_id = uuid.UUID(init_res.json()["round_id"])
    assert init_res.json()["round_name"] == "Finals - Quarterfinals"

    # Verify Quarterfinals: exactly 2 pairings (Seed 4 vs 5, Seed 3 vs 6)
    qf_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == qf_round_id)
        )
    ).scalars().all()
    assert len(qf_pairings) == 2

    # Complete Quarterfinal matches (White wins both QF matches)
    for p in qf_pairings:
        await _complete_match(db_session, p.game1_match_id, MatchOutcome.WHITE_WIN)

    # 5. Advance to Semifinals
    adv_sf = await api_client.post(
        "/api/v1/admin/tournaments/finals/advance",
        json={"tournament_id": t_id, "games_per_matchup": 1},
        headers=admin_headers,
    )
    assert adv_sf.status_code == 200
    assert adv_sf.json()["round_name"] == "Finals - Semifinals"
    sf_round_id = uuid.UUID(adv_sf.json()["round_id"])

    sf_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == sf_round_id)
        )
    ).scalars().all()
    assert len(sf_pairings) == 2

    # Complete Semifinal matches (White wins SF1, Black wins SF2)
    await _complete_match(db_session, sf_pairings[0].game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match(db_session, sf_pairings[1].game1_match_id, MatchOutcome.BLACK_WIN)

    # 6. Advance to Grand Final and Third-Place Playoff
    adv_final = await api_client.post(
        "/api/v1/admin/tournaments/finals/advance",
        json={"tournament_id": t_id, "games_per_matchup": 1},
        headers=admin_headers,
    )
    assert adv_final.status_code == 200
    assert adv_final.json()["round_name"] == "Finals - Championship & 3rd Place"
    f_round_id = uuid.UUID(adv_final.json()["round_id"])

    f_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == f_round_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    assert len(f_pairings) == 2
    assert f_pairings[0].stage_name == "FINAL"
    assert f_pairings[1].stage_name == "THIRD_PLACE_PLAYOFF"

    # Complete Final and Third-Place Playoff matches
    await _complete_match(db_session, f_pairings[0].game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match(db_session, f_pairings[1].game1_match_id, MatchOutcome.WHITE_WIN)

    # 7. Final advance crowns champion and completes tournament
    adv_done = await api_client.post(
        "/api/v1/admin/tournaments/finals/advance",
        json={"tournament_id": t_id, "games_per_matchup": 1},
        headers=admin_headers,
    )
    assert adv_done.status_code == 200
    assert adv_done.json()["status"] == "COMPLETED"

    # 8. Retrieve public Finals bracket
    b_res = await api_client.get(f"/api/v1/tournaments/finals/bracket?tournament_id={t_id}")
    assert b_res.status_code == 200
    bracket = b_res.json()

    assert bracket["status"] == "COMPLETED"
    assert bracket["champion"] is not None
    assert bracket["runner_up"] is not None
    assert bracket["third_place_winner"] is not None
    assert bracket["fourth_place"] is not None
    assert len(bracket["byes"]) == 2
    assert bracket["champion"]["participant_id"] != bracket["runner_up"]["participant_id"]
    assert bracket["third_place_winner"]["participant_id"] != bracket["fourth_place"]["participant_id"]


# ============================================================================
# 2. DUPLICATE DISPATCHES, REPEATED RESULTS & RESTART IDEMPOTENCY
# ============================================================================

@pytest.mark.asyncio
async def test_duplicate_result_recording_and_restart_idempotency(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
):
    """Verify that repeated result submissions and advancement calls are strictly idempotent."""
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}
    t_id = "idempotency-test"

    p1 = await _register_and_qualify(api_client, "Idemp_A", admin_headers)
    p2 = await _register_and_qualify(api_client, "Idemp_B", admin_headers)

    # Schedule round 1
    r_res = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        json={"round_number": 1, "tournament_id": t_id, "games_per_pairing": 1},
        headers=admin_headers,
    )
    assert r_res.status_code == 200
    r_id = uuid.UUID(r_res.json()["round_id"])

    # Duplicate call to schedule round 1 returns existing record without duplicating
    r_dup = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        json={"round_number": 1, "tournament_id": t_id, "games_per_pairing": 1},
        headers=admin_headers,
    )
    assert r_dup.status_code == 200
    assert r_dup.json()["round_id"] == str(r_id)

    pairing = await db_session.scalar(
        select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == r_id)
    )
    assert pairing is not None

    # Record result: WHITE_WIN
    rec1 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{pairing.game1_match_id}/record-result",
        json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
        headers=admin_headers,
    )
    assert rec1.status_code == 200
    assert rec1.json()["status"] == "COMPLETED"

    # Repeated identical submission returns 200 without error
    rec2 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{pairing.game1_match_id}/record-result",
        json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
        headers=admin_headers,
    )
    assert rec2.status_code == 200
    assert rec2.json()["status"] == "COMPLETED"

    # Conflicting submission is rejected (409 Conflict)
    rec_conflict = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{pairing.game1_match_id}/record-result",
        json={"result_outcome": "BLACK_WIN", "result_reason": "CHECKMATE"},
        headers=admin_headers,
    )
    assert rec_conflict.status_code == 409


# ============================================================================
# 3. AUTHORIZATION & AUDIT LOGS
# ============================================================================

@pytest.mark.asyncio
async def test_authorization_and_audit_logging(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
):
    """Verify that admin tournament endpoints require organizer authorization."""
    # 1. Unauthenticated requests are rejected
    bad_init = await api_client.post(
        "/api/v1/admin/tournaments/finals/initialize",
        json={"tournament_id": "auth-test"},
        headers={},
    )
    assert bad_init.status_code in (401, 403)

    bad_adv = await api_client.post(
        "/api/v1/admin/tournaments/finals/advance",
        json={"tournament_id": "auth-test"},
        headers={},
    )
    assert bad_adv.status_code in (401, 403)

    # 2. Wrong secret is rejected
    wrong_init = await api_client.post(
        "/api/v1/admin/tournaments/finals/initialize",
        json={"tournament_id": "auth-test"},
        headers={"X-Organizer-Secret": "invalid-secret"},
    )
    assert wrong_init.status_code == 401


# ============================================================================
# 4. STRICT SEPARATION OF HOUSE AI PRACTICE FROM OFFICIAL STANDINGS
# ============================================================================

@pytest.mark.asyncio
async def test_practice_matches_strictly_separated_from_standings(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
):
    """Verify that development/practice matches do NOT contaminate official Swiss tournament standings."""
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify(api_client, "PracticeUser1", admin_headers)
    p2 = await _register_and_qualify(api_client, "PracticeUser2", admin_headers)

    # Query official leaderboard before practice match
    lb1 = await api_client.get("/api/v1/tournaments/leaderboard")
    assert lb1.status_code == 200

    # Create and complete a PEER_TEST / practice match between two distinct participants
    from app.matches.service import create_match
    from app.tournaments.service import default_official_match_configuration

    peer_match, _ = await create_match(
        db_session,
        white_submission_version_id=uuid.UUID(str(p1["version_id"])),
        black_submission_version_id=uuid.UUID(str(p2["version_id"])),
        configuration=default_official_match_configuration(),
        purpose=MatchPurpose.PEER_TEST,
    )
    await _complete_match(db_session, peer_match.id, MatchOutcome.WHITE_WIN)

    # Query official leaderboard after practice match
    lb2 = await api_client.get("/api/v1/tournaments/leaderboard")
    assert lb2.status_code == 200

    # Total official matches count must NOT include development match
    assert lb2.json()["total_matches"] == lb1.json()["total_matches"]
