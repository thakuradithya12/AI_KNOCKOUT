"""Public tournament bracket, leaderboard, and live spectator API."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.match import Match
from app.tournaments.schemas import (
    PlayoffBracketSchema,
    SpectatorMatchDetail,
    TournamentBracket,
    TournamentLeaderboard,
)
from app.tournaments.service import (
    get_official_tournament_matches,
    get_six_player_finals_bracket,
    get_tournament_bracket,
    get_tournament_leaderboard,
)

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


@router.get("/bracket", response_model=TournamentBracket)
async def get_bracket(
    db: AsyncSession = Depends(get_db),
) -> TournamentBracket:
    """Retrieve the official tournament single-elimination bracket with live match statuses."""
    return await get_tournament_bracket(db)


@router.get("/leaderboard", response_model=TournamentLeaderboard)
async def get_leaderboard(
    db: AsyncSession = Depends(get_db),
) -> TournamentLeaderboard:
    """Retrieve official tournament standings computed with standard chess scoring and Sonneborn-Berger tie-breaks."""
    return await get_tournament_leaderboard(db)


@router.get("/categories")
async def get_tournament_categories(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Retrieve Category A and Category B standings and qualification progress."""
    leaderboard = await get_tournament_leaderboard(db)
    return {
        "tournament_id": leaderboard.tournament_id,
        "updated_at": leaderboard.updated_at,
        "total_matches": leaderboard.total_matches,
        "categories": leaderboard.categories,
    }


@router.get("/matches", response_model=list[dict])
async def list_tournament_matches(
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Retrieve all official tournament matches for spectator display."""
    try:
        return await get_official_tournament_matches(db)
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_MATCHES_UNAVAILABLE",
        ) from exc


@router.get("/matches/{match_id}", response_model=SpectatorMatchDetail)
async def get_spectator_match(
    match_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SpectatorMatchDetail:
    """Retrieve detailed real-time spectator board state and move history for a match."""
    matches = await get_official_tournament_matches(db)
    match_data = next((m for m in matches if str(m["match_id"]) == str(match_id)), None)
    if not match_data:
        from app.models.match import Match as MatchModel, MatchSide, MatchSideColor, MatchSideKind
        from app.models.participant import Participant
        from app.models.match import MatchPurpose

        raw_match = await db.get(MatchModel, match_id)
        # This endpoint is public: only official matches may be served. Practice and
        # organizer development matches stay private (participants read their own
        # through the authenticated development-match API).
        if raw_match is None or raw_match.purpose != MatchPurpose.OFFICIAL:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="TOURNAMENT_MATCH_NOT_FOUND",
            )
        sides = (
            await db.execute(
                select(MatchSide).where(MatchSide.match_id == match_id).order_by(MatchSide.color)
            )
        ).scalars().all()
        white_name = "White"
        black_name = "Black"
        for s in sides:
            name = "Unknown"
            if s.kind == MatchSideKind.PARTICIPANT and s.participant_binding:
                p = await db.scalar(select(Participant).where(Participant.participant_id == s.participant_binding.participant_id))
                name = p.name if p else s.participant_binding.participant_id
            elif s.kind == MatchSideKind.HOUSE_AI and s.house_ai_binding:
                name = f"House AI ({s.house_ai_binding.house_ai_id})"
            if s.color == MatchSideColor.WHITE:
                white_name = name
            else:
                black_name = name
        match_data = {
            "match_id": raw_match.id,
            "purpose": raw_match.purpose.value if hasattr(raw_match.purpose, "value") else str(raw_match.purpose),
            "state": raw_match.state.value if hasattr(raw_match.state, "value") else str(raw_match.state),
            "round_name": None,
            "white_name": white_name,
            "black_name": black_name,
            "current_fen": raw_match.current_fen,
            "current_ply": raw_match.current_ply,
            "side_to_move": raw_match.side_to_move.value if hasattr(raw_match.side_to_move, "value") else str(raw_match.side_to_move),
            "initial_clock_ms": raw_match.initial_clock_ms,
            "increment_ms": raw_match.increment_ms,
            "white_remaining_ms": raw_match.white_remaining_ms,
            "black_remaining_ms": raw_match.black_remaining_ms,
            "result_outcome": raw_match.result_outcome,
            "result_reason": raw_match.result_reason,
            "result_source": raw_match.result_source.value if hasattr(raw_match.result_source, "value") else raw_match.result_source,
            "started_at": raw_match.started_at,
            "completed_at": raw_match.completed_at,
        }

    from app.models.match import MatchMove
    moves_rows = (
        await db.execute(
            select(MatchMove).where(MatchMove.match_id == match_id).order_by(MatchMove.sequence.asc())
        )
    ).scalars().all()
    moves = [
        {
            "ply": m.sequence,
            "move_uci": m.move_uci,
            "from_square": m.move_uci[:2] if len(m.move_uci) >= 4 else None,
            "to_square": m.move_uci[2:4] if len(m.move_uci) >= 4 else None,
            "position_before_fen": m.position_before_fen,
            "position_after_fen": m.position_after_fen,
            "elapsed_ms": m.elapsed_ms,
            "recorded_at": m.recorded_at.isoformat() if m.recorded_at else None,
        }
        for m in moves_rows
    ]

    return SpectatorMatchDetail(
        match_id=match_data["match_id"],
        purpose=match_data.get("purpose", "OFFICIAL"),
        state=match_data["state"],
        round_name=match_data.get("round_name"),
        white_name=match_data["white_name"],
        black_name=match_data["black_name"],
        current_fen=match_data["current_fen"],
        current_ply=match_data["current_ply"],
        side_to_move=match_data["side_to_move"],
        initial_clock_ms=match_data["initial_clock_ms"],
        increment_ms=match_data["increment_ms"],
        white_remaining_ms=match_data["white_remaining_ms"],
        black_remaining_ms=match_data["black_remaining_ms"],
        result_outcome=match_data["result_outcome"],
        result_reason=match_data["result_reason"],
        result_source=match_data.get("result_source"),
        started_at=match_data["started_at"],
        completed_at=match_data["completed_at"],
        moves=moves,
    )


@router.get("/rounds")
async def list_tournament_rounds(
    stage_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Retrieve all tournament rounds (Swiss and Playoff) with their pairings and results."""
    from sqlalchemy import select
    from app.models.event import TournamentPairingRecord, TournamentRoundRecord
    from app.models.participant import Participant

    try:
        stmt = select(TournamentRoundRecord).order_by(
            TournamentRoundRecord.round_number.asc()
        )
        if stage_filter:
            stmt = stmt.where(TournamentRoundRecord.stage == stage_filter.upper())
        rounds = (await db.execute(stmt)).scalars().all()

        results = []
        for r in rounds:
            pairings = (
                await db.execute(
                    select(TournamentPairingRecord)
                    .where(TournamentPairingRecord.round_id == r.id)
                    .order_by(TournamentPairingRecord.pairing_index.asc())
                )
            ).scalars().all()

            round_match_ids = {
                match_id
                for pairing in pairings
                for match_id in (
                    pairing.game1_match_id,
                    pairing.game2_match_id,
                    *(pairing.sudden_death_match_ids or []),
                )
                if match_id is not None
            }
            source_rows = (
                await db.execute(
                    select(Match.id, Match.result_source).where(Match.id.in_(round_match_ids))
                )
            ).all() if round_match_ids else []
            source_by_id = {
                str(match_id): source.value if hasattr(source, "value") else source
                for match_id, source in source_rows
            }

            p_list = []
            for p in pairings:
                p1 = await db.scalar(select(Participant).where(Participant.participant_id == p.participant1_id))
                p2 = (
                    await db.scalar(select(Participant).where(Participant.participant_id == p.participant2_id))
                    if p.participant2_id else None
                )
                p_list.append({
                    "pairing_id": str(p.id),
                    "pairing_index": p.pairing_index,
                    "stage_name": p.stage_name,
                    "participant1_id": p.participant1_id,
                    "participant1_name": p1.name if p1 else "Player 1",
                    "participant2_id": p.participant2_id,
                    "participant2_name": p2.name if p2 else None,
                    "is_bye": p.is_bye,
                    "game1_match_id": str(p.game1_match_id) if p.game1_match_id else None,
                    "game2_match_id": str(p.game2_match_id) if p.game2_match_id else None,
                    "sudden_death_match_ids": p.sudden_death_match_ids or [],
                    "game1_result_source": (
                        source_by_id.get(str(p.game1_match_id)) if p.game1_match_id else None
                    ),
                    "game2_result_source": (
                        source_by_id.get(str(p.game2_match_id)) if p.game2_match_id else None
                    ),
                    "sudden_death_result_sources": [
                        source_by_id[str(match_id)]
                        for match_id in (p.sudden_death_match_ids or [])
                        if str(match_id) in source_by_id
                    ],
                    "status": p.status,
                    "winner_id": p.winner_id,
                    "p1_points": p.p1_points,
                    "p2_points": p.p2_points,
                })
            results.append({
                "round_id": str(r.id),
                "tournament_id": r.tournament_id,
                "stage": r.stage,
                "round_number": r.round_number,
                "round_name": r.round_name,
                "status": r.status,
                "pairings": p_list,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            })
        return results
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_ROUNDS_UNAVAILABLE",
        ) from exc


@router.get("/finals/bracket", response_model=PlayoffBracketSchema)
async def get_finals_bracket(
    tournament_id: str = "official-v1",
    db: AsyncSession = Depends(get_db),
) -> PlayoffBracketSchema:
    """Retrieve the official 6-player Finals bracket and progression state."""
    try:
        return await get_six_player_finals_bracket(db, tournament_id=tournament_id)
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FINALS_BRACKET_UNAVAILABLE",
        ) from exc


