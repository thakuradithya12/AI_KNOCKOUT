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
    category: Optional[str] = None  # "A" or "B"
    is_house_ai: bool = False  # House AI filling odd slot is not prize-eligible


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
    result_source: Optional[str] = None
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
    head_to_head_points: float = 0.0
    opponent_strength: float = 0.0  # Buchholz score (sum of opponents' points)
    buchholz: float = 0.0  # Explicit alias to opponent_strength
    sonneborn_berger: float = 0.0
    tie_break_score: float = 0.0  # Aligned to opponent_strength / Buchholz
    byes: int = 0
    needs_playoff: bool = False
    status: str = "ACTIVE"  # CHAMPION, FINALIST, SEMIFINALIST, ELIMINATED, QUALIFIED, ACTIVE
    category: Optional[str] = None  # "A" or "B"
    is_house_ai: bool = False  # True if participant is House AI filler
    promoted: bool = False  # True if promoted into qualification because House AI was ineligible


class TwoGamePairing(BaseModel):
    pairing_id: str
    round_number: int
    pairing_index: int
    stage_name: str = "SWISS"
    category: Optional[str] = None  # "A" or "B"
    participant1: TournamentParticipant
    participant2: Optional[TournamentParticipant] = None  # None indicates a bye
    is_bye: bool = False
    game1_match_id: Optional[uuid.UUID] = None  # P1 White, P2 Black
    game2_match_id: Optional[uuid.UUID] = None  # P2 White, P1 Black
    sudden_death_match_ids: list[uuid.UUID] = Field(default_factory=list)
    game1_result_source: Optional[str] = None
    game2_result_source: Optional[str] = None
    sudden_death_result_sources: list[str] = Field(default_factory=list)
    state: str = "QUEUED"  # QUEUED, IN_PROGRESS, COMPLETED
    p1_points: float = 0.0
    p2_points: float = 0.0
    winner_id: Optional[str] = None


class SwissRoundSchema(BaseModel):
    round_number: int
    round_name: str
    status: str = "PENDING"  # PENDING, IN_PROGRESS, COMPLETED
    pairings: list[TwoGamePairing] = Field(default_factory=list)


class PlayoffPairingSchema(BaseModel):
    pairing_id: str
    stage: str  # QUARTERFINAL, SEMIFINAL, FINAL, THIRD_PLACE_PLAYOFF
    pairing_index: int
    participant1: Optional[TournamentParticipant] = None
    participant2: Optional[TournamentParticipant] = None
    game1_match_id: Optional[uuid.UUID] = None
    game2_match_id: Optional[uuid.UUID] = None
    sudden_death_match_ids: list[uuid.UUID] = Field(default_factory=list)
    game1_result_source: Optional[str] = None
    game2_result_source: Optional[str] = None
    sudden_death_result_sources: list[str] = Field(default_factory=list)
    state: str = "QUEUED"  # QUEUED, IN_PROGRESS, COMPLETED
    p1_points: float = 0.0
    p2_points: float = 0.0
    winner_id: Optional[str] = None
    loser_id: Optional[str] = None


class PlayoffBracketSchema(BaseModel):
    tournament_id: str = "official-v1"
    status: str = "PENDING"  # PENDING, IN_PROGRESS, COMPLETED
    quarterfinals: list[PlayoffPairingSchema] = Field(default_factory=list)
    semifinals: list[PlayoffPairingSchema] = Field(default_factory=list)
    finals: list[PlayoffPairingSchema] = Field(default_factory=list)
    third_place: Optional[PlayoffPairingSchema] = None
    champion: Optional[TournamentParticipant] = None
    runner_up: Optional[TournamentParticipant] = None
    third_place_winner: Optional[TournamentParticipant] = None
    fourth_place: Optional[TournamentParticipant] = None
    byes: list[TournamentParticipant] = Field(default_factory=list)
    total_participants: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TournamentLeaderboard(BaseModel):
    tournament_id: str = "official-v1"
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    total_matches: int = 0
    standings: list[ParticipantStanding] = Field(default_factory=list)
    categories: dict[str, list[ParticipantStanding]] = Field(default_factory=dict)


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
    result_source: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    moves: list[dict] = Field(default_factory=list)


