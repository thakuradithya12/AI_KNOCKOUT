"""Comprehensive unit tests for the Unified House AI (AIK-HOUSE-001 v1.0.0).

Verifies:
1. Canonical USTAR archive structure and manifest.json compliance.
2. Participant intelligence.py contract API (choose_move, ChessAgent).
3. Opening book retrieval for standard opening setups.
4. Tactical capabilities: immediate checkmate detection and free piece capture.
5. Strict adherence to aik-jsonl/1 protocol over subprocess stdin/stdout.
6. Deterministic seeding, freezing, and active selection in the database.
7. Development match creation against AIK-HOUSE-001 with zero impact on Swiss standings.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timezone

import chess
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import ArtifactState, Submission, SubmissionVersion
from app.models.house_ai import EventHouseAISelection, HouseAIVersion
from app.models.match import Match, MatchPurpose, MatchSideColor, MatchState
from app.models.participant import Participant, ParticipantStatus
from app.services.development_matches import request_development_match
from app.services.house_ai_seeding import (
    seed_all_house_ais,
    seed_deterministic_house_ai_unified,
)
from app.services.house_ai_unified import (
    HOUSE_AI_ID,
    HOUSE_AI_VERSION,
    ChessAgent,
    choose_move,
    get_opening_book_move,
)
from app.services.trusted_engine_builder import (
    AIK_HOUSE_001_ENGINE_NAME,
    AIK_HOUSE_001_ENGINE_VERSION,
    AIK_HOUSE_001_ID,
    AIK_HOUSE_001_VERSION,
    build_aik_house_001_engine_archive,
)
from app.tournaments.engine import compute_leaderboard_standings
from app.tournaments.schemas import TournamentParticipant


def test_unified_house_ai_archive_integrity():
    """Verify canonical USTAR archive structure and manifest fields."""
    archive_bytes = build_aik_house_001_engine_archive()
    assert len(archive_bytes) > 0

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert "engine/main.py" in names
        assert "engine/intelligence.py" in names

        manifest_file = tar.extractfile("manifest.json")
        assert manifest_file is not None
        manifest = json.loads(manifest_file.read().decode("utf-8"))
        assert manifest["contract_version"] == "1"
        assert manifest["protocol_version"] == "aik-jsonl/1"
        assert manifest["engine_name"] == AIK_HOUSE_001_ENGINE_NAME
        assert manifest["engine_version"] == AIK_HOUSE_001_ENGINE_VERSION
        assert manifest["runtime"] == "python-3.12"
        assert manifest["entrypoint"] == "engine/main.py"


def test_unified_house_ai_intelligence_contract():
    """Verify participant contract API: choose_move and ChessAgent."""
    fen = chess.STARTING_FEN
    board = chess.Board(fen)
    legal_moves = sorted([m.uci() for m in board.legal_moves])

    # 1. Top-level choose_move function
    chosen = choose_move(fen, legal_moves, 1000)
    assert chosen in legal_moves
    assert isinstance(chosen, str)

    # 2. ChessAgent class interface
    agent = ChessAgent()
    assert agent.name == "AIK Unified House AI"
    assert agent.version == HOUSE_AI_VERSION
    chosen_agent = agent.choose_move(fen, legal_moves, 1000)
    assert chosen_agent in legal_moves

    # 3. Graceful handling of empty legal moves
    assert choose_move(fen, [], 1000) == "0000"

    # 4. Graceful handling of invalid FEN
    invalid_fallback = choose_move("invalid_fen_string", ["e2e4", "d2d4"], 1000)
    assert invalid_fallback in ["e2e4", "d2d4"]


def test_unified_house_ai_opening_book():
    """Verify opening book returns valid book moves on standard openings."""
    fen = chess.STARTING_FEN
    board = chess.Board(fen)
    legal_moves = [m.uci() for m in board.legal_moves]

    book_move = get_opening_book_move(fen, legal_moves)
    assert book_move is not None
    assert book_move in ["e2e4", "d2d4", "c2c4", "g1f3"]

    # After 1.e4
    board.push_san("e4")
    black_legal = [m.uci() for m in board.legal_moves]
    book_response = get_opening_book_move(board.fen(), black_legal)
    assert book_response is not None
    assert book_response in black_legal


def test_unified_house_ai_finds_mate_in_one():
    """Verify tactical search finds immediate checkmate (Scholar's Mate pattern)."""
    # 1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#
    board = chess.Board()
    for san in ["e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6"]:
        board.push_san(san)

    legal_moves = [m.uci() for m in board.legal_moves]
    chosen = choose_move(board.fen(), legal_moves, 2000)

    # Qxf7 is h5f7
    assert chosen == "h5f7", f"Expected checkmate h5f7, got {chosen}"
    board.push_uci(chosen)
    assert board.is_checkmate()


def test_unified_house_ai_captures_hanging_queen():
    """Verify positional evaluator and MVV-LVA captures undefended queen."""
    # Black blundered Queen to e4 where White's pawn on d3 can capture it (d3xe4)
    board = chess.Board("rnb1kbnr/pppp1ppp/8/8/4q3/3P4/PPP1PPPP/RNBQKBNR w KQkq - 0 1")
    legal_moves = [m.uci() for m in board.legal_moves]

    chosen = choose_move(board.fen(), legal_moves, 2000)
    assert chosen == "d3e4", f"Expected pawn to capture queen (d3e4), got {chosen}"


def test_unified_house_ai_subprocess_protocol():
    """Verify protocol over stdin/stdout with JSONL framing."""
    archive_bytes = build_aik_house_001_engine_archive()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tar:
        extracted = tar.extractfile("engine/main.py")
        assert extracted is not None
        code = extracted.read().decode("utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        commands = [
            {"type": "hello", "protocol_version": "aik-jsonl/1", "request_id": "r1"},
            {"type": "new_game", "color": "white", "request_id": "r2"},
            {"type": "position", "fen": chess.STARTING_FEN, "moves": [], "request_id": "r3"},
            {"type": "go", "clocks_ms": {"white": 5000, "black": 5000}, "request_id": "r4"},
            {"type": "stop", "request_id": "r5"},
        ]

        responses = []
        for cmd in commands:
            proc.stdin.write(json.dumps(cmd) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            assert line, f"Engine died on {cmd}"
            responses.append(json.loads(line.strip()))

        assert responses[0]["type"] == "ready"
        assert responses[0]["identity"] == AIK_HOUSE_001_ID
        assert responses[1]["type"] == "ack"
        assert responses[2]["type"] == "ack"
        assert responses[3]["type"] == "move"
        assert len(responses[3]["move"]) in [4, 5]  # UCI format
        assert responses[4]["type"] == "ack"
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=5.0)


@pytest.mark.asyncio
async def test_unified_house_ai_database_seeding(db_session: AsyncSession):
    """Verify database seeding, freezing, and active selection of AIK-HOUSE-001."""
    v_unified, sel_unified = await seed_deterministic_house_ai_unified(db_session, auto_activate=True)
    await db_session.commit()

    assert v_unified.house_ai_id == AIK_HOUSE_001_ID
    assert v_unified.version == AIK_HOUSE_001_VERSION
    assert v_unified.frozen_at is not None

    # Check database query
    stored = await db_session.scalar(
        select(HouseAIVersion).where(
            HouseAIVersion.house_ai_id == AIK_HOUSE_001_ID,
            HouseAIVersion.version == AIK_HOUSE_001_VERSION,
        )
    )
    assert stored is not None
    assert stored.artifact_sha256 == v_unified.artifact_sha256

    # Verify seed_all_house_ais returns all three
    all_seeded = await seed_all_house_ais(db_session)
    await db_session.commit()
    assert "baseline" in all_seeded
    assert "maximum_difficulty" in all_seeded
    assert "unified" in all_seeded
    assert all_seeded["active_selection"].house_ai_id == AIK_HOUSE_001_ID


@pytest.mark.asyncio
async def test_unified_house_ai_practice_match_workflow(db_session: AsyncSession):
    """Verify full practice match creation against AIK-HOUSE-001 and tournament isolation."""
    # 1. Seed house AIs
    await seed_all_house_ais(db_session)
    await db_session.commit()

    # 2. Create active participant and approved submission
    p_id = f"part_{uuid.uuid4().hex[:8]}"
    participant = Participant(
        participant_id=p_id,
        name="Practice Master",
        access_key_hash="hash",
        status=ParticipantStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(participant)

    sub_id = uuid.uuid4()
    submission = Submission(id=sub_id, participant_id=p_id, created_at=datetime.now(timezone.utc))
    db_session.add(submission)

    import hashlib
    from app.services.quarantine_storage import quarantine_storage

    archive_data = b"TEST_ARCHIVE_BYTES"
    digest = hashlib.sha256(archive_data).hexdigest()
    storage_key = await quarantine_storage.put(db_session, digest, archive_data)

    version_id = uuid.uuid4()
    now_ts = datetime.now(timezone.utc)
    sub_version = SubmissionVersion(
        id=version_id,
        submission_id=sub_id,
        participant_id=p_id,
        version=1,
        artifact_sha256=digest,
        artifact_size=len(archive_data),
        storage_key=storage_key,
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        state=ArtifactState.QUARANTINED,
        created_at=now_ts,
    )
    db_session.add(sub_version)
    await db_session.flush()

    from app.models.competition import ArtifactStateTransition

    db_session.add(
        ArtifactStateTransition(
            submission_version_id=version_id,
            from_state=ArtifactState.QUARANTINED.value,
            to_state=ArtifactState.INSPECTING,
            actor_kind="system",
            actor_id="test",
            reason_code="START_INSPECTION",
            recorded_at=now_ts,
        )
    )
    sub_version.state = ArtifactState.INSPECTING
    await db_session.flush()

    db_session.add(
        ArtifactStateTransition(
            submission_version_id=version_id,
            from_state=ArtifactState.INSPECTING.value,
            to_state=ArtifactState.VALIDATING,
            actor_kind="system",
            actor_id="test",
            reason_code="START_VALIDATION",
            recorded_at=now_ts,
        )
    )
    sub_version.state = ArtifactState.VALIDATING
    await db_session.flush()

    db_session.add(
        ArtifactStateTransition(
            submission_version_id=version_id,
            from_state=ArtifactState.VALIDATING.value,
            to_state=ArtifactState.APPROVED,
            actor_kind="system",
            actor_id="test",
            reason_code="VALIDATED",
            recorded_at=now_ts,
        )
    )
    sub_version.state = ArtifactState.APPROVED
    await db_session.commit()

    # 3. Request development match vs AIK-HOUSE-001
    idempotency_key = f"idem_{uuid.uuid4().hex}"
    match, created = await request_development_match(
        db_session,
        participant_id=p_id,
        submission_id=sub_id,
        version_number=1,
        idempotency_key=idempotency_key,
        side="WHITE",
        house_ai_id=AIK_HOUSE_001_ID,
    )
    await db_session.commit()

    assert created is True
    assert match.purpose == MatchPurpose.DEVELOPMENT
    assert match.state == MatchState.QUEUED

    # 4. Standings isolation: verify practice match never affects Swiss leaderboard
    t_part = TournamentParticipant(
        participant_id=p_id,
        name="Practice Master",
        team_name="Practice Team",
        submission_version_id=version_id,
        version=1,
        artifact_sha256="a" * 64,
        seed=1,
    )
    completed_practice = {
        "match_id": str(match.id),
        "purpose": "DEVELOPMENT",
        "state": "COMPLETED",
        "white_participant_id": p_id,
        "black_participant_id": AIK_HOUSE_001_ID,
        "result_outcome": "WHITE_WIN",
        "result_reason": "CHECKMATE",
    }
    # compute standings with practice match
    standings = compute_leaderboard_standings([t_part], [completed_practice])
    # Practice match must not yield tournament points
    assert standings[0].points == 0.0
    assert standings[0].wins == 0
