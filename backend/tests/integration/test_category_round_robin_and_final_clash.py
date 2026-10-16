"""Integration tests for Category A & B Round-Robin scheduling, standings, and Final Clash progression."""

from __future__ import annotations

import uuid
import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

from app.models.match import Match, MatchOutcome, MatchResultSource, MatchState
from app.models.event import TournamentPairingRecord, TournamentRoundRecord
from tests.unit.test_submission_validation import _valid_tar


async def _complete_match(
    session: AsyncSession,
    match: Match,
    outcome: MatchOutcome,
    reason: str = "CHECKMATE",
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
    match.result_source = MatchResultSource.UNKNOWN
    match.completed_at = now
    await session.commit()



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
        json={"submission_version_id": vid, "reviewer_id": "lead-arbiter"},
    )
    assert app.status_code == 200

    sel = await api_client.put(
        "/api/v1/participants/me/official-submission",
        headers=headers,
        json={"submission_id": sid, "version": v_num},
    )
    assert sel.status_code == 200
    return {"participant_id": pid, "name": name, "submission_version_id": vid}


@pytest.mark.asyncio
async def test_category_round_robin_and_final_clash_flow(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
):
    from app.core.config import Settings
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # 1. Register 6 participants (Seeds 1, 3, 5 -> Category A; Seeds 2, 4, 6 -> Category B)
    participants = []
    for i in range(1, 7):
        p = await _register_and_qualify(api_client, f"Contender_{i}", admin_headers)
        participants.append(p)

    # 2. Schedule Category A Round 1

    resp_a1 = await api_client.post(
        "/api/v1/admin/tournaments/category-round-robin/schedule-round",
        headers=admin_headers,
        json={"category": "A", "round_number": 1, "use_house_ai_filler": True},
    )
    if resp_a1.status_code != 200:
        with open("test_err.txt", "w", encoding="utf-8") as f:
            f.write(resp_a1.text)
    assert resp_a1.status_code == 200, f"Detail: {resp_a1.text}"
    round_a1_data = resp_a1.json()
    assert round_a1_data["round_name"] == "Category A - Round 1"

    # Schedule Category B Round 1
    resp_b1 = await api_client.post(
        "/api/v1/admin/tournaments/category-round-robin/schedule-round",
        headers=admin_headers,
        json={"category": "B", "round_number": 1, "use_house_ai_filler": True},
    )
    assert resp_b1.status_code == 200
    round_b1_data = resp_b1.json()
    assert round_b1_data["round_name"] == "Category B - Round 1"

    # 3. Terminal state check: attempting to schedule Category A Round 2 while Round 1 matches are queued must fail
    resp_a2_blocked = await api_client.post(
        "/api/v1/admin/tournaments/category-round-robin/schedule-round",
        headers=admin_headers,
        json={"category": "A", "round_number": 2, "use_house_ai_filler": True},
    )
    assert resp_a2_blocked.status_code == 400
    assert "PREVIOUS_ROUND_NOT_COMPLETED" in resp_a2_blocked.json()["detail"]

    # 4. Check category standings via public API
    cat_resp = await api_client.get("/api/v1/tournaments/categories")
    assert cat_resp.status_code == 200
    cat_data = cat_resp.json()
    assert "A" in cat_data["categories"]
    assert "B" in cat_data["categories"]
    assert len(cat_data["categories"]["A"]) > 0
    assert len(cat_data["categories"]["B"]) > 0

    # 5. Complete Category A & B Round 1 matches
    matches = (await db_session.execute(select(Match))).scalars().all()
    for m in matches:
        if m.state != MatchState.COMPLETED:
            await _complete_match(db_session, m, MatchOutcome.WHITE_WIN, "CHECKMATE")

    # 6. Initialize Final Clash (A1 and B1 Semifinal byes; QF1 A2 vs B3; QF2 B2 vs A3)
    clash_init = await api_client.post(
        "/api/v1/admin/tournaments/final-clash/initialize",
        headers=admin_headers,
        json={"tournament_id": "official-v1"},
    )
    assert clash_init.status_code == 200
    clash_data = clash_init.json()
    assert clash_data["round_name"] == "Final Clash - Quarterfinals"

    # Verify Quarterfinal pairings
    qf_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.stage_name == "QUARTERFINAL")
        )
    ).scalars().all()
    assert len(qf_pairings) == 2  # QF1 and QF2

    # 7. Complete QF matches and advance to Semifinals
    for p in qf_pairings:
        m1 = await db_session.get(Match, p.game1_match_id)
        m2 = await db_session.get(Match, p.game2_match_id)
        await _complete_match(db_session, m1, MatchOutcome.WHITE_WIN, "CHECKMATE")
        await _complete_match(db_session, m2, MatchOutcome.BLACK_WIN, "CHECKMATE")

    advance_sf = await api_client.post(
        "/api/v1/admin/tournaments/final-clash/advance",
        headers=admin_headers,
        json={"tournament_id": "official-v1"},
    )
    assert advance_sf.status_code == 200
    assert advance_sf.json()["round_name"] == "Final Clash - Semifinals"

    # Verify Semifinal pairings
    sf_pairings = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.stage_name == "SEMIFINAL")
        )
    ).scalars().all()
    assert len(sf_pairings) == 2

    # 8. Complete SF matches and advance to Finals
    for p in sf_pairings:
        m1 = await db_session.get(Match, p.game1_match_id)
        m2 = await db_session.get(Match, p.game2_match_id)
        await _complete_match(db_session, m1, MatchOutcome.WHITE_WIN, "CHECKMATE")
        await _complete_match(db_session, m2, MatchOutcome.BLACK_WIN, "CHECKMATE")

    advance_finals = await api_client.post(
        "/api/v1/admin/tournaments/final-clash/advance",
        headers=admin_headers,
        json={"tournament_id": "official-v1"},
    )
    assert advance_finals.status_code == 200
    assert advance_finals.json()["round_name"] == "Final Clash - Finals"

    # Verify Final pairing
    final_pairing = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.stage_name == "FINAL")
        )
    ).scalar_one()
    assert final_pairing is not None

    # 9. Complete Final and crown Champion
    m1 = await db_session.get(Match, final_pairing.game1_match_id)
    m2 = await db_session.get(Match, final_pairing.game2_match_id)
    await _complete_match(db_session, m1, MatchOutcome.WHITE_WIN, "CHECKMATE")
    await _complete_match(db_session, m2, MatchOutcome.BLACK_WIN, "CHECKMATE")

    advance_champion = await api_client.post(
        "/api/v1/admin/tournaments/final-clash/advance",
        headers=admin_headers,
        json={"tournament_id": "official-v1"},
    )
    print("Status:", advance_champion.status_code)
    print("Response:", advance_champion.text)
    assert advance_champion.status_code == 200
    assert advance_champion.json()["status"] == "COMPLETED"
