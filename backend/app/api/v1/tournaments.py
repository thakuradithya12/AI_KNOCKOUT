"""Public tournament bracket, leaderboard, and live spectator API."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.tournaments.schemas import (
    SpectatorMatchDetail,
    TournamentBracket,
    TournamentLeaderboard,
)
from app.tournaments.service import (
    get_official_tournament_matches,
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


@router.get("/matches", response_model=list[dict])
async def list_tournament_matches(
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Retrieve all official tournament matches for spectator display."""
    return await get_official_tournament_matches(db)


@router.get("/matches/{match_id}", response_model=SpectatorMatchDetail)
async def get_spectator_match(
    match_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SpectatorMatchDetail:
    """Retrieve detailed real-time spectator board state for an official tournament match."""
    matches = await get_official_tournament_matches(db)
    match_data = next((m for m in matches if m["match_id"] == match_id), None)
    if not match_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="TOURNAMENT_MATCH_NOT_FOUND",
        )

    return SpectatorMatchDetail(
        match_id=match_data["match_id"],
        purpose="OFFICIAL",
        state=match_data["state"],
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
        started_at=match_data["started_at"],
        completed_at=match_data["completed_at"],
    )
