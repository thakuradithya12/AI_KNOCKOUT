"""Safe offline integration tests for trusted House AI vs House AI development matches.

Strictly verifies:
- Coordinator, controller, trusted engines, and persistence integration
- Move legality and board replay from initial FEN
- Final automated result and contract reasons
- Persisted match state, moves, outbox competition events, and serialization
- Zero execution of participant code, zero external network, zero Docker dependency
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import chess
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import CompetitionEvent
from app.models.match import (
    Match,
    MatchMove,
    MatchOutcome,
    MatchPurpose,
    MatchResultSource,
    MatchSide,
    MatchSideColor,
    MatchSideKind,
    MatchState,
)
from app.models.participant import Participant, ParticipantStatus
from app.services.development_matches import (
    DevelopmentMatchError,
    request_trusted_house_development_match,
    run_trusted_development_match,
    serialize_participant_development_match,
)
from app.services.house_ai_seeding import seed_all_house_ais
from app.services.trusted_engine_builder import TrustedEngineSession


async def _create_active_participant(db: AsyncSession) -> Participant:
    participant = Participant(
        participant_id=f"trusted-runner-{uuid.uuid4().hex[:12]}",
        name="Trusted House AI Runner",
        status=ParticipantStatus.ACTIVE,
        access_key_hash=hashlib.sha256(b"dummy-trusted-key").hexdigest(),
        created_at=datetime.now(timezone.utc),
    )
    db.add(participant)
    await db.commit()
    await db.refresh(participant)
    return participant


@pytest.mark.integration
async def test_trusted_house_ai_vs_house_ai_match_full_lifecycle(db_session: AsyncSession):
    """Verify full end-to-end lifecycle of trusted House AI vs House AI match."""
    # 1. Seed trusted House AIs and development policy
    await seed_all_house_ais(db_session)
    participant = await _create_active_participant(db_session)

    idempotency_key = f"trusted-duel-{uuid.uuid4().hex}"
    white_id = "house-ai-benchmark-baseline"
    black_id = "house-ai-maximum-difficulty"

    # 2. Request trusted development match
    match, created = await request_trusted_house_development_match(
        db_session,
        participant_id=participant.participant_id,
        idempotency_key=idempotency_key,
        white_house_ai_id=white_id,
        black_house_ai_id=black_id,
    )

    assert created is True
    assert match.state == MatchState.QUEUED
    assert match.purpose == MatchPurpose.DEVELOPMENT
    assert match.idempotency_participant_id == participant.participant_id
    assert match.current_ply == 0

    # Verify sides and bindings
    sides = (
        await db_session.execute(
            select(MatchSide).where(MatchSide.match_id == match.id).order_by(MatchSide.color)
        )
    ).scalars().all()
    assert len(sides) == 2
    assert all(s.kind == MatchSideKind.HOUSE_AI for s in sides)
    w_side = next(s for s in sides if s.color == MatchSideColor.WHITE)
    b_side = next(s for s in sides if s.color == MatchSideColor.BLACK)
    assert w_side.house_ai_binding.house_ai_id == white_id
    assert b_side.house_ai_binding.house_ai_id == black_id

    # Verify initial queue event
    queue_event = await db_session.scalar(
        select(CompetitionEvent).where(
            CompetitionEvent.aggregate_id == match.id,
            CompetitionEvent.event_type == "DEVELOPMENT_MATCH_QUEUED",
        )
    )
    assert queue_event is not None
    assert queue_event.metadata_json["white_house_ai_id"] == white_id
    assert queue_event.metadata_json["black_house_ai_id"] == black_id

    # 3. Run the trusted match with coordinator, controller, engines, and persistence
    completed = await run_trusted_development_match(
        db_session,
        match_id=match.id,
        max_plies=50,
    )

    # 4. Verify match terminal state and result
    assert completed.state == MatchState.COMPLETED
    assert completed.failure_reason is None
    assert completed.result_outcome in (
        MatchOutcome.WHITE_WIN.value,
        MatchOutcome.BLACK_WIN.value,
        MatchOutcome.DRAW.value,
    )
    assert completed.result_reason in (
        "CHECKMATE",
        "STALEMATE",
        "DEAD_POSITION",
        "FIVEFOLD_REPETITION",
        "SEVENTY_FIVE_MOVE_RULE",
        "THREEFOLD_REPETITION",
        "FIFTY_MOVE_RULE",
    )
    assert completed.result_source == MatchResultSource.AUTOMATED
    assert completed.started_at is not None
    assert completed.completed_at is not None
    assert completed.current_ply > 0

    # 5. Verify all persisted moves and replay chess board from initial FEN
    moves = (
        await db_session.execute(
            select(MatchMove).where(MatchMove.match_id == match.id).order_by(MatchMove.sequence.asc())
        )
    ).scalars().all()
    assert len(moves) == completed.current_ply

    replay_board = chess.Board(completed.initial_fen)
    for idx, move_row in enumerate(moves):
        assert move_row.sequence == idx + 1
        assert move_row.position_before_fen == replay_board.fen()
        # Verify move was strictly legal in the position
        chess_move = chess.Move.from_uci(move_row.move_uci)
        assert chess_move in replay_board.legal_moves, (
            f"Move {move_row.move_uci} at sequence {move_row.sequence} was not legal!"
        )
        replay_board.push(chess_move)
        assert move_row.position_after_fen == replay_board.fen()

    assert replay_board.fen() == completed.current_fen

    # 6. Verify competition lifecycle events recorded in order
    events = (
        await db_session.execute(
            select(CompetitionEvent)
            .where(CompetitionEvent.aggregate_id == match.id)
            .order_by(CompetitionEvent.aggregate_sequence.asc())
        )
    ).scalars().all()
    event_types = [e.event_type for e in events]

    assert "DEVELOPMENT_MATCH_QUEUED" in event_types
    assert "EXECUTION_BOUND" in event_types
    assert "EXECUTION_READY" in event_types
    assert "MATCH_STARTED" in event_types
    assert "MOVE_ACCEPTED" in event_types
    assert "MATCH_COMPLETED" in event_types

    # 7. Verify serialization
    serialized = await serialize_participant_development_match(db_session, completed)
    assert serialized["match_id"] == match.id
    assert serialized["state"] == "COMPLETED"
    assert serialized["white_house_ai"]["house_ai_id"] == white_id
    assert serialized["black_house_ai"]["house_ai_id"] == black_id
    assert serialized["result"]["outcome"] == completed.result_outcome
    assert serialized["result"]["reason"] == completed.result_reason
    assert serialized["result"]["failure"] is None
    assert len(serialized["moves"]) == completed.current_ply


@pytest.mark.integration
async def test_trusted_house_ai_match_checkmate_scenario(db_session: AsyncSession):
    """Verify trusted engine match with deterministic scholar's mate checkmate."""
    await seed_all_house_ais(db_session)
    participant = await _create_active_participant(db_session)

    match, created = await request_trusted_house_development_match(
        db_session,
        participant_id=participant.participant_id,
        idempotency_key=f"checkmate-{uuid.uuid4().hex}",
        white_house_ai_id="house-ai-benchmark-baseline",
        black_house_ai_id="AIK-HOUSE-001",
    )
    assert created is True

    # Scholar's mate:
    # 1. e4 e5
    # 2. Qh5 Nc6
    # 3. Bc4 Nf6
    # 4. Qxf7# (White wins by checkmate)
    white_moves = iter(["e2e4", "d1h5", "f1c4", "h5f7"])
    black_moves = iter(["e7e5", "b8c6", "g8f6"])

    def white_fn(fen, legal, budget):
        try:
            return next(white_moves)
        except StopIteration:
            return legal[0]

    def black_fn(fen, legal, budget):
        try:
            return next(black_moves)
        except StopIteration:
            return legal[0]

    w_engine = TrustedEngineSession("house-ai-benchmark-baseline", move_fn=white_fn)
    b_engine = TrustedEngineSession("AIK-HOUSE-001", move_fn=black_fn)

    completed = await run_trusted_development_match(
        db_session,
        match_id=match.id,
        white_engine=w_engine,
        black_engine=b_engine,
        max_plies=20,
    )

    assert completed.state == MatchState.COMPLETED
    assert completed.current_ply == 7
    assert completed.result_outcome == MatchOutcome.WHITE_WIN.value
    assert completed.result_reason == "CHECKMATE"
    assert completed.result_source == MatchResultSource.AUTOMATED
    assert completed.failure_reason is None

    # Verify board is in fact checkmate
    board = chess.Board(completed.current_fen)
    assert board.is_checkmate()
    assert board.turn == chess.BLACK  # Black was checkmated


@pytest.mark.integration
async def test_trusted_house_ai_match_idempotency_and_duplicate_guards(
    db_session: AsyncSession,
):
    """Verify duplicate side prevention, active match guards, and idempotency key reuse."""
    await seed_all_house_ais(db_session)
    participant = await _create_active_participant(db_session)

    # 1. Duplicate sides guard (same House AI on both sides)
    with pytest.raises(DevelopmentMatchError, match="DUPLICATE_HOUSE_AI_SIDE"):
        await request_trusted_house_development_match(
            db_session,
            participant_id=participant.participant_id,
            idempotency_key="dup-side-key",
            white_house_ai_id="house-ai-benchmark-baseline",
            black_house_ai_id="house-ai-benchmark-baseline",
        )

    # 2. Successful creation
    key = f"key-{uuid.uuid4().hex}"
    match1, created1 = await request_trusted_house_development_match(
        db_session,
        participant_id=participant.participant_id,
        idempotency_key=key,
        white_house_ai_id="house-ai-benchmark-baseline",
        black_house_ai_id="house-ai-maximum-difficulty",
    )
    assert created1 is True

    # 3. Idempotent retry returns existing match
    match_retry, created_retry = await request_trusted_house_development_match(
        db_session,
        participant_id=participant.participant_id,
        idempotency_key=key,
        white_house_ai_id="house-ai-benchmark-baseline",
        black_house_ai_id="house-ai-maximum-difficulty",
    )
    assert created_retry is False
    assert match_retry.id == match1.id

    # 4. Reusing idempotency key with different request raises error
    with pytest.raises(DevelopmentMatchError, match="IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST"):
        await request_trusted_house_development_match(
            db_session,
            participant_id=participant.participant_id,
            idempotency_key=key,
            white_house_ai_id="house-ai-benchmark-baseline",
            black_house_ai_id="AIK-HOUSE-001",
        )

    # 5. Active match guard (attempting another active match while first is queued)
    with pytest.raises(DevelopmentMatchError, match="ACTIVE_DEVELOPMENT_MATCH_EXISTS"):
        await request_trusted_house_development_match(
            db_session,
            participant_id=participant.participant_id,
            idempotency_key=f"different-key-{uuid.uuid4().hex}",
            white_house_ai_id="house-ai-benchmark-baseline",
            black_house_ai_id="AIK-HOUSE-001",
        )
