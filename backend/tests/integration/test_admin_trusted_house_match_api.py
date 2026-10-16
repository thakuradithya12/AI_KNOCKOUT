"""Isolated integration tests for the organizer-only trusted House AI match API.

Verifies:
- 401 Unauthorized rejection for requests lacking organizer credentials or using participant tokens
- 422 Unprocessable Entity rejection for unsupported/untrusted House AI engine identifiers
- 409 Conflict rejection for duplicate House AI sides
- Safe queue-only interface (execute_now=False)
- End-to-end execution, chess move legality replay, and terminal result verification (execute_now=True)
- Organizer match inspection endpoint (GET /api/v1/admin/development-matches/{match_id})
- Idempotency key replay behavior
- Strict database isolation using per-test temporary SQLite or isolated test schema
"""

from __future__ import annotations

import uuid
import chess
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.match import MatchOutcome, MatchResultSource
from app.services.house_ai_seeding import seed_all_house_ais


@pytest.fixture
def admin_headers() -> dict[str, str]:
    settings = Settings()
    return {"X-Organizer-Secret": settings.secret_key}


@pytest.mark.integration
async def test_admin_trusted_house_match_unauthorized_access(api_client: httpx.AsyncClient):
    """Verify that unauthenticated requests and participant tokens cannot access organizer endpoint."""
    url = "/api/v1/admin/development-matches/house-duel"

    # 1. No credentials -> 401 Unauthorized
    resp = await api_client.post(url, json={}, headers={"X-Anonymous": "true"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "ORGANIZER_AUTHENTICATION_REQUIRED"

    # 2. Participant credentials cannot access organizer admin route
    participant_resp = await api_client.post(
        "/api/v1/participants/register",
        json={"name": f"Part_{uuid.uuid4().hex[:8]}"},
    )
    assert participant_resp.status_code == 201
    p_data = participant_resp.json()
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": p_data["participant_id"], "access_key": p_data["access_key"]},
    )
    assert login_resp.status_code == 200
    participant_token = login_resp.json()["access_token"]

    forbidden = await api_client.post(
        url,
        json={},
        headers={"Authorization": f"Bearer {participant_token}", "X-Anonymous": "true"},
    )
    assert forbidden.status_code == 401
    assert forbidden.json()["detail"] in ("ORGANIZER_AUTHENTICATION_REQUIRED", "ORGANIZER_SESSION_INVALID")


@pytest.mark.integration
async def test_admin_trusted_house_match_engine_validation_guards(
    api_client: httpx.AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
):
    """Verify that untrusted engines and duplicate sides are rejected."""
    await seed_all_house_ais(db_session)
    await db_session.commit()
    url = "/api/v1/admin/development-matches/house-duel"

    # 1. Unsupported White engine
    bad_white = await api_client.post(
        url,
        json={
            "white_house_ai_id": "malicious-custom-engine",
            "black_house_ai_id": "house-ai-benchmark-baseline",
        },
        headers=admin_headers,
    )
    assert bad_white.status_code == 422
    assert "UNSUPPORTED_HOUSE_AI_ENGINE" in bad_white.json()["detail"]

    # 2. Unsupported Black engine
    bad_black = await api_client.post(
        url,
        json={
            "white_house_ai_id": "house-ai-benchmark-baseline",
            "black_house_ai_id": "untrusted-stockfish-binary",
        },
        headers=admin_headers,
    )
    assert bad_black.status_code == 422
    assert "UNSUPPORTED_HOUSE_AI_ENGINE" in bad_black.json()["detail"]

    # 3. Duplicate sides guard
    dup_side = await api_client.post(
        url,
        json={
            "white_house_ai_id": "house-ai-benchmark-baseline",
            "black_house_ai_id": "house-ai-benchmark-baseline",
        },
        headers=admin_headers,
    )
    assert dup_side.status_code == 409
    assert dup_side.json()["detail"] == "DUPLICATE_HOUSE_AI_SIDE"


@pytest.mark.integration
async def test_admin_trusted_house_match_queue_only(
    api_client: httpx.AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
):
    """Verify smallest safe queue-only mode (execute_now=False)."""
    await seed_all_house_ais(db_session)
    await db_session.commit()
    url = "/api/v1/admin/development-matches/house-duel"

    resp = await api_client.post(
        url,
        json={
            "white_house_ai_id": "house-ai-benchmark-baseline",
            "black_house_ai_id": "house-ai-maximum-difficulty",
            "execute_now": False,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "QUEUED"
    assert body["executed"] is False
    assert "remains unwired" in body["execution_note"]
    assert body["white_house_ai"]["house_ai_id"] == "house-ai-benchmark-baseline"
    assert body["black_house_ai"]["house_ai_id"] == "house-ai-maximum-difficulty"


@pytest.mark.integration
async def test_admin_trusted_house_match_run_legal_moves_and_replay(
    api_client: httpx.AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
):
    """Verify execution mode (execute_now=True), move legality replay, and inspector endpoint."""
    await seed_all_house_ais(db_session)
    await db_session.commit()
    url = "/api/v1/admin/development-matches/house-duel"

    resp = await api_client.post(
        url,
        json={
            "white_house_ai_id": "house-ai-benchmark-baseline",
            "black_house_ai_id": "house-ai-maximum-difficulty",
            "execute_now": True,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "COMPLETED"
    assert body["executed"] is True
    assert body["result"]["outcome"] in (
        MatchOutcome.WHITE_WIN.value,
        MatchOutcome.BLACK_WIN.value,
        MatchOutcome.DRAW.value,
    )
    assert body["result"]["reason"] in (
        "CHECKMATE",
        "STALEMATE",
        "DEAD_POSITION",
        "FIVEFOLD_REPETITION",
        "SEVENTY_FIVE_MOVE_RULE",
        "THREEFOLD_REPETITION",
        "FIFTY_MOVE_RULE",
    )
    assert body["current_ply"] > 0
    assert len(body["moves"]) == body["current_ply"]

    # Replay all moves sequentially on an independent chess.Board from initial FEN
    replay_board = chess.Board(body["initial_fen"])
    for idx, move_data in enumerate(body["moves"]):
        assert move_data["ply"] == idx + 1
        assert move_data["position_before_fen"] == replay_board.fen()
        chess_move = chess.Move.from_uci(move_data["move_uci"])
        assert chess_move in replay_board.legal_moves, (
            f"Move {move_data['move_uci']} at ply {move_data['ply']} was illegal!"
        )
        replay_board.push(chess_move)
        assert move_data["position_after_fen"] == replay_board.fen()

    assert replay_board.fen() == body["current_fen"]

    # Verify organizer inspector endpoint
    inspect_url = f"/api/v1/admin/development-matches/{body['match_id']}"
    inspect_resp = await api_client.get(inspect_url, headers=admin_headers)
    assert inspect_resp.status_code == 200
    inspect_body = inspect_resp.json()
    assert inspect_body["match_id"] == body["match_id"]
    assert inspect_body["state"] == "COMPLETED"
    assert inspect_body["current_fen"] == body["current_fen"]
    assert len(inspect_body["moves"]) == body["current_ply"]


@pytest.mark.integration
async def test_admin_trusted_house_match_idempotency_behavior(
    api_client: httpx.AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
):
    """Verify that repeated requests with the same Idempotency-Key return the existing match."""
    await seed_all_house_ais(db_session)
    await db_session.commit()
    url = "/api/v1/admin/development-matches/house-duel"
    idempotency_key = f"admin-idemp-{uuid.uuid4().hex}"
    headers = {**admin_headers, "Idempotency-Key": idempotency_key}

    # First request -> 201 Created
    first = await api_client.post(
        url,
        json={
            "white_house_ai_id": "house-ai-benchmark-baseline",
            "black_house_ai_id": "AIK-HOUSE-001",
            "execute_now": True,
        },
        headers=headers,
    )
    assert first.status_code == 201
    assert first.json()["idempotent_replay"] is False
    match_id = first.json()["match_id"]

    # Second request with identical key -> 200 OK replay
    second = await api_client.post(
        url,
        json={
            "white_house_ai_id": "house-ai-benchmark-baseline",
            "black_house_ai_id": "AIK-HOUSE-001",
            "execute_now": True,
        },
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["match_id"] == match_id
