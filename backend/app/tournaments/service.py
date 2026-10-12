"""Database service for tournament bracket retrieval, match scheduling, and leaderboard aggregation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.controller import MatchConfiguration
from app.matches.service import create_match
from app.models.competition import ArtifactState, Submission, SubmissionVersion
from app.models.event import BuildSession, EventControl, OfficialSubmissionSelection
from app.models.match import (
    Match,
    MatchPurpose,
    MatchSide,
    MatchSideColor,
    MatchSideKind,
    MatchState,
    ParticipantMatchSideBinding,
)
from app.models.participant import Participant
from app.tournaments.engine import (
    compute_leaderboard_standings,
    determine_match_winner,
    generate_single_elimination_bracket,
)
from app.tournaments.schemas import (
    SpectatorMatchDetail,
    TournamentBracket,
    TournamentLeaderboard,
    TournamentMatchPairing,
    TournamentParticipant,
)


async def get_frozen_official_participants(
    db: AsyncSession,
) -> list[TournamentParticipant]:
    """Retrieve all participants who have selected an eligible official submission version."""
    statement = (
        select(
            Participant.participant_id,
            Participant.name,
            Participant.team_name,
            Participant.created_at,
            SubmissionVersion.id.label("submission_version_id"),
            SubmissionVersion.version,
            SubmissionVersion.artifact_sha256,
            SubmissionVersion.build_duration_seconds,
        )
        .join(
            OfficialSubmissionSelection,
            OfficialSubmissionSelection.participant_id == Participant.participant_id,
        )
        .join(
            SubmissionVersion,
            SubmissionVersion.id == OfficialSubmissionSelection.submission_version_id,
        )
        .where(SubmissionVersion.state == ArtifactState.ELIGIBLE)
        .order_by(
            SubmissionVersion.build_duration_seconds.asc().nulls_last(),
            Participant.created_at.asc(),
        )
    )
    rows = (await db.execute(statement)).all()

    participants = []
    for seed, row in enumerate(rows, start=1):
        participants.append(
            TournamentParticipant(
                participant_id=row.participant_id,
                name=row.name,
                team_name=row.team_name,
                submission_version_id=row.submission_version_id,
                version=row.version,
                artifact_sha256=row.artifact_sha256,
                seed=seed,
            )
        )
    return participants


async def get_official_tournament_matches(
    db: AsyncSession,
) -> list[dict]:
    """Retrieve all matches created with purpose='OFFICIAL' and map their sides to participants."""
    statement = (
        select(
            Match.id.label("match_id"),
            Match.state,
            Match.result_outcome,
            Match.result_reason,
            Match.current_fen,
            Match.current_ply,
            Match.side_to_move,
            Match.initial_clock_ms,
            Match.increment_ms,
            Match.white_remaining_ms,
            Match.black_remaining_ms,
            Match.started_at,
            Match.completed_at,
            Match.created_at,
        )
        .where(Match.purpose == MatchPurpose.OFFICIAL)
        .order_by(Match.created_at.asc())
    )
    matches_raw = (await db.execute(statement)).all()

    matches = []
    for m in matches_raw:
        # Resolve white and black participant bindings
        white_binding = await db.scalar(
            select(ParticipantMatchSideBinding)
            .join(MatchSide, MatchSide.id == ParticipantMatchSideBinding.match_side_id)
            .where(
                MatchSide.match_id == m.match_id,
                MatchSide.color == MatchSideColor.WHITE,
            )
        )
        black_binding = await db.scalar(
            select(ParticipantMatchSideBinding)
            .join(MatchSide, MatchSide.id == ParticipantMatchSideBinding.match_side_id)
            .where(
                MatchSide.match_id == m.match_id,
                MatchSide.color == MatchSideColor.BLACK,
            )
        )
        white_p = await db.get(Participant, white_binding.participant_id) if white_binding else None
        black_p = await db.get(Participant, black_binding.participant_id) if black_binding else None

        matches.append(
            {
                "match_id": m.match_id,
                "state": m.state.value if hasattr(m.state, "value") else str(m.state),
                "result_outcome": m.result_outcome.value if hasattr(m.result_outcome, "value") else m.result_outcome,
                "result_reason": m.result_reason.value if hasattr(m.result_reason, "value") else m.result_reason,
                "current_fen": m.current_fen,
                "current_ply": m.current_ply,
                "side_to_move": m.side_to_move.value if hasattr(m.side_to_move, "value") else str(m.side_to_move),
                "initial_clock_ms": m.initial_clock_ms,
                "increment_ms": m.increment_ms,
                "white_remaining_ms": m.white_remaining_ms,
                "black_remaining_ms": m.black_remaining_ms,
                "white_participant_id": white_binding.participant_id if white_binding else None,
                "black_participant_id": black_binding.participant_id if black_binding else None,
                "white_name": white_p.name if white_p else "White Player",
                "black_name": black_p.name if black_p else "Black Player",
                "white_submission_version_id": white_binding.submission_version_id if white_binding else None,
                "black_submission_version_id": black_binding.submission_version_id if black_binding else None,
                "started_at": m.started_at,
                "completed_at": m.completed_at,
                "created_at": m.created_at,
            }
        )
    return matches


async def get_tournament_bracket(
    db: AsyncSession,
) -> TournamentBracket:
    """Generate and hydrate the tournament bracket with live persisted match records."""
    participants = await get_frozen_official_participants(db)
    bracket = generate_single_elimination_bracket(participants)

    if not participants or not bracket.rounds:
        return bracket

    # Hydrate round 1 pairings with existing official matches
    matches = await get_official_tournament_matches(db)
    matches_by_participants = {
        (m["white_participant_id"], m["black_participant_id"]): m
        for m in matches
    }

    round_1 = bracket.rounds[0]
    for pairing in round_1.pairings:
        if pairing.white and pairing.black:
            key = (pairing.white.participant_id, pairing.black.participant_id)
            rev_key = (pairing.black.participant_id, pairing.white.participant_id)
            match_data = matches_by_participants.get(key) or matches_by_participants.get(rev_key)
            if match_data:
                pairing.match_id = match_data["match_id"]
                pairing.state = match_data["state"]
                pairing.result_outcome = match_data["result_outcome"]
                pairing.result_reason = match_data["result_reason"]
                pairing.current_fen = match_data["current_fen"]
                pairing.current_ply = match_data["current_ply"]
                pairing.white_remaining_ms = match_data["white_remaining_ms"]
                pairing.black_remaining_ms = match_data["black_remaining_ms"]
                pairing.winner_participant_id = determine_match_winner(
                    match_data["result_outcome"],
                    match_data["white_participant_id"],
                    match_data["black_participant_id"],
                )

    # Check if tournament is completed and champion can be named
    final_round = bracket.rounds[-1]
    if final_round.pairings:
        final_pairing = final_round.pairings[0]
        if final_pairing.state == "COMPLETED" and final_pairing.winner_participant_id:
            bracket.status = "COMPLETED"
            bracket.champion = next(
                (p for p in participants if p.participant_id == final_pairing.winner_participant_id),
                None,
            )

    return bracket


async def get_tournament_leaderboard(
    db: AsyncSession,
) -> TournamentLeaderboard:
    """Compute real-time tournament standings across all official matches."""
    participants = await get_frozen_official_participants(db)
    matches = await get_official_tournament_matches(db)
    standings = compute_leaderboard_standings(participants, matches)

    return TournamentLeaderboard(
        tournament_id="official-v1",
        updated_at=datetime.now(timezone.utc),
        total_matches=len(matches),
        standings=standings,
    )


async def schedule_official_match(
    db: AsyncSession,
    *,
    white_submission_version_id: uuid.UUID,
    black_submission_version_id: uuid.UUID,
    configuration: MatchConfiguration,
) -> tuple[Match, tuple[MatchSide, MatchSide]]:
    """Create a tournament match with purpose='OFFICIAL'."""
    return await create_match(
        db,
        white_submission_version_id=white_submission_version_id,
        black_submission_version_id=black_submission_version_id,
        configuration=configuration,
        purpose=MatchPurpose.OFFICIAL,
    )
