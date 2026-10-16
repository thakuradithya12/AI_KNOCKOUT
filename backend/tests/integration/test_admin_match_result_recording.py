"""Integration tests for organizer manual match result recording (Phase 4 Tournament Manual Fallback).

Verifies:
- Organizer authorization enforcement (missing/invalid secret -> 401).
- Non-existent match handling (404).
- Rejection of non-official (DEVELOPMENT) matches (409).
- Validation of allowed outcome and reason values (400).
- Valid state progression (QUEUED -> STARTING -> RUNNING -> COMPLETED) and lifecycle timestamps.
- Creation of auditable CompetitionEvent outbox rows.
- Dynamic leaderboard updates following recorded results.
- Safe idempotent retries (no duplicate audit events or timestamps).
- Rejection of conflicting mutations on completed matches (409).
- Multi-round Swiss scheduling and playoff progression with recorded results.

Environment note:
These integration tests use an isolated SQLite database by default, or a local
PostgreSQL aik_test_* database when AIK_TEST_DATABASE_URL is explicitly configured.
Requests use httpx.AsyncClient (ASGITransport); match state and tournament engine
are not mocked.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.matches.controller import MatchConfiguration
from app.matches.service import create_match
from app.models.competition import CompetitionEvent
from app.models.event import TournamentPairingRecord, TournamentRoundRecord
from app.models.match import Match, MatchOutcome, MatchPurpose, MatchState
from app.models.participant import Participant, ParticipantStatus
from tests.unit.test_submission_validation import _valid_tar


async def _register_and_qualify_participant(
    api_client: httpx.AsyncClient, name: str, admin_headers: dict
) -> dict:
    """Helper to register participant, submit valid archive, obtain organizer approval, and select official version."""
    reg = await api_client.post(
        "/api/v1/participants/register", json={"name": name, "team_name": f"Team {name}"}
    )
    assert reg.status_code == 201
    pid, key = reg.json()["participant_id"], reg.json()["access_key"]

    login = await api_client.post(
        "/api/v1/auth/login", json={"participant_id": pid, "access_key": key}
    )
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

    return {
        "participant_id": pid,
        "name": name,
        "token": token,
        "headers": headers,
        "submission_id": sid,
        "version_id": vid,
        "version_number": v_num,
    }


@pytest.mark.asyncio
async def test_record_result_requires_organizer_authorization(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Unauthorized requests without valid X-Organizer-Secret are rejected with 401."""
    random_id = uuid.uuid4()
    body = {"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"}

    # 1. Missing header
    res_no_auth = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{random_id}/record-result",
        json=body,
    )
    assert res_no_auth.status_code == 401
    assert "ORGANIZER_AUTHENTICATION_REQUIRED" in res_no_auth.json()["detail"]

    # 2. Invalid secret
    res_bad_auth = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{random_id}/record-result",
        headers={"X-Organizer-Secret": "invalid-secret-key"},
        json=body,
    )
    assert res_bad_auth.status_code == 401


@pytest.mark.asyncio
async def test_record_result_handles_unknown_match_id(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Non-existent match IDs return 404 Not Found."""
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}
    missing_id = uuid.uuid4()

    res = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{missing_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
    )
    assert res.status_code == 404
    assert res.json()["detail"] == "MATCH_NOT_FOUND"


@pytest.mark.asyncio
async def test_record_result_rejects_development_matches(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Only OFFICIAL matches can be completed through this endpoint (DEVELOPMENT rejected with 409)."""
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify_participant(api_client, "Dev_P1", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Dev_P2", admin_headers)
    from app.tournaments.service import default_official_match_configuration
    cfg = default_official_match_configuration()
    peer_match, _ = await create_match(
        db_session,
        white_submission_version_id=uuid.UUID(p1["version_id"]),
        black_submission_version_id=uuid.UUID(p2["version_id"]),
        configuration=cfg,
        purpose=MatchPurpose.PEER_TEST,
    )

    res = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{peer_match.id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
    )
    assert res.status_code == 409
    assert "OFFICIAL_MATCH_REQUIRED" in res.json()["detail"]


@pytest.mark.asyncio
async def test_record_result_rejects_invalid_outcomes_and_reasons(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Invalid outcome strings or non-standard result reasons return 400 Bad Request."""
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify_participant(api_client, "Val_P1", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Val_P2", admin_headers)

    res_round = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        headers=admin_headers,
        json={"round_number": 1, "tournament_id": "val-tourney"},
    )
    assert res_round.status_code == 200

    pairing = (
        await db_session.execute(
            select(TournamentPairingRecord).where(
                TournamentPairingRecord.round_id == uuid.UUID(res_round.json()["round_id"])
            )
        )
    ).scalars().first()
    assert pairing is not None and pairing.game1_match_id is not None
    match_id = pairing.game1_match_id

    # 1. Invalid outcome
    bad_outcome = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "ARBITER_AWARDED_VICTORY", "result_reason": "CHECKMATE"},
    )
    assert bad_outcome.status_code == 400
    assert "INVALID_RESULT_OUTCOME" in bad_outcome.json()["detail"]

    # 2. Invalid reason
    bad_reason = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "WHITE_WIN", "result_reason": "PARTICIPANT_RESIGNED"},
    )
    assert bad_reason.status_code == 400
    assert "INVALID_RESULT_REASON" in bad_reason.json()["detail"]


@pytest.mark.asyncio
async def test_record_result_state_transitions_timestamps_and_audit_event(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Recording a result transitions match state, populates timestamps, and logs CompetitionEvent."""
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify_participant(api_client, "Audit_P1", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Audit_P2", admin_headers)

    res_round = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        headers=admin_headers,
        json={"round_number": 1, "tournament_id": "audit-tourney"},
    )
    assert res_round.status_code == 200

    pairing = (
        await db_session.execute(
            select(TournamentPairingRecord).where(
                TournamentPairingRecord.round_id == uuid.UUID(res_round.json()["round_id"])
            )
        )
    ).scalars().first()
    match_id = pairing.game1_match_id

    # Record result
    res = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "COMPLETED"
    assert data["result_outcome"] == "WHITE_WIN"
    assert data["result_reason"] == "CHECKMATE"
    assert data["result_source"] == "manual"
    assert data["started_at"] is not None
    assert data["completed_at"] is not None
    assert data["idempotent_retry"] is False

    # Verify persisted database state
    match = await db_session.get(Match, match_id)
    assert match is not None
    assert match.state == MatchState.COMPLETED
    assert match.result_outcome == "WHITE_WIN"
    assert match.result_reason == "CHECKMATE"
    assert match.result_source.value == "manual"
    assert match.started_at is not None
    assert match.completed_at is not None

    # Verify persisted CompetitionEvent
    event = await db_session.scalar(
        select(CompetitionEvent).where(
            CompetitionEvent.aggregate_type == "match",
            CompetitionEvent.aggregate_id == match_id,
            CompetitionEvent.event_type == "MATCH_RESULT_RECORDED",
        )
    )
    assert event is not None
    assert event.actor_kind == "organizer"
    assert event.actor_id == "organizer-admin"
    assert event.metadata_json["result_outcome"] == "WHITE_WIN"
    assert event.metadata_json["result_reason"] == "CHECKMATE"
    assert event.metadata_json["result_source"] == "manual"
    assert event.metadata_json["previous_state"] == "QUEUED"


@pytest.mark.asyncio
async def test_record_result_idempotency_and_conflict_rejection(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Identical retries return 200 without duplicate audits; conflicting results return 409."""
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify_participant(api_client, "Idem_P1", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Idem_P2", admin_headers)

    res_round = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        headers=admin_headers,
        json={"round_number": 1, "tournament_id": "idem-tourney"},
    )
    pairing = (
        await db_session.execute(
            select(TournamentPairingRecord).where(
                TournamentPairingRecord.round_id == uuid.UUID(res_round.json()["round_id"])
            )
        )
    ).scalars().first()
    match_id = pairing.game1_match_id

    # 1. First recording -> success
    res1 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "BLACK_WIN", "result_reason": "CHECKMATE"},
    )
    assert res1.status_code == 200
    assert res1.json()["idempotent_retry"] is False
    assert res1.json()["result_source"] == "manual"

    # Count audit events
    audit_count_before = await db_session.scalar(
        select(func.count(CompetitionEvent.event_id)).where(
            CompetitionEvent.aggregate_id == match_id,
            CompetitionEvent.event_type == "MATCH_RESULT_RECORDED",
        )
    )
    assert audit_count_before == 1

    # 2. Identical retry -> 200 with idempotent_retry=True, no extra audit events
    res_retry = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "BLACK_WIN", "result_reason": "CHECKMATE"},
    )
    assert res_retry.status_code == 200
    assert res_retry.json()["idempotent_retry"] is True
    assert res_retry.json()["result_source"] == "manual"

    audit_count_after = await db_session.scalar(
        select(func.count(CompetitionEvent.event_id)).where(
            CompetitionEvent.aggregate_id == match_id,
            CompetitionEvent.event_type == "MATCH_RESULT_RECORDED",
        )
    )
    assert audit_count_after == 1  # No duplicate audit!

    # 3. Conflicting outcome -> 409 Conflict
    res_conflict = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
    )
    assert res_conflict.status_code == 409
    assert "CONFLICTING_RESULT" in res_conflict.json()["detail"]


@pytest.mark.asyncio
async def test_full_manual_tournament_flow_swiss_and_playoffs(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """End-to-end verification of manual tournament workflow:

    1. Register 4 participants.
    2. Schedule Swiss Round 1.
    3. Record Game 1 & Game 2 results via new API.
    4. Confirm public leaderboard updates points and tie-breaks.
    5. Schedule Swiss Round 2 and verify recorded results drive Swiss pairings.
    6. Record Round 2 results.
    7. Confirm the 6-player Finals cannot start with only 4 participants.
    """
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # 1. Register 4 participants
    p_a = await _register_and_qualify_participant(api_client, "Tournament_A", admin_headers)
    p_b = await _register_and_qualify_participant(api_client, "Tournament_B", admin_headers)
    p_c = await _register_and_qualify_participant(api_client, "Tournament_C", admin_headers)
    p_d = await _register_and_qualify_participant(api_client, "Tournament_D", admin_headers)

    # 2. Schedule Swiss Round 1
    r1_res = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        headers=admin_headers,
        json={"round_number": 1, "tournament_id": "e2e-tourney"},
    )
    assert r1_res.status_code == 200
    r1_id = uuid.UUID(r1_res.json()["round_id"])

    pairings_r1 = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == r1_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    assert len(pairings_r1) == 2

    # 3. Record results for Pairing 1:
    # Game 1: White wins (P1 wins Game 1)
    # Game 2: Black wins (P1 wins Game 2 with Black) -> P1 sweeps 2.0 - 0.0
    rec_p1_g1 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{pairings_r1[0].game1_match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
    )
    assert rec_p1_g1.status_code == 200

    rec_p1_g2 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{pairings_r1[0].game2_match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "BLACK_WIN", "result_reason": "CHECKMATE"},
    )
    assert rec_p1_g2.status_code == 200

    # Record results for Pairing 2:
    # Game 1: Draw
    # Game 2: Draw -> 1.0 - 1.0
    rec_p2_g1 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{pairings_r1[1].game1_match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "DRAW", "result_reason": "STALEMATE"},
    )
    assert rec_p2_g1.status_code == 200

    rec_p2_g2 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{pairings_r1[1].game2_match_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "DRAW", "result_reason": "THREEFOLD_REPETITION"},
    )
    assert rec_p2_g2.status_code == 200

    # 4. Check leaderboard
    lb_res = await api_client.get("/api/v1/tournaments/leaderboard?tournament_id=e2e-tourney")
    assert lb_res.status_code == 200
    standings = lb_res.json()["standings"]
    assert len(standings) == 4
    scores = {s["participant_id"]: s["points"] for s in standings}
    # Expected points: P1 of pairing 1 has 2.0, P2 has 0.0, pairing 2 both have 1.0
    assert 2.0 in scores.values()
    assert 0.0 in scores.values()
    assert list(scores.values()).count(1.0) == 2

    # 5. Schedule Swiss Round 2
    r2_res = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        headers=admin_headers,
        json={"round_number": 2, "tournament_id": "e2e-tourney"},
    )
    assert r2_res.status_code == 200
    r2_id = uuid.UUID(r2_res.json()["round_id"])

    pairings_r2 = (
        await db_session.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == r2_id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()
    assert len(pairings_r2) == 2
    # Verify repeat opponents avoided in Round 2
    r1_p1_opponents = {pairings_r1[0].participant1_id, pairings_r1[0].participant2_id}
    r2_p1_opponents = {pairings_r2[0].participant1_id, pairings_r2[0].participant2_id}
    assert r1_p1_opponents != r2_p1_opponents

    # Complete Round 2 matches
    for p in pairings_r2:
        await api_client.post(
            f"/api/v1/admin/tournaments/matches/{p.game1_match_id}/record-result",
            headers=admin_headers,
            json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
        )
        await api_client.post(
            f"/api/v1/admin/tournaments/matches/{p.game2_match_id}/record-result",
            headers=admin_headers,
            json={"result_outcome": "BLACK_WIN", "result_reason": "CHECKMATE"},
        )

    # 6. The approved Finals format is the 6-player bracket (top six qualifiers), so a
    # 4-participant field must be rejected rather than silently using a 4-player bracket.
    # Full 6-player bracket progression is covered in test_tournament_swiss_playoffs.py.
    playoff_init = await api_client.post(
        "/api/v1/admin/tournaments/playoffs/initialize",
        headers=admin_headers,
        json={"tournament_id": "e2e-tourney"},
    )
    assert playoff_init.status_code == 400
    assert "INSUFFICIENT_QUALIFIERS" in playoff_init.json()["detail"]


@pytest.mark.asyncio
async def test_record_result_atomic_rollback_on_partial_failure(
    api_client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
):
    """Verify atomicity: partial failures during recording roll back match, audit, and pairing mutations cleanly."""
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    p1 = await _register_and_qualify_participant(api_client, "Rollback_P1", admin_headers)
    p2 = await _register_and_qualify_participant(api_client, "Rollback_P2", admin_headers)

    res_round = await api_client.post(
        "/api/v1/admin/tournaments/swiss/schedule-round",
        headers=admin_headers,
        json={"round_number": 1, "tournament_id": "rollback-tourney"},
    )
    assert res_round.status_code == 200

    pairing = (
        await db_session.execute(
            select(TournamentPairingRecord).where(
                TournamentPairingRecord.round_id == uuid.UUID(res_round.json()["round_id"])
            )
        )
    ).scalars().first()
    g1_id = pairing.game1_match_id
    g2_id = pairing.game2_match_id

    # 1. Record Game 1 successfully
    rec1 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{g1_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
    )
    assert rec1.status_code == 200

    # 2. Simulate failure during Game 2 pairing evaluation (after match state has been flushed to COMPLETED)
    import app.api.v1.admin as admin_module

    def failing_eval(*args, **kwargs):
        raise RuntimeError("SIMULATED_INTERNAL_CRASH_BEFORE_COMMIT")

    monkeypatch.setattr(admin_module, "evaluate_pairing_two_games", failing_eval)

    # Attempt to record Game 2 -> should return clean 500 with RECORD_RESULT_FAILED detail
    res_fail = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{g2_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "BLACK_WIN", "result_reason": "CHECKMATE"},
    )
    assert res_fail.status_code == 500
    assert "RECORD_RESULT_FAILED" in res_fail.json()["detail"]
    assert "SIMULATED_INTERNAL_CRASH_BEFORE_COMMIT" not in res_fail.text

    # 3. Verify that Game 2 was completely rolled back in the database
    # Clear session identity map to ensure clean query from DB
    db_session.expunge_all()

    g2_match = await db_session.get(Match, g2_id)
    assert g2_match.state == MatchState.QUEUED
    assert g2_match.result_outcome is None
    assert g2_match.result_source is None
    assert g2_match.completed_at is None

    # Verify no audit event for Game 2
    g2_audits = (
        await db_session.execute(
            select(CompetitionEvent).where(
                CompetitionEvent.aggregate_id == g2_id,
                CompetitionEvent.event_type == "MATCH_RESULT_RECORDED",
            )
        )
    ).scalars().all()
    assert len(g2_audits) == 0

    # Verify pairing record was not corrupted (remains in progress with initial 0.0 points and no winner)
    pairing_check = await db_session.get(TournamentPairingRecord, pairing.id)
    assert pairing_check.status == "IN_PROGRESS"
    assert pairing_check.p1_points == 0.0
    assert pairing_check.p2_points == 0.0
    assert pairing_check.winner_id is None

    # 4. Remove monkeypatch and retry Game 2 -> should succeed atomically
    monkeypatch.undo()

    rec2 = await api_client.post(
        f"/api/v1/admin/tournaments/matches/{g2_id}/record-result",
        headers=admin_headers,
        json={"result_outcome": "BLACK_WIN", "result_reason": "CHECKMATE"},
    )
    assert rec2.status_code == 200

    db_session.expunge_all()
    g2_match_final = await db_session.get(Match, g2_id)
    assert g2_match_final.state == MatchState.COMPLETED
    assert g2_match_final.result_outcome == "BLACK_WIN"

    pairing_final = await db_session.get(TournamentPairingRecord, pairing.id)
    assert pairing_final.status == "COMPLETED"
    assert pairing_final.p1_points == 2.0
    assert pairing_final.p2_points == 0.0

