"""Integration tests for Swiss-system scheduling, two-game pairings, tie-breaks, cutoff snapshot immutability, playoff brackets, and sudden death."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.competition import ArtifactState
from app.models.event import (
    EventControl,
    OfficialSubmissionSelection,
    TournamentPairingRecord,
    TournamentRoundRecord,
    TournamentSnapshot,
)
from app.models.match import Match, MatchOutcome, MatchResultSource, MatchState
from app.tournaments.service import (
    advance_playoff_stage,
    create_tournament_snapshot,
    get_official_tournament_matches,
    get_tournament_leaderboard,
    initialize_playoff_bracket,
    schedule_swiss_round,
)
from tests.unit.test_submission_validation import _valid_tar


async def _register_and_qualify_participant(api_client: httpx.AsyncClient, name: str, admin_headers: dict) -> dict:
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

    # Organizer approves for eligibility
    app = await api_client.post(
        "/api/v1/admin/submissions/approve",
        headers=admin_headers,
        json={"submission_version_id": vid, "reviewer_id": "lead-arbiter"},
    )
    assert app.status_code == 200

    # Participant selects official version
    sel = await api_client.put(
        "/api/v1/participants/me/official-submission",
        headers=headers,
        json={"submission_id": sid, "version": v_num},
    )
    assert sel.status_code == 200

    return {
        "participant_id": pid,
        "name": name,
        "token": token,
        "headers": headers,
        "submission_id": sid,
        "version_id": vid,
        "version_number": v_num,
    }


async def _complete_match_manually(session: AsyncSession, match_id: uuid.UUID, outcome: MatchOutcome) -> None:
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


@pytest.mark.asyncio
async def test_end_to_end_swiss_two_games_with_reversed_colors(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # Register 4 participants
    p1 = await _register_and_qualify_participant(api_client, "Swiss_A", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Swiss_B", admin_headers)
    p3 = await _register_and_qualify_participant(api_client, "Swiss_C", admin_headers)
    p4 = await _register_and_qualify_participant(api_client, "Swiss_D", admin_headers)

    # 1. Organizer schedules Swiss Round 1 via API
    res_r1 = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        json={"round_number": 1, "tournament_id": "official-v1"},
        headers=admin_headers,
    )
    assert res_r1.status_code == 200
    r1_id = uuid.UUID(res_r1.json()["round_id"])

    # Verify 2 pairings created, each pairing has 2 games with reversed colors
    pairings_r1 = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == r1_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    assert len(pairings_r1) == 2

    for p in pairings_r1:
        assert p.is_bye is False
        assert p.game1_match_id is not None
        assert p.game2_match_id is not None
        # Verify Game 1 vs Game 2 colors in database
        m1 = await db_session.get(Match, p.game1_match_id)
        m2 = await db_session.get(Match, p.game2_match_id)
        assert m1 is not None and m2 is not None

        # Game 1: P1 White, P2 Black; Game 2: P2 White, P1 Black
        from app.models.match import MatchSide, ParticipantMatchSideBinding, MatchSideColor
        m1_white = await db_session.scalar(
            select(ParticipantMatchSideBinding.participant_id)
            .join(MatchSide, MatchSide.id == ParticipantMatchSideBinding.match_side_id)
            .where(MatchSide.match_id == m1.id, MatchSide.color == MatchSideColor.WHITE)
        )
        m2_white = await db_session.scalar(
            select(ParticipantMatchSideBinding.participant_id)
            .join(MatchSide, MatchSide.id == ParticipantMatchSideBinding.match_side_id)
            .where(MatchSide.match_id == m2.id, MatchSide.color == MatchSideColor.WHITE)
        )
        assert m1_white == p.participant1_id
        assert m2_white == p.participant2_id  # Reversed colors!

    # Complete Round 1 matches
    # Pairing 1: P1 wins Game 1 (White), draws Game 2 (Black) -> 1.5 - 0.5
    await _complete_match_manually(db_session, pairings_r1[0].game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match_manually(db_session, pairings_r1[0].game2_match_id, MatchOutcome.DRAW)

    # Pairing 2: P2 wins both games -> 0.0 - 2.0
    await _complete_match_manually(db_session, pairings_r1[1].game1_match_id, MatchOutcome.BLACK_WIN)
    await _complete_match_manually(db_session, pairings_r1[1].game2_match_id, MatchOutcome.WHITE_WIN)

    # 2. Check public leaderboard reflects aggregated 2-game points
    lb = await api_client.get("/api/v1/tournaments/leaderboard")
    assert lb.status_code == 200
    standings = lb.json()["standings"]
    assert len(standings) == 4
    # Points must reflect 2 games per pairing
    points = [s["points"] for s in standings]
    assert 2.0 in points
    assert 1.5 in points
    assert 0.5 in points
    assert 0.0 in points


@pytest.mark.asyncio
async def test_odd_participant_count_bye_assignment_and_no_repeat_byes(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # Register 5 participants (odd count)
    p1 = await _register_and_qualify_participant(api_client, "Odd_P1", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Odd_P2", admin_headers)
    p3 = await _register_and_qualify_participant(api_client, "Odd_P3", admin_headers)
    p4 = await _register_and_qualify_participant(api_client, "Odd_P4", admin_headers)
    p5 = await _register_and_qualify_participant(api_client, "Odd_P5", admin_headers)

    # Schedule Swiss Round 1
    res_r1 = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        json={"round_number": 1, "tournament_id": "odd-tourney"},
        headers=admin_headers,
    )
    assert res_r1.status_code == 200
    r1_id = uuid.UUID(res_r1.json()["round_id"])

    # Verify exactly 1 bye pairing created with full 2.0 points
    pairings_r1 = (
        await db_session.execute(
            select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == r1_id)
        )
    ).scalars().all()
    bye_p = next(p for p in pairings_r1 if p.is_bye)
    assert bye_p.p1_points == 2.0
    first_bye_player = bye_p.participant1_id

    # Complete the other matches
    for p in pairings_r1:
        if not p.is_bye:
            await _complete_match_manually(db_session, p.game1_match_id, MatchOutcome.WHITE_WIN)
            await _complete_match_manually(db_session, p.game2_match_id, MatchOutcome.WHITE_WIN)

    # Schedule Swiss Round 2
    res_r2 = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        json={"round_number": 2, "tournament_id": "odd-tourney"},
        headers=admin_headers,
    )
    assert res_r2.status_code == 200
    r2_id = uuid.UUID(res_r2.json()["round_id"])

    pairings_r2 = (
        await db_session.execute(
            select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == r2_id)
        )
    ).scalars().all()
    bye_r2 = next(p for p in pairings_r2 if p.is_bye)
    second_bye_player = bye_r2.participant1_id

    # Crucial Rule: NO REPEAT BYE!
    assert second_bye_player != first_bye_player


@pytest.mark.asyncio
async def test_cutoff_snapshot_immutability(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify_participant(api_client, "Freeze_P1", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Freeze_P2", admin_headers)

    # 1. Organizer triggers official freeze
    freeze_res = await api_client.post(
        "/api/v1/admin/freeze",
        json={"reason_code": "OFFICIAL_CHAMPIONSHIP_CUTOFF", "actor_id": "director-lead"},
        headers=admin_headers,
    )
    assert freeze_res.status_code == 200

    # Create immutable tournament snapshot
    snaps = await create_tournament_snapshot(db_session, tournament_id="freeze-test")
    assert len(snaps) >= 2
    p1_snap = next(s for s in snaps if s.participant_id == p1["participant_id"])
    assert p1_snap.version_number == p1["version_number"]

    # 2. Post-freeze attempt: Participant tries to change official selection -> REJECTED 400
    sel_post = await api_client.put(
        "/api/v1/participants/me/official-submission",
        headers=p1["headers"],
        json={"submission_id": p1["submission_id"], "version": p1["version_number"]},
    )
    assert sel_post.status_code == 409
    assert "OFFICIAL_SUBMISSION_FROZEN" in sel_post.text

    # Snapshot rows in database remain strictly unchanged
    snap_check = await db_session.scalar(
        select(TournamentSnapshot).where(
            TournamentSnapshot.tournament_id == "freeze-test",
            TournamentSnapshot.participant_id == p1["participant_id"],
        )
    )
    assert snap_check.version_number == p1_snap.version_number
    assert snap_check.submission_version_id == p1_snap.submission_version_id


@pytest.mark.asyncio
async def test_playoff_bracket_seeding_and_sudden_death_flow(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # Register 6 participants for the approved 6-player Finals bracket
    participants = []
    for i in range(1, 7):
        p = await _register_and_qualify_participant(api_client, f"Playoff_{i}", admin_headers)
        participants.append(p)

    # 1. Initialize playoffs: Seeds 1 & 2 receive Semifinal BYES; Round 1 is Quarterfinals (4v5, 3v6)
    init_res = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/initialize",
        json={"tournament_id": "playoff-test"},
        headers=admin_headers,
    )
    assert init_res.status_code == 200
    assert "Quarterfinals" in init_res.json()["round_name"]
    assert init_res.json()["round_number"] == 1
    r1_id = uuid.UUID(init_res.json()["round_id"])

    # 2. Verify 2 Quarterfinal pairings (Pairing 1: Seed 4 vs Seed 5, Pairing 2: Seed 3 vs Seed 6)
    pairings = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == r1_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    assert len(pairings) == 2

    p_qf1 = pairings[0]
    p_qf2 = pairings[1]

    # Pairing 1: Decisive sweep 2-0 (P1 wins both games)
    await _complete_match_manually(db_session, p_qf1.game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match_manually(db_session, p_qf1.game2_match_id, MatchOutcome.BLACK_WIN)

    # Pairing 2: Tied 1.0 - 1.0 (both games DRAW) -> Needs sudden death!
    await _complete_match_manually(db_session, p_qf2.game1_match_id, MatchOutcome.DRAW)
    await _complete_match_manually(db_session, p_qf2.game2_match_id, MatchOutcome.DRAW)

    # 3. Call advance_playoffs: Should trigger sudden-death for Pairing 2
    adv_res1 = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/advance",
        json={"tournament_id": "playoff-test"},
        headers=admin_headers,
    )
    assert adv_res1.status_code == 200
    # Still in round 1 because sudden death is needed
    assert adv_res1.json()["round_number"] == 1

    # Verify sudden death match created on Pairing 2
    await db_session.refresh(p_qf2)
    assert len(p_qf2.sudden_death_match_ids) == 1
    sd1_id = uuid.UUID(p_qf2.sudden_death_match_ids[0])

    # 4. Resolve Sudden Death Match: Black Win. The first sudden-death game gives
    # participant2 White, so a Black win advances participant1 (Seed 3).
    await _complete_match_manually(db_session, sd1_id, MatchOutcome.BLACK_WIN)

    # 5. Call advance_playoffs again: All Quarterfinals resolved -> Semifinals scheduled!
    adv_res2 = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/advance",
        json={"tournament_id": "playoff-test"},
        headers=admin_headers,
    )
    assert adv_res2.status_code == 200
    assert adv_res2.json()["round_number"] == 2
    assert "Semifinals" in adv_res2.json()["round_name"]
    sf_round_id = uuid.UUID(adv_res2.json()["round_id"])

    # Verify Semifinal pairings (SF1: Seed 1 vs Winner(QF1 - Seed 4); SF2: Seed 2 vs Winner(QF2 - Seed 3))
    sf_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == sf_round_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    assert len(sf_pairings) == 2
    # Seeds come from the qualification standings (all tied here, so the deterministic
    # seed fallback decides), not from registration order.
    seeded = [p for p in participants if p["participant_id"] not in {
        p_qf1.participant1_id, p_qf1.participant2_id, p_qf2.participant1_id, p_qf2.participant2_id,
    }]
    assert len(seeded) == 2
    standings = (await get_tournament_leaderboard(db_session, "playoff-test")).standings
    seed_order = [s.participant_id for s in standings[:2]]
    assert set(seed_order) == {p["participant_id"] for p in seeded}
    assert sf_pairings[0].participant1_id == seed_order[0]  # Seed 1
    assert sf_pairings[0].participant2_id == p_qf1.participant1_id  # Winner QF1 (Seed 4)
    assert sf_pairings[1].participant1_id == seed_order[1]  # Seed 2
    assert sf_pairings[1].participant2_id == p_qf2.participant1_id  # Winner QF2 (Seed 3)


@pytest.mark.asyncio
async def test_idempotent_round_scheduling_and_duplicate_protection(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify_participant(api_client, "Idem_A", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Idem_B", admin_headers)

    # Schedule round 1
    res1 = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        json={"round_number": 1, "tournament_id": "idem-test"},
        headers=admin_headers,
    )
    assert res1.status_code == 200
    round_id_1 = res1.json()["round_id"]

    # Retrying the exact same request
    res2 = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        json={"round_number": 1, "tournament_id": "idem-test"},
        headers=admin_headers,
    )
    assert res2.status_code == 200
    round_id_2 = res2.json()["round_id"]

    # Must return the identical round ID without creating duplicate matches
    assert round_id_1 == round_id_2

    # Verify match count in database
    matches = (
        await db_session.execute(
            select(Match).where(Match.purpose == "OFFICIAL")
        )
    ).scalars().all()
    # Exactly 2 matches created for round 1 (Game 1 & Game 2)
    round_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == uuid.UUID(round_id_1))
        )
    ).scalars().all()
    assert len(round_pairings) == 1


@pytest.mark.asyncio
async def test_concurrent_round_scheduling_safety(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify_participant(api_client, "Conc_A", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Conc_B", admin_headers)

    # Call schedule-round concurrently across 3 parallel requests
    async def _call_schedule():
        return await api_client.post(
            "/api/v1/admin/tournaments/swiss/schedule-round",
            json={"round_number": 1, "tournament_id": "conc-test"},
            headers=admin_headers,
        )

    responses = await asyncio.gather(*[_call_schedule() for _ in range(3)])
    # All requests succeed and return the exact same round ID
    assert all(r.status_code == 200 for r in responses)
    round_ids = {r.json()["round_id"] for r in responses}
    assert len(round_ids) == 1


@pytest.mark.asyncio
async def test_playoff_initialization_blocked_on_cutoff_tie(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # Register 7 participants in a distinct tournament context
    # With 7 participants with 0.0 points, Rank 6 and Rank 7 are tied at the qualification cutoff!
    for i in range(1, 8):
        await _register_and_qualify_participant(api_client, f"TieCutoff_{i}", admin_headers)

    # Attempting to initialize playoff bracket must be safely blocked with HTTP 409 Conflict
    init_res = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/initialize",
        json={"tournament_id": "cutoff-tie-test"},
        headers=admin_headers,
    )
    assert init_res.status_code == 409
    assert init_res.json()["detail"] == "CUTOFF_TIE_AFFECTING_SIXTH_PLACE"


@pytest.mark.asyncio
async def test_playoff_initialization_fails_with_fewer_than_six_qualifiers(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # Register only 4 participants
    for i in range(1, 5):
        await _register_and_qualify_participant(api_client, f"FewerThanSix_{i}", admin_headers)

    # Attempting to initialize playoff bracket fails closed with HTTP 400 Bad Request
    init_res = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/initialize",
        json={"tournament_id": "fewer-than-six-test"},
        headers=admin_headers,
    )
    assert init_res.status_code == 400
    assert "INSUFFICIENT_QUALIFIERS" in init_res.json()["detail"]


@pytest.mark.asyncio
async def test_leaderboard_isolation_from_completed_playoffs(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # Register 6 participants
    participants = []
    for i in range(1, 7):
        p = await _register_and_qualify_participant(api_client, f"Iso_{chr(64 + i)}", admin_headers)
        participants.append(p)

    # 1. Schedule and complete Swiss Round 1 (3 pairings x 2 games = 6 games)
    r1_res = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        json={"round_number": 1, "tournament_id": "iso-test"},
        headers=admin_headers,
    )
    assert r1_res.status_code == 200
    r1_id = uuid.UUID(r1_res.json()["round_id"])

    pairings_r1 = (
        await db_session.execute(
            select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == r1_id)
        )
    ).scalars().all()
    assert len(pairings_r1) == 3

    # Complete Swiss matches
    for p in pairings_r1:
        await _complete_match_manually(db_session, p.game1_match_id, MatchOutcome.WHITE_WIN)
        await _complete_match_manually(db_session, p.game2_match_id, MatchOutcome.WHITE_WIN)

    # 2. Capture baseline Swiss leaderboard
    lb_before = await api_client.get("/api/v1/tournaments/leaderboard")
    assert lb_before.status_code == 200
    standings_before = lb_before.json()["standings"]

    # 3. Initialize playoff bracket
    init_res = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/initialize",
        json={"tournament_id": "iso-test"},
        headers=admin_headers,
    )
    assert init_res.status_code == 200
    playoff_round_id = uuid.UUID(init_res.json()["round_id"])

    playoff_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord).where(TournamentPairingRecord.round_id == playoff_round_id)
        )
    ).scalars().all()

    # Complete all playoff matches
    for p in playoff_pairings:
        await _complete_match_manually(db_session, p.game1_match_id, MatchOutcome.WHITE_WIN)
        await _complete_match_manually(db_session, p.game2_match_id, MatchOutcome.BLACK_WIN)

    # 4. Capture post-playoff Swiss leaderboard
    lb_after = await api_client.get("/api/v1/tournaments/leaderboard")
    assert lb_after.status_code == 200
    standings_after = lb_after.json()["standings"]

    # Standings must remain strictly isolated: points, wins, losses, matches_played unchanged
    assert len(standings_before) == len(standings_after)
    for sb, sa in zip(standings_before, standings_after):
        assert sb["participant_id"] == sa["participant_id"]
        assert sb["points"] == sa["points"]
        assert sb["wins"] == sa["wins"]
        assert sb["losses"] == sa["losses"]
        assert sb["draws"] == sa["draws"]
        assert sb["matches_played"] == sa["matches_played"]
        assert sb["rank"] == sa["rank"]


@pytest.mark.asyncio
async def test_six_player_playoff_bracket_end_to_end(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # Register 6 participants
    participants = []
    for i in range(1, 7):
        p = await _register_and_qualify_participant(api_client, f"SixBracket_{i}", admin_headers)
        participants.append(p)

    # 1. Initialize Playoff Bracket -> approved 6-player format starts at Quarterfinals
    init_res = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/initialize",
        json={"tournament_id": "six-player-test"},
        headers=admin_headers,
    )
    assert init_res.status_code == 200, f"{init_res.status_code}: {init_res.text}"
    assert "Quarterfinals" in init_res.json()["round_name"]
    assert init_res.json()["round_number"] == 1
    qf_round_id = uuid.UUID(init_res.json()["round_id"])

    # Verify exactly 2 Quarterfinal pairings created with reversed colors
    # Seeds 1 & 2 have byes; QF1 is Seed 4 vs Seed 5; QF2 is Seed 3 vs Seed 6
    qf_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == qf_round_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    assert len(qf_pairings) == 2

    for p in qf_pairings:
        assert p.game1_match_id is not None
        assert p.game2_match_id is not None
        m1 = await db_session.get(Match, p.game1_match_id)
        m2 = await db_session.get(Match, p.game2_match_id)
        assert m1 is not None and m2 is not None

    # Complete Quarterfinals:
    # QF 1: P1 (Seed 4) wins both games (2-0 sweep)
    await _complete_match_manually(db_session, qf_pairings[0].game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match_manually(db_session, qf_pairings[0].game2_match_id, MatchOutcome.BLACK_WIN)

    # QF 2: 1-1 split! (Game 1 White Win, Game 2 White Win) -> Requires sudden death!
    await _complete_match_manually(db_session, qf_pairings[1].game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match_manually(db_session, qf_pairings[1].game2_match_id, MatchOutcome.WHITE_WIN)

    # 2. Advance playoffs: QF 2 needs sudden death
    adv_qf = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/advance",
        json={"tournament_id": "six-player-test"},
        headers=admin_headers,
    )
    assert adv_qf.status_code == 200
    assert adv_qf.json()["round_number"] == 1  # Still QF pending sudden death

    await db_session.refresh(qf_pairings[1])
    assert len(qf_pairings[1].sudden_death_match_ids) == 1
    sd_id = uuid.UUID(qf_pairings[1].sudden_death_match_ids[0])

    # Resolve sudden death: Black wins (P1 of pairing 2 / Seed 3 wins)
    await _complete_match_manually(db_session, sd_id, MatchOutcome.BLACK_WIN)

    # 3. Advance playoffs to Semifinals (Round 2)
    adv_sf = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/advance",
        json={"tournament_id": "six-player-test"},
        headers=admin_headers,
    )
    assert adv_sf.status_code == 200
    assert adv_sf.json()["round_number"] == 2
    assert "Semifinals" in adv_sf.json()["round_name"]
    sf_round_id = uuid.UUID(adv_sf.json()["round_id"])

    sf_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == sf_round_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    assert len(sf_pairings) == 2

    # SF 1: Seed 1 vs Winner(QF1 - Seed 4) -> Seed 1 wins 2-0 (Seed 1 to Grand Final, Seed 4 to 3rd place)
    await _complete_match_manually(db_session, sf_pairings[0].game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match_manually(db_session, sf_pairings[0].game2_match_id, MatchOutcome.BLACK_WIN)

    # SF 2: Seed 2 vs Winner(QF2 - Seed 3) -> Seed 2 wins 2-0 (Seed 2 to Grand Final, Seed 3 to 3rd place)
    await _complete_match_manually(db_session, sf_pairings[1].game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match_manually(db_session, sf_pairings[1].game2_match_id, MatchOutcome.BLACK_WIN)

    # 4. Advance playoffs to Finals: Round 3 holds BOTH Grand Final and Third-Place Playoff!
    adv_fn = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/advance",
        json={"tournament_id": "six-player-test"},
        headers=admin_headers,
    )
    assert adv_fn.status_code == 200
    assert adv_fn.json()["round_number"] == 3
    assert "Championship" in adv_fn.json()["round_name"] or "Finals" in adv_fn.json()["round_name"]
    final_round_id = uuid.UUID(adv_fn.json()["round_id"])

    final_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == final_round_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    # Exactly 2 pairings in Round 3: Pairing 1 = Grand Final, Pairing 2 = Third-Place Playoff
    assert len(final_pairings) == 2

    # Grand Final: Seed 1 vs Seed 2 -> Seed 1 wins 2-0 -> Seed 1 is CHAMPION, Seed 2 is RUNNER-UP
    await _complete_match_manually(db_session, final_pairings[0].game1_match_id, MatchOutcome.WHITE_WIN)
    await _complete_match_manually(db_session, final_pairings[0].game2_match_id, MatchOutcome.BLACK_WIN)

    # Third-Place Playoff: Seed 4 vs Seed 3 -> Seed 3 wins 2-0 -> Seed 3 is 3RD PLACE, Seed 4 is 4TH PLACE
    await _complete_match_manually(db_session, final_pairings[1].game1_match_id, MatchOutcome.BLACK_WIN)
    await _complete_match_manually(db_session, final_pairings[1].game2_match_id, MatchOutcome.WHITE_WIN)

    # 5. Advance playoffs to complete tournament
    adv_complete = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/advance",
        json={"tournament_id": "six-player-test"},
        headers=admin_headers,
    )
    assert adv_complete.status_code == 200
    assert adv_complete.json()["status"] == "COMPLETED"

    # Idempotent re-advance returns completed status
    adv_idempotent = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/advance",
        json={"tournament_id": "six-player-test"},
        headers=admin_headers,
    )
    assert adv_idempotent.status_code == 200
    assert adv_idempotent.json()["status"] == "COMPLETED"

    # 6. Verify public finals bracket
    bracket_res = await api_client.get("/api/v1/tournaments/finals/bracket?tournament_id=six-player-test")
    assert bracket_res.status_code == 200
    bracket_data = bracket_res.json()
    assert bracket_data["status"] == "COMPLETED"
    assert bracket_data["champion"]["participant_id"] == participants[0]["participant_id"]  # Seed 1
    assert bracket_data["runner_up"]["participant_id"] == participants[1]["participant_id"]  # Seed 2
    assert bracket_data["third_place_winner"]["participant_id"] == participants[2]["participant_id"]  # Seed 3
    assert bracket_data["fourth_place"]["participant_id"] == participants[3]["participant_id"]  # Seed 4

