"""Pydantic schemas for tournament bracket, pairings, scoring, and standings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class TournamentParticipant(BaseModel):
    participant_id: str
    name: str
    team_name: Optional[str] = None
    submission_version_id: uuid.UUID
    version: int
    artifact_sha256: str
    seed: int = 1


class TournamentMatchPairing(BaseModel):
    pairing_id: str
    match_id: Optional[uuid.UUID] = None
    round_index: int
    round_name: str
    white: Optional[TournamentParticipant] = None
    black: Optional[TournamentParticipant] = None
    state: str = "QUEUED"
    result_outcome: Optional[str] = None
    result_reason: Optional[str] = None
    winner_participant_id: Optional[str] = None
    current_fen: Optional[str] = None
    current_ply: int = 0
    white_remaining_ms: Optional[int] = None
    black_remaining_ms: Optional[int] = None


class TournamentRound(BaseModel):
    round_index: int
    round_name: str
    pairings: list[TournamentMatchPairing] = Field(default_factory=list)


class TournamentBracket(BaseModel):
    tournament_id: str = "official-v1"
    name: str = "AI Knockout Championship"
    status: str = "PENDING"  # PENDING, IN_PROGRESS, COMPLETED
    rounds: list[TournamentRound] = Field(default_factory=list)
    champion: Optional[TournamentParticipant] = None
    total_participants: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ParticipantStanding(BaseModel):
    rank: int
    participant_id: str
    name: str
    team_name: Optional[str] = None
    matches_played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    points: float = 0.0
    tie_break_score: float = 0.0
    status: str = "ACTIVE"  # CHAMPION, FINALIST, SEMIFINALIST, ELIMINATED, ACTIVE


class TournamentLeaderboard(BaseModel):
    tournament_id: str = "official-v1"
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    total_matches: int = 0
    standings: list[ParticipantStanding] = Field(default_factory=list)


class SpectatorMatchDetail(BaseModel):
    match_id: uuid.UUID
    purpose: str
    state: str
    round_name: Optional[str] = None
    white_name: str
    black_name: str
    current_fen: str
    current_ply: int
    side_to_move: str
    initial_clock_ms: int
    increment_ms: int
    white_remaining_ms: int
    black_remaining_ms: int
    result_outcome: Optional[str] = None
    result_reason: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
