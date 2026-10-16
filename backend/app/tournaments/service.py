"""Database service for tournament bracket retrieval, Swiss rounds, playoff progression, and leaderboard aggregation."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

logger = logging.getLogger("ai-knockout.tournaments")

from app.matches.controller import MatchConfiguration
from app.matches.service import create_match
from app.models.competition import ArtifactState, Submission, SubmissionVersion
from app.models.event import (
    BuildSession,
    EventControl,
    OfficialSubmissionSelection,
    TournamentPairingRecord,
    TournamentRoundRecord,
    TournamentSnapshot,
)
from app.models.match import (
    Match,
    MatchOutcome,
    MatchPurpose,
    MatchSide,
    MatchSideColor,
    MatchSideKind,
    MatchState,
    ParticipantMatchSideBinding,
)
from app.models.participant import Participant, ParticipantStatus
from app.services.quarantine_storage import quarantine_storage
from app.tournaments.adjudication import adjudicate_match_record
from app.tournaments.engine import (
    CutoffTieError,
    compute_category_standings,
    compute_leaderboard_standings,
    determine_match_winner,
    evaluate_pairing_outcome,
    evaluate_pairing_two_games,
    generate_category_round_robin_pairings,
    generate_final_clash_bracket,
    generate_playoff_bracket,
    generate_qualification_swiss_pairings,
    generate_single_elimination_bracket,
    generate_six_player_finals_bracket,
    generate_swiss_pairings,
    partition_categories,
    select_final_clash_qualifiers,
    select_top_eight_qualifiers,
    select_top_six_qualifiers,
)
from app.tournaments.schemas import (
    ParticipantStanding,
    PlayoffBracketSchema,
    PlayoffPairingSchema,
    SpectatorMatchDetail,
    SwissRoundSchema,
    TournamentBracket,
    TournamentLeaderboard,
    TournamentMatchPairing,
    TournamentParticipant,
    TwoGamePairing,
)


def default_official_match_configuration(seed: Optional[str] = None) -> MatchConfiguration:
    """Return default match configuration for official tournament games."""
    return MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=180_000,
        increment_ms=2_000,
        move_timeout_ms=3_000,
        match_seed=seed or hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )


async def get_frozen_official_participants(
    db: AsyncSession,
    tournament_id: str = "official-v1",
) -> list[TournamentParticipant]:
    """Retrieve all participants frozen for the tournament.
    
    If a TournamentSnapshot exists, queries the immutable snapshot.
    Otherwise queries eligible selections (for pre-cutoff inspection).
    """
    # 1. Check if an immutable tournament snapshot exists
    snapshots = []
    try:
        snapshots = (
            await db.execute(
                select(TournamentSnapshot, Participant.name, Participant.team_name)
                .join(Participant, Participant.participant_id == TournamentSnapshot.participant_id)
                .where(TournamentSnapshot.tournament_id == tournament_id)
                .order_by(TournamentSnapshot.seed.asc())
            )
        ).all()
    except Exception:
        await db.rollback()
        snapshots = []

    if snapshots:
        return [
            TournamentParticipant(
                participant_id=snap.participant_id,
                name=name,
                team_name=team,
                submission_version_id=snap.submission_version_id,
                version=snap.version_number,
                artifact_sha256=snap.artifact_sha256,
                seed=snap.seed,
            )
            for snap, name, team in snapshots
        ]

    # 2. Pre-cutoff fallback: official submission selections with ELIGIBLE state
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


async def create_tournament_snapshot(
    db: AsyncSession,
    *,
    actor_id: str = "organizer-admin",
    tournament_id: str = "official-v1",
) -> list[TournamentSnapshot]:
    """Freeze an immutable snapshot of eligible participants for the official tournament."""
    existing = (
        await db.execute(
            select(TournamentSnapshot).where(TournamentSnapshot.tournament_id == tournament_id)
        )
    ).scalars().all()
    if existing:
        return list(existing)

    # Query eligible participants
    statement = (
        select(
            Participant.participant_id,
            SubmissionVersion.id.label("submission_version_id"),
            SubmissionVersion.version,
            SubmissionVersion.artifact_sha256,
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

    snapshots = []
    for seed, row in enumerate(rows, start=1):
        snap = TournamentSnapshot(
            tournament_id=tournament_id,
            participant_id=row.participant_id,
            submission_version_id=row.submission_version_id,
            version_number=row.version,
            artifact_sha256=row.artifact_sha256,
            seed=seed,
        )
        db.add(snap)
        snapshots.append(snap)

    try:
        await db.commit()
        return snapshots
    except IntegrityError:
        await db.rollback()
        existing = (
            await db.execute(
                select(TournamentSnapshot).where(TournamentSnapshot.tournament_id == tournament_id)
            )
        ).scalars().all()
        return list(existing)


async def get_official_tournament_matches(
    db: AsyncSession,
    stage_filter: Optional[str] = None,
) -> list[dict]:
    """Retrieve all matches created with purpose='OFFICIAL' and map their sides to participants.
    
    If stage_filter='SWISS', excludes matches created for playoff rounds to ensure Swiss
    qualification standings remain strictly separated from post-Swiss playoff results.
    """
    playoff_match_ids: set[uuid.UUID] = set()
    if stage_filter == "SWISS":
        p_rows = (
            await db.execute(
                select(
                    TournamentPairingRecord.game1_match_id,
                    TournamentPairingRecord.game2_match_id,
                    TournamentPairingRecord.sudden_death_match_ids,
                )
                .join(TournamentRoundRecord, TournamentRoundRecord.id == TournamentPairingRecord.round_id)
                .where(TournamentRoundRecord.stage == "PLAYOFF")
            )
        ).all()
        for g1, g2, sd_list in p_rows:
            if g1:
                playoff_match_ids.add(g1)
            if g2:
                playoff_match_ids.add(g2)
            if sd_list:
                for sd_id_str in sd_list:
                    try:
                        playoff_match_ids.add(uuid.UUID(sd_id_str))
                    except (ValueError, TypeError):
                        pass

    statement = (
        select(
            Match.id.label("match_id"),
            Match.state,
            Match.result_outcome,
            Match.result_reason,
            Match.result_source,
            Match.failure_reason,
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
    if playoff_match_ids:
        statement = statement.where(Match.id.not_in(playoff_match_ids))

    matches_raw = (await db.execute(statement)).all()

    matches = []
    for m in matches_raw:
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
        white_p = (
            await db.scalar(select(Participant).where(Participant.participant_id == white_binding.participant_id))
            if white_binding else None
        )
        black_p = (
            await db.scalar(select(Participant).where(Participant.participant_id == black_binding.participant_id))
            if black_binding else None
        )

        matches.append(
            {
                "match_id": m.match_id,
                "state": m.state.value if hasattr(m.state, "value") else str(m.state),
                "result_outcome": m.result_outcome.value if hasattr(m.result_outcome, "value") else m.result_outcome,
                "result_reason": m.result_reason.value if hasattr(m.result_reason, "value") else m.result_reason,
                "result_source": m.result_source.value if hasattr(m.result_source, "value") else m.result_source,
                "failure_reason": m.failure_reason.value if hasattr(m.failure_reason, "value") else m.failure_reason,
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


async def get_tournament_leaderboard(
    db: AsyncSession,
    tournament_id: str = "official-v1",
) -> TournamentLeaderboard:
    """Compute real-time tournament standings across official Swiss matches using approved tie-breaks."""
    try:
        participants = await get_frozen_official_participants(db, tournament_id)
        matches = await get_official_tournament_matches(db, stage_filter="SWISS")

        # Collect bye counts per participant from TournamentPairingRecord (only Swiss byes)
        byes_rows = (
            await db.execute(
                select(
                    TournamentPairingRecord.participant1_id,
                    func.count(TournamentPairingRecord.id),
                )
                .join(TournamentRoundRecord, TournamentRoundRecord.id == TournamentPairingRecord.round_id)
                .where(
                    TournamentPairingRecord.is_bye.is_(True),
                    TournamentRoundRecord.stage == "SWISS",
                )
                .group_by(TournamentPairingRecord.participant1_id)
            )
        ).all()
        byes_map = {r[0]: r[1] for r in byes_rows}

        standings = compute_leaderboard_standings(participants, matches, byes_map)

        # Compute Category A and Category B standings
        categories = {}
        if participants:
            house_a = await get_or_create_house_ai_filler_participant(db, category="A")
            house_b = await get_or_create_house_ai_filler_participant(db, category="B")
            cat_a_participants, cat_b_participants = partition_categories(
                participants,
                house_ai_filler=house_a,
                house_ai_filler_b=house_b,
            )

            pairing_stages = (
                await db.execute(
                    select(
                        TournamentPairingRecord.game1_match_id,
                        TournamentPairingRecord.game2_match_id,
                        TournamentPairingRecord.stage_name,
                    )
                    .join(TournamentRoundRecord, TournamentRoundRecord.id == TournamentPairingRecord.round_id)
                    .where(TournamentRoundRecord.stage == "SWISS")
                )
            ).all()
            match_to_stage = {}
            for g1, g2, stage_name in pairing_stages:
                if g1:
                    match_to_stage[str(g1)] = stage_name
                if g2:
                    match_to_stage[str(g2)] = stage_name

            matches_a = [
                m for m in matches
                if match_to_stage.get(str(m["match_id"])) == "CATEGORY_A"
                or (not match_to_stage.get(str(m["match_id"])) and any(p.participant_id == m["white_participant_id"] for p in cat_a_participants))
            ]
            matches_b = [
                m for m in matches
                if match_to_stage.get(str(m["match_id"])) == "CATEGORY_B"
                or (not match_to_stage.get(str(m["match_id"])) and any(p.participant_id == m["white_participant_id"] for p in cat_b_participants))
            ]

            standings_a = compute_category_standings(cat_a_participants, matches_a, category="A", byes_by_participant=byes_map)
            standings_b = compute_category_standings(cat_b_participants, matches_b, category="B", byes_by_participant=byes_map)

            p_map = {p.participant_id: p for p in (cat_a_participants + cat_b_participants)}
            select_final_clash_qualifiers(standings_a, standings_b, p_map)

            categories = {"A": standings_a, "B": standings_b}

        return TournamentLeaderboard(
            tournament_id=tournament_id,
            updated_at=datetime.now(timezone.utc),
            total_matches=len(matches),
            standings=standings,
            categories=categories,
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to compute tournament leaderboard: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_LEADERBOARD_UNAVAILABLE",
        ) from exc


# ============================================================================
# SWISS-SYSTEM ROUND SCHEDULING (TWO GAMES PER PAIRING WITH REVERSED COLORS)
# ============================================================================

async def schedule_swiss_round(
    db: AsyncSession,
    round_number: int,
    tournament_id: str = "official-v1",
    configuration: Optional[MatchConfiguration] = None,
    games_per_pairing: Optional[int] = None,
) -> TournamentRoundRecord:
    """Schedule a complete Swiss-system round.
    
    Supports:
    - Qualification mode (games_per_pairing=1 or tournament_id starting with 'qualification'):
      1 scheduled game per pairing with White/Black balance tracking, anti-repeat opponent,
      and 1.0 bye points.
    - Legacy 2-game Swiss mode (games_per_pairing=2 or default):
      2 games per pairing with reversed colors and 2.0 bye points.
    
    Idempotent: If round_number already exists, returns the existing record
    without duplicating matches.
    """
    # 1. Check if round already exists (idempotency check)
    existing_round = await db.scalar(
        select(TournamentRoundRecord).where(
            TournamentRoundRecord.tournament_id == tournament_id,
            TournamentRoundRecord.stage == "SWISS",
            TournamentRoundRecord.round_number == round_number,
        )
    )
    if existing_round is not None:
        return existing_round

    # Freeze participant versions before Round 1 if not already frozen
    if round_number == 1:
        await create_tournament_snapshot(db, tournament_id=tournament_id)

    # 2. Retrieve participants and history
    participants = await get_frozen_official_participants(db, tournament_id)
    if len(participants) < 2:
        raise ValueError("INSUFFICIENT_PARTICIPANTS_FOR_SWISS_ROUND")

    completed_matches = await get_official_tournament_matches(db, stage_filter="SWISS")

    # 3. Retrieve historical byes
    byes_rows = (
        await db.execute(
            select(
                TournamentPairingRecord.participant1_id,
                func.count(TournamentPairingRecord.id),
            )
            .where(TournamentPairingRecord.is_bye.is_(True))
            .group_by(TournamentPairingRecord.participant1_id)
        )
    ).all()
    byes_map = {r[0]: r[1] for r in byes_rows}

    # 4. Generate Swiss pairings
    is_qualification = (games_per_pairing == 1 or tournament_id.startswith("qualification"))
    if is_qualification:
        pairings = generate_qualification_swiss_pairings(participants, completed_matches, round_number, byes_map)
    else:
        pairings = generate_swiss_pairings(participants, completed_matches, round_number, byes_map)

    try:
        # 5. Create round record
        round_record = TournamentRoundRecord(
            id=uuid.uuid4(),
            tournament_id=tournament_id,
            stage="SWISS",
            round_number=round_number,
            round_name=f"Swiss Round {round_number}",
            status="IN_PROGRESS",
        )
        db.add(round_record)
        await db.flush()

        # 6. Schedule games per pairing
        for pairing in pairings:
            if pairing.is_bye:
                bye_pts = 1.0 if is_qualification else 2.0
                p_record = TournamentPairingRecord(
                    id=uuid.uuid4(),
                    round_id=round_record.id,
                    pairing_index=pairing.pairing_index,
                    stage_name="SWISS",
                    participant1_id=pairing.participant1.participant_id,
                    participant2_id=None,
                    is_bye=True,
                    game1_match_id=None,
                    game2_match_id=None,
                    status="COMPLETED",
                    winner_id=pairing.participant1.participant_id,
                    p1_points=bye_pts,
                    p2_points=0.0,
                )
                db.add(p_record)
            else:
                p1 = pairing.participant1
                p2 = pairing.participant2
                assert p2 is not None

                cfg1 = configuration or default_official_match_configuration()
                m1, _ = await create_match(
                    db,
                    white_submission_version_id=p1.submission_version_id,
                    black_submission_version_id=p2.submission_version_id,
                    configuration=cfg1,
                    purpose=MatchPurpose.OFFICIAL,
                )

                if is_qualification:
                    # Single scheduled game with balanced colors
                    p_record = TournamentPairingRecord(
                        id=uuid.uuid4(),
                        round_id=round_record.id,
                        pairing_index=pairing.pairing_index,
                        stage_name="SWISS",
                        participant1_id=p1.participant_id,
                        participant2_id=p2.participant_id,
                        is_bye=False,
                        game1_match_id=m1.id,
                        game2_match_id=None,
                        status="QUEUED",
                        p1_points=0.0,
                        p2_points=0.0,
                    )
                    db.add(p_record)
                else:
                    # Legacy 2-game series with reversed colors
                    cfg2 = configuration or default_official_match_configuration()
                    m2, _ = await create_match(
                        db,
                        white_submission_version_id=p2.submission_version_id,
                        black_submission_version_id=p1.submission_version_id,
                        configuration=cfg2,
                        purpose=MatchPurpose.OFFICIAL,
                    )

                    p_record = TournamentPairingRecord(
                        id=uuid.uuid4(),
                        round_id=round_record.id,
                        pairing_index=pairing.pairing_index,
                        stage_name="SWISS",
                        participant1_id=p1.participant_id,
                        participant2_id=p2.participant_id,
                        is_bye=False,
                        game1_match_id=m1.id,
                        game2_match_id=m2.id,
                        status="QUEUED",
                        p1_points=0.0,
                        p2_points=0.0,
                    )
                    db.add(p_record)

        await db.commit()
        await db.refresh(round_record)
        return round_record
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(
            select(TournamentRoundRecord).where(
                TournamentRoundRecord.tournament_id == tournament_id,
                TournamentRoundRecord.stage == "SWISS",
                TournamentRoundRecord.round_number == round_number,
            )
        )
        if existing is not None:
            return existing
        raise


# ============================================================================
# PLAYOFF BRACKET INITIALIZATION & PROGRESSION (OFFICIAL 6-PLAYER FINALS)
# ============================================================================

async def initialize_playoff_bracket(
    db: AsyncSession,
    tournament_id: str = "official-v1",
    configuration: Optional[MatchConfiguration] = None,
    allow_cutoff_fallback: bool = False,
    games_per_matchup: int = 2,
) -> TournamentRoundRecord:
    """Initialize the official 6-player Finals bracket for qualifiers after the Swiss stage.
    
    Structure:
    - Qualifiers: Exactly Top 6 qualifiers selected by official qualification standings and tie-breaks.
    - Quarterfinals: Seed 4 vs Seed 5 (QF1), Seed 3 vs Seed 6 (QF2).
    - Semifinals byes: Seeds 1 and 2 advance directly to Semifinals.
    - Matches: Two games per matchup with reversed colors by default.
    - Idempotent: If round 1 of playoffs is already created, returns existing record.
    - Rejects with ValueError if fewer than 6 eligible qualifiers exist.
    - Rejects with CutoffTieError if unresolved tie affects the 6th-place qualification cutoff.
    """
    return await initialize_six_player_finals(
        db,
        tournament_id=tournament_id,
        configuration=configuration,
        allow_cutoff_fallback=allow_cutoff_fallback,
        games_per_matchup=games_per_matchup,
    )


async def advance_playoff_stage(
    db: AsyncSession,
    tournament_id: str = "official-v1",
    configuration: Optional[MatchConfiguration] = None,
    games_per_matchup: int = 2,
) -> Optional[TournamentRoundRecord]:
    """Inspect active 6-player Finals round, evaluate outcomes, advance winners and losers:
    - QF winners -> Semifinals (Seed 1 vs Winner(QF1 - 4 vs 5), Seed 2 vs Winner(QF2 - 3 vs 6))
    - SF winners -> Grand Final (determines 1st and 2nd place)
    - SF losers -> Third-Place Playoff (determines 3rd place)
    - Idempotent: Returns completed round if all rounds finished.
    """
    return await advance_six_player_finals(
        db,
        tournament_id=tournament_id,
        configuration=configuration,
        games_per_matchup=games_per_matchup,
    )



# ============================================================================
# SPECTATOR BRACKET & TOURNAMENT VIEW
# ============================================================================

async def get_tournament_bracket(
    db: AsyncSession,
) -> TournamentBracket:
    """Generate and hydrate the tournament bracket with live persisted match records."""
    try:
        participants = await get_frozen_official_participants(db)
        bracket = generate_single_elimination_bracket(participants)

        if not participants or not bracket.rounds:
            return bracket

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
                    pairing.result_source = match_data["result_source"]
                    pairing.current_fen = match_data["current_fen"]
                    pairing.current_ply = match_data["current_ply"]
                    pairing.white_remaining_ms = match_data["white_remaining_ms"]
                    pairing.black_remaining_ms = match_data["black_remaining_ms"]
                    pairing.winner_participant_id = determine_match_winner(
                        match_data["result_outcome"],
                        match_data["white_participant_id"],
                        match_data["black_participant_id"],
                    )

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
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOURNAMENT_BRACKET_UNAVAILABLE",
        ) from exc


async def schedule_official_match(
    db: AsyncSession,
    *,
    white_submission_version_id: uuid.UUID,
    black_submission_version_id: uuid.UUID,
    configuration: MatchConfiguration,
    dispatch_jobs: bool = False,
    execution_mode: str = "MODAL_AI_EXECUTION",
) -> tuple[Match, tuple[MatchSide, MatchSide]]:
    """Create a tournament match with purpose='OFFICIAL' and optionally dispatch execution jobs."""
    match, sides = await create_match(
        db,
        white_submission_version_id=white_submission_version_id,
        black_submission_version_id=black_submission_version_id,
        configuration=configuration,
        purpose=MatchPurpose.OFFICIAL,
    )
    if dispatch_jobs:
        from app.workers.service import dispatch_participant_match_jobs
        await dispatch_participant_match_jobs(
            db, match_id=match.id, execution_mode=execution_mode
        )
    return match, sides


# ============================================================================
# CATEGORY A & B ROUND-ROBIN SCHEDULING (TWO GAMES PER PAIRING)
# ============================================================================

async def get_or_create_house_ai_filler_participant(
    db: AsyncSession,
    category: Optional[str] = None,
) -> Optional[TournamentParticipant]:
    """Ensure a deterministic House AI filler participant with an eligible submission exists.
    
    Used exclusively to fill an odd-sized category in official round-robin
    so that every human participant plays every round.
    House AI is flagged with is_house_ai=True and is NOT prize-eligible.
    """
    if category:
        cat_suffix = f"-cat-{category.lower()}"
        house_ai_id = f"house-ai-baseline{cat_suffix}"
        disp_name = f"House AI Baseline (Cat {category.upper()})"
        email = f"house-ai-filler-{category.lower()}@internal.aiknockout.com"
        reg_source = "REGISTRATION"
        corr_id = f"house-ai-filler-{category.lower()}-init"
        app_ref = f"house-ai-filler-{category.lower()}"
    else:
        house_ai_id = "house-ai-baseline-filler"
        disp_name = "House AI Baseline"
        email = "house-ai-filler@internal.aiknockout.com"
        reg_source = "REGISTRATION"
        corr_id = "house-ai-filler-init"
        app_ref = "house-ai-filler"

    part = await db.scalar(
        select(Participant).where(Participant.participant_id == house_ai_id)
    )
    if part is None:
        part = Participant(
            participant_id=house_ai_id,
            name=disp_name,
            team_name="AI Knockout Benchmark",
            email=email,
            access_key_hash=f"{house_ai_id}_immutable_key",
            status=ParticipantStatus.ACTIVE,
            created_at=datetime.now(timezone.utc),
        )
        db.add(part)
        await db.flush()

    sub = await db.scalar(
        select(Submission).where(Submission.participant_id == house_ai_id)
    )
    if sub is None:
        sub = Submission(
            id=uuid.uuid4(),
            participant_id=house_ai_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(sub)
        await db.flush()

    sub_ver = await db.scalar(
        select(SubmissionVersion).where(
            SubmissionVersion.submission_id == sub.id,
            SubmissionVersion.version == 1,
        )
    )
    if sub_ver is None:
        from app.services.trusted_engine_builder import build_trusted_engine_archive
        from app.services.submission_validation import DEFAULT_LIMITS
        from app.services.submissions import accept_upload, approve_submission_for_eligibility, OrganizerApproval

        b_sess = await db.scalar(
            select(BuildSession).where(BuildSession.participant_id == house_ai_id)
        )
        if b_sess is None:
            db.add(BuildSession(
                participant_id=house_ai_id,
                started_at=datetime.now(timezone.utc),
                start_source=reg_source,
            ))
            await db.flush()

        archive = build_trusted_engine_archive()
        upload = await accept_upload(
            db,
            participant_id=house_ai_id,
            submission_id=sub.id,
            archive_bytes=archive,
            correlation_id=corr_id,
            limits=DEFAULT_LIMITS,
            bypass_freeze=True,
        )
        sub_ver = upload.version
        await approve_submission_for_eligibility(
            db,
            submission_version_id=sub_ver.id,
            approval=OrganizerApproval(
                actor_id="system-tournament-seeder",
                reason_code="HOUSE_AI_BASELINE_APPROVED",
                approval_reference=app_ref,
            ),
        )
        await db.flush()

    return TournamentParticipant(
        participant_id=house_ai_id,
        name=part.name,
        team_name=part.team_name,
        submission_version_id=sub_ver.id,
        version=sub_ver.version,
        artifact_sha256=sub_ver.artifact_sha256,
        seed=999,
        is_house_ai=True,
    )


async def schedule_category_round_robin_round(
    db: AsyncSession,
    category: str,
    round_number: int,
    tournament_id: str = "official-v1",
    configuration: Optional[MatchConfiguration] = None,
    use_house_ai_filler: bool = True,
) -> TournamentRoundRecord:
    """Schedule round N of the Category A or Category B round-robin stage.
    
    Rules enforced:
    - Round-robin within each category (up to 8 participants).
    - Each opponent pairing schedules TWO games with reversed colors:
      - Game 1: P1 White, P2 Black
      - Game 2: P2 White, P1 Black
    - Terminal state check: Cannot schedule round R until all matches in round R-1
      are completed or aborted.
    - Idempotent: If round N is already scheduled, returns existing record.
    - If odd participant count and use_house_ai_filler=True, fills with fixed House AI baseline.
      Otherwise lowest seed receives a bye (2.0 pts).
    """
    category = category.upper().strip()
    if category not in {"A", "B"}:
        raise ValueError(f"INVALID_CATEGORY: Category must be 'A' or 'B', got {category}")

    cat_tournament_id = f"{tournament_id}:CAT_{category}"

    # 1. Idempotency check:
    round_name = f"Category {category} - Round {round_number}"
    existing_round = await db.scalar(
        select(TournamentRoundRecord).where(
            TournamentRoundRecord.tournament_id == cat_tournament_id,
            TournamentRoundRecord.stage == "SWISS",
            TournamentRoundRecord.round_number == round_number,
        )
    )
    if existing_round is not None:
        return existing_round

    # 2. Terminal state check: Previous round R-1 must be completed
    if round_number > 1:
        prev_name = f"Category {category} - Round {round_number - 1}"
        prev_round = await db.scalar(
            select(TournamentRoundRecord).where(
                TournamentRoundRecord.tournament_id == cat_tournament_id,
                TournamentRoundRecord.stage == "SWISS",
                TournamentRoundRecord.round_number == round_number - 1,
            )
        )
        if prev_round is None:
            raise ValueError(f"PREVIOUS_ROUND_NOT_FOUND: {prev_name} must be scheduled before round {round_number}")

        # Check all matches in prev_round
        prev_pairings = (
            await db.execute(
                select(TournamentPairingRecord).where(
                    TournamentPairingRecord.round_id == prev_round.id
                )
            )
        ).scalars().all()

        for p in prev_pairings:
            if p.is_bye:
                continue
            m1 = await db.get(Match, p.game1_match_id) if p.game1_match_id else None
            m2 = await db.get(Match, p.game2_match_id) if p.game2_match_id else None
            if m1 and m1.state not in {MatchState.COMPLETED, MatchState.ABORTED}:
                raise ValueError(
                    f"PREVIOUS_ROUND_NOT_COMPLETED: Match {m1.id} in {prev_name} is currently {m1.state.value if hasattr(m1.state, 'value') else m1.state}"
                )
            if m2 and m2.state not in {MatchState.COMPLETED, MatchState.ABORTED}:
                raise ValueError(
                    f"PREVIOUS_ROUND_NOT_COMPLETED: Match {m2.id} in {prev_name} is currently {m2.state.value if hasattr(m2.state, 'value') else m2.state}"
                )

        if prev_round.status != "COMPLETED":
            prev_round.status = "COMPLETED"
            prev_round.completed_at = datetime.now(timezone.utc)
            await db.flush()

    # 3. Retrieve participants and partition into categories
    participants = await get_frozen_official_participants(db, tournament_id)
    if len(participants) < 2:
        raise ValueError("INSUFFICIENT_PARTICIPANTS_FOR_ROUND_ROBIN")

    house_ai_filler_a = None
    house_ai_filler_b = None
    if use_house_ai_filler:
        house_ai_filler_a = await get_or_create_house_ai_filler_participant(db, category="A")
        house_ai_filler_b = await get_or_create_house_ai_filler_participant(db, category="B")

    cat_a, cat_b = partition_categories(
        participants,
        house_ai_filler=house_ai_filler_a,
        house_ai_filler_b=house_ai_filler_b,
    )
    pool = cat_a if category == "A" else cat_b

    if len(pool) < 2:
        raise ValueError(f"INSUFFICIENT_PARTICIPANTS_IN_CATEGORY_{category}")

    # 4. Generate round-robin pairings for round_number
    pairings = generate_category_round_robin_pairings(pool, round_number, category=category)

    try:
        round_record = TournamentRoundRecord(
            id=uuid.uuid4(),
            tournament_id=cat_tournament_id,
            stage="SWISS",
            round_number=round_number,
            round_name=round_name,
            status="IN_PROGRESS",
        )
        db.add(round_record)
        await db.flush()

        for pairing in pairings:
            if pairing.is_bye:
                p_record = TournamentPairingRecord(
                    id=uuid.uuid4(),
                    round_id=round_record.id,
                    pairing_index=pairing.pairing_index,
                    stage_name=f"CATEGORY_{category}",
                    participant1_id=pairing.participant1.participant_id,
                    participant2_id=None,
                    is_bye=True,
                    game1_match_id=None,
                    game2_match_id=None,
                    status="COMPLETED",
                    winner_id=pairing.participant1.participant_id,
                    p1_points=2.0,
                    p2_points=0.0,
                )
                db.add(p_record)
            else:
                p1 = pairing.participant1
                p2 = pairing.participant2
                assert p2 is not None

                cfg1 = configuration or default_official_match_configuration()
                m1, _ = await create_match(
                    db,
                    white_submission_version_id=p1.submission_version_id,
                    black_submission_version_id=p2.submission_version_id,
                    configuration=cfg1,
                    purpose=MatchPurpose.OFFICIAL,
                )

                cfg2 = configuration or default_official_match_configuration()
                m2, _ = await create_match(
                    db,
                    white_submission_version_id=p2.submission_version_id,
                    black_submission_version_id=p1.submission_version_id,
                    configuration=cfg2,
                    purpose=MatchPurpose.OFFICIAL,
                )

                p_record = TournamentPairingRecord(
                    id=uuid.uuid4(),
                    round_id=round_record.id,
                    pairing_index=pairing.pairing_index,
                    stage_name=f"CATEGORY_{category}",
                    participant1_id=p1.participant_id,
                    participant2_id=p2.participant_id,
                    is_bye=False,
                    game1_match_id=m1.id,
                    game2_match_id=m2.id,
                    status="QUEUED",
                    p1_points=0.0,
                    p2_points=0.0,
                )
                db.add(p_record)

        await db.commit()
        await db.refresh(round_record)
        return round_record
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(
            select(TournamentRoundRecord).where(
                TournamentRoundRecord.tournament_id == cat_tournament_id,
                TournamentRoundRecord.stage == "SWISS",
                TournamentRoundRecord.round_number == round_number,
            )
        )
        if existing is not None:
            return existing
        raise


# ============================================================================
# FINAL CLASH INITIALIZATION & PROGRESSION (CATEGORY ADVANCEMENT & BYES)
# ============================================================================

async def initialize_final_clash(
    db: AsyncSession,
    tournament_id: str = "official-v1",
    configuration: Optional[MatchConfiguration] = None,
) -> TournamentRoundRecord:
    """Initialize the Final Clash 6-player playoff bracket.
    
    Structure:
    - Top 3 eligible participants from Category A and Category B advance.
    - Category A #1 and Category B #1 earn byes into the Semifinals.
    - Quarterfinals:
      - QF1: Category A #2 vs Category B #3 (2 games with reversed colors)
      - QF2: Category B #2 vs Category A #3 (2 games with reversed colors)
    - House AI Ineligibility Rule:
      - If House AI placed in top 3 of either category, the next eligible participant
        (rank 4) is promoted.
    """
    # 1. Idempotency check
    existing = await db.scalar(
        select(TournamentRoundRecord).where(
            TournamentRoundRecord.tournament_id == tournament_id,
            TournamentRoundRecord.stage == "PLAYOFF",
        )
    )
    if existing is not None:
        return existing

    # 2. Compute category standings
    leaderboard = await get_tournament_leaderboard(db, tournament_id)
    cat_a_standings = leaderboard.categories.get("A", [])
    cat_b_standings = leaderboard.categories.get("B", [])

    participants = await get_frozen_official_participants(db, tournament_id)
    house_a = await get_or_create_house_ai_filler_participant(db, category="A")
    house_b = await get_or_create_house_ai_filler_participant(db, category="B")
    all_participants = list(participants)
    if house_a:
        all_participants.append(house_a)
    if house_b:
        all_participants.append(house_b)
    p_dict = {p.participant_id: p for p in all_participants}

    qualifiers_a, qualifiers_b = select_final_clash_qualifiers(cat_a_standings, cat_b_standings, p_dict)

    if not qualifiers_a or not qualifiers_b:
        raise ValueError("INSUFFICIENT_QUALIFIERS_FOR_FINAL_CLASH")

    bracket = generate_final_clash_bracket(qualifiers_a, qualifiers_b)

    # 3. Determine initial playoff round
    if bracket.quarterfinals:
        round_name = "Final Clash - Quarterfinals"
        active_pairings = bracket.quarterfinals
        stage_name = "QUARTERFINAL"
    elif bracket.semifinals:
        round_name = "Final Clash - Semifinals"
        active_pairings = bracket.semifinals
        stage_name = "SEMIFINAL"
    elif bracket.finals:
        round_name = "Final Clash - Finals"
        active_pairings = bracket.finals
        stage_name = "FINAL"
    else:
        raise ValueError("INSUFFICIENT_QUALIFIERS_FOR_FINAL_CLASH")

    round_record = TournamentRoundRecord(
        id=uuid.uuid4(),
        tournament_id=tournament_id,
        stage="PLAYOFF",
        round_number=1,
        round_name=round_name,
        status="IN_PROGRESS",
    )
    db.add(round_record)
    await db.flush()

    # Active matchups for initial round
    for pairing in active_pairings:
        p1 = pairing.participant1
        p2 = pairing.participant2
        assert p1 is not None and p2 is not None

        cfg1 = configuration or default_official_match_configuration()
        m1, _ = await create_match(
            db,
            white_submission_version_id=p1.submission_version_id,
            black_submission_version_id=p2.submission_version_id,
            configuration=cfg1,
            purpose=MatchPurpose.OFFICIAL,
        )

        cfg2 = configuration or default_official_match_configuration()
        m2, _ = await create_match(
            db,
            white_submission_version_id=p2.submission_version_id,
            black_submission_version_id=p1.submission_version_id,
            configuration=cfg2,
            purpose=MatchPurpose.OFFICIAL,
        )

        p_record = TournamentPairingRecord(
            id=uuid.uuid4(),
            round_id=round_record.id,
            pairing_index=pairing.pairing_index,
            stage_name=stage_name,
            participant1_id=p1.participant_id,
            participant2_id=p2.participant_id,
            is_bye=False,
            game1_match_id=m1.id,
            game2_match_id=m2.id,
            status="QUEUED",
            p1_points=0.0,
            p2_points=0.0,
        )
        db.add(p_record)

    await db.commit()
    await db.refresh(round_record)
    return round_record


async def advance_final_clash_stage(
    db: AsyncSession,
    tournament_id: str = "official-v1",
    configuration: Optional[MatchConfiguration] = None,
) -> Optional[TournamentRoundRecord]:
    """Inspect active Final Clash stage, evaluate 2-game outcomes, handle sudden death if tied,
    or advance to Semifinals (matching with A1 and B1 bye seeds) or Finals.
    """
    # 1. Find active Final Clash playoff round
    active_round = await db.scalar(
        select(TournamentRoundRecord)
        .where(
            TournamentRoundRecord.tournament_id == tournament_id,
            TournamentRoundRecord.stage == "PLAYOFF",
            TournamentRoundRecord.status == "IN_PROGRESS",
        )
        .order_by(TournamentRoundRecord.round_number.desc())
    )
    if active_round is None:
        return None

    pairings = (
        await db.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == active_round.id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()

    participants = await get_frozen_official_participants(db, tournament_id)
    house_a = await get_or_create_house_ai_filler_participant(db, category="A")
    house_b = await get_or_create_house_ai_filler_participant(db, category="B")
    all_participants = list(participants)
    if house_a:
        all_participants.append(house_a)
    if house_b:
        all_participants.append(house_b)
    p_dict = {p.participant_id: p for p in all_participants}

    all_pairings_decided = True
    winners_in_order: list[TournamentParticipant] = []

    for pairing in pairings:
        if pairing.status == "COMPLETED" and pairing.winner_id:
            winners_in_order.append(p_dict[pairing.winner_id])
            continue

        m1 = await db.get(Match, pairing.game1_match_id) if pairing.game1_match_id else None
        m2 = await db.get(Match, pairing.game2_match_id) if pairing.game2_match_id else None

        if not m1 or not m2 or m1.state != MatchState.COMPLETED or m2.state != MatchState.COMPLETED:
            all_pairings_decided = False
            continue

        sd_outcomes = []
        sd_matches = []
        for sd_id_str in (pairing.sudden_death_match_ids or []):
            sd_match = await db.get(Match, uuid.UUID(sd_id_str))
            if sd_match:
                sd_matches.append(sd_match)
                if sd_match.state == MatchState.COMPLETED and sd_match.result_outcome:
                    sd_val = sd_match.result_outcome.value if hasattr(sd_match.result_outcome, "value") else str(sd_match.result_outcome)
                    sd_outcomes.append(sd_val)

        if sd_matches and sd_matches[-1].state != MatchState.COMPLETED:
            all_pairings_decided = False
            continue

        m1_out = m1.result_outcome.value if hasattr(m1.result_outcome, "value") else str(m1.result_outcome)
        m2_out = m2.result_outcome.value if hasattr(m2.result_outcome, "value") else str(m2.result_outcome)

        p1_pts, p2_pts, winner_code, needs_sd = evaluate_pairing_two_games(m1_out, m2_out, sd_outcomes)
        pairing.p1_points = p1_pts
        pairing.p2_points = p2_pts

        if needs_sd:
            all_pairings_decided = False
            sd_count = len(sd_matches) + 1
            p1 = p_dict[pairing.participant1_id]
            p2 = p_dict[pairing.participant2_id]
            w_sub = p2.submission_version_id if sd_count % 2 == 1 else p1.submission_version_id
            b_sub = p1.submission_version_id if sd_count % 2 == 1 else p2.submission_version_id

            cfg_sd = configuration or default_official_match_configuration()
            sd_match, _ = await create_match(
                db,
                white_submission_version_id=w_sub,
                black_submission_version_id=b_sub,
                configuration=cfg_sd,
                purpose=MatchPurpose.OFFICIAL,
            )
            sd_ids = list(pairing.sudden_death_match_ids or [])
            sd_ids.append(str(sd_match.id))
            pairing.sudden_death_match_ids = sd_ids
            pairing.status = "IN_PROGRESS"
            await db.flush()
        elif winner_code:
            pairing.winner_id = pairing.participant1_id if winner_code == "P1" else pairing.participant2_id
            pairing.status = "COMPLETED"
            winners_in_order.append(p_dict[pairing.winner_id])
            await db.flush()
        else:
            all_pairings_decided = False

    if not all_pairings_decided:
        await db.commit()
        return active_round

    # Stage completed!
    active_round.status = "COMPLETED"
    active_round.completed_at = datetime.now(timezone.utc)
    await db.flush()

    if "Quarterfinals" in active_round.round_name:
        # Quarterfinals finished -> Schedule Semifinals
        # SF1: Category A #1 vs Winner(QF2)
        # SF2: Category B #1 vs Winner(QF1)
        leaderboard = await get_tournament_leaderboard(db, tournament_id)
        cat_a_standings = leaderboard.categories.get("A", [])
        cat_b_standings = leaderboard.categories.get("B", [])
        qual_a, qual_b = select_final_clash_qualifiers(cat_a_standings, cat_b_standings, p_dict)

        a1 = qual_a[0]
        b1 = qual_b[0]
        qf1_winner = winners_in_order[0]
        qf2_winner = winners_in_order[1]

        next_round = TournamentRoundRecord(
            id=uuid.uuid4(),
            tournament_id=tournament_id,
            stage="PLAYOFF",
            round_number=2,
            round_name="Final Clash - Semifinals",
            status="IN_PROGRESS",
        )
        db.add(next_round)
        await db.flush()

        sf_matchups = [
            (a1, qf2_winner, 1),
            (b1, qf1_winner, 2),
        ]
        for p1, p2, p_idx in sf_matchups:
            cfg1 = configuration or default_official_match_configuration()
            m1, _ = await create_match(
                db,
                white_submission_version_id=p1.submission_version_id,
                black_submission_version_id=p2.submission_version_id,
                configuration=cfg1,
                purpose=MatchPurpose.OFFICIAL,
            )
            cfg2 = configuration or default_official_match_configuration()
            m2, _ = await create_match(
                db,
                white_submission_version_id=p2.submission_version_id,
                black_submission_version_id=p1.submission_version_id,
                configuration=cfg2,
                purpose=MatchPurpose.OFFICIAL,
            )
            p_record = TournamentPairingRecord(
                id=uuid.uuid4(),
                round_id=next_round.id,
                pairing_index=p_idx,
                stage_name="SEMIFINAL",
                participant1_id=p1.participant_id,
                participant2_id=p2.participant_id,
                is_bye=False,
                game1_match_id=m1.id,
                game2_match_id=m2.id,
                status="QUEUED",
            )
            db.add(p_record)

        await db.commit()
        await db.refresh(next_round)
        return next_round

    elif "Semifinals" in active_round.round_name:
        # Semifinals finished -> Schedule Finals
        # Final: Winner(SF1) vs Winner(SF2)
        next_round = TournamentRoundRecord(
            id=uuid.uuid4(),
            tournament_id=tournament_id,
            stage="PLAYOFF",
            round_number=3,
            round_name="Final Clash - Finals",
            status="IN_PROGRESS",
        )
        db.add(next_round)
        await db.flush()

        p1 = winners_in_order[0]
        p2 = winners_in_order[1]
        cfg1 = configuration or default_official_match_configuration()
        m1, _ = await create_match(
            db,
            white_submission_version_id=p1.submission_version_id,
            black_submission_version_id=p2.submission_version_id,
            configuration=cfg1,
            purpose=MatchPurpose.OFFICIAL,
        )
        cfg2 = configuration or default_official_match_configuration()
        m2, _ = await create_match(
            db,
            white_submission_version_id=p2.submission_version_id,
            black_submission_version_id=p1.submission_version_id,
            configuration=cfg2,
            purpose=MatchPurpose.OFFICIAL,
        )
        p_record = TournamentPairingRecord(
            id=uuid.uuid4(),
            round_id=next_round.id,
            pairing_index=1,
            stage_name="FINAL",
            participant1_id=p1.participant_id,
            participant2_id=p2.participant_id,
            is_bye=False,
            game1_match_id=m1.id,
            game2_match_id=m2.id,
            status="QUEUED",
        )
        db.add(p_record)

        await db.commit()
        await db.refresh(next_round)
        return next_round

    elif "Finals" in active_round.round_name:
        # Final finished -> Champion crowned!
        await db.commit()
        return active_round

    await db.commit()
    return active_round


# ============================================================================
# OFFICIAL SIX-PLAYER FINALS (TOP 6 QUALIFIERS, BYES, GRAND FINAL & 3RD PLACE)
# ============================================================================

async def initialize_six_player_finals(
    db: AsyncSession,
    tournament_id: str = "official-v1",
    configuration: Optional[MatchConfiguration] = None,
    allow_cutoff_fallback: bool = False,
    games_per_matchup: int = 2,
) -> TournamentRoundRecord:
    """Initialize the official 6-player Finals bracket.
    
    Structure:
    - Qualifiers: Top 6 by qualifying score and tie-break order.
    - Quarterfinals: Seed 3 vs 6 and Seed 4 vs 5.
    - Semifinals: Seed 1 faces Winner(4 vs 5); Seed 2 faces Winner(3 vs 6).
    - Seeds 1 and 2 receive Quarterfinal byes.
    - Grand final determines 1st and 2nd place.
    - Third-place playoff between semifinal losers determines 3rd place.
    - Deterministic fallback for unresolved ties affecting 6th place when allow_cutoff_fallback=True.
    - Idempotent: If round 1 of finals is already created, returns existing record.
    """
    # 1. Idempotency check: see if round 1 already exists
    existing = await db.scalar(
        select(TournamentRoundRecord).where(
            TournamentRoundRecord.tournament_id == tournament_id,
            TournamentRoundRecord.stage == "PLAYOFF",
            TournamentRoundRecord.round_number == 1,
        )
    )
    if existing is not None:
        return existing

    # 2. Retrieve leaderboard and participants
    participants = await get_frozen_official_participants(db, tournament_id)
    p_dict = {p.participant_id: p for p in participants}

    leaderboard = await get_tournament_leaderboard(db, tournament_id)
    qualifiers = select_top_six_qualifiers(
        leaderboard.standings,
        p_dict,
        allow_fallback=allow_cutoff_fallback,
    )

    bracket = generate_six_player_finals_bracket(qualifiers)

    try:
        round_record = TournamentRoundRecord(
            id=uuid.uuid4(),
            tournament_id=tournament_id,
            stage="PLAYOFF",
            round_number=1,
            round_name="Finals - Quarterfinals",
            status="IN_PROGRESS",
        )
        db.add(round_record)
        await db.flush()

        for pairing in bracket.quarterfinals:
            p1 = pairing.participant1
            p2 = pairing.participant2
            assert p1 is not None and p2 is not None

            cfg1 = configuration or default_official_match_configuration()
            m1, _ = await create_match(
                db,
                white_submission_version_id=p1.submission_version_id,
                black_submission_version_id=p2.submission_version_id,
                configuration=cfg1,
                purpose=MatchPurpose.OFFICIAL,
            )

            m2_id = None
            if games_per_matchup == 2:
                cfg2 = configuration or default_official_match_configuration()
                m2, _ = await create_match(
                    db,
                    white_submission_version_id=p2.submission_version_id,
                    black_submission_version_id=p1.submission_version_id,
                    configuration=cfg2,
                    purpose=MatchPurpose.OFFICIAL,
                )
                m2_id = m2.id

            p_record = TournamentPairingRecord(
                id=uuid.uuid4(),
                round_id=round_record.id,
                pairing_index=pairing.pairing_index,
                stage_name="QUARTERFINAL",
                participant1_id=p1.participant_id,
                participant2_id=p2.participant_id,
                is_bye=False,
                game1_match_id=m1.id,
                game2_match_id=m2_id,
                status="QUEUED",
                p1_points=0.0,
                p2_points=0.0,
            )
            db.add(p_record)

        await db.commit()
        await db.refresh(round_record)
        return round_record
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(
            select(TournamentRoundRecord).where(
                TournamentRoundRecord.tournament_id == tournament_id,
                TournamentRoundRecord.stage == "PLAYOFF",
                TournamentRoundRecord.round_number == 1,
            )
        )
        if existing is not None:
            return existing
        raise


async def advance_six_player_finals(
    db: AsyncSession,
    tournament_id: str = "official-v1",
    configuration: Optional[MatchConfiguration] = None,
    games_per_matchup: int = 2,
) -> Optional[TournamentRoundRecord]:
    """Inspect active 6-player Finals round, evaluate outcomes, advance winners,
    schedule Semifinals, Grand Final, and Third-Place Playoff.
    
    Prevents duplicate advancement (idempotent).
    """
    # 1. Find active playoff round
    active_round = await db.scalar(
        select(TournamentRoundRecord)
        .where(
            TournamentRoundRecord.tournament_id == tournament_id,
            TournamentRoundRecord.stage == "PLAYOFF",
            TournamentRoundRecord.status == "IN_PROGRESS",
        )
        .order_by(TournamentRoundRecord.round_number.desc())
    )
    if active_round is None:
        # Check if already completed
        last_round = await db.scalar(
            select(TournamentRoundRecord)
            .where(
                TournamentRoundRecord.tournament_id == tournament_id,
                TournamentRoundRecord.stage == "PLAYOFF",
            )
            .order_by(TournamentRoundRecord.round_number.desc())
        )
        return last_round

    # 2. Inspect pairings in the active round
    pairings = (
        await db.execute(
            select(TournamentPairingRecord)
            .where(TournamentPairingRecord.round_id == active_round.id)
            .order_by(TournamentPairingRecord.pairing_index.asc())
        )
    ).scalars().all()

    participants = await get_frozen_official_participants(db, tournament_id)
    p_dict = {p.participant_id: p for p in participants}

    all_pairings_decided = True
    winners_in_order: list[TournamentParticipant] = []
    losers_in_order: list[TournamentParticipant] = []

    for pairing in pairings:
        if pairing.status == "COMPLETED" and pairing.winner_id:
            winners_in_order.append(p_dict[pairing.winner_id])
            if getattr(pairing, "loser_id", None):
                losers_in_order.append(p_dict[pairing.loser_id])
            elif pairing.participant1_id and pairing.participant2_id:
                other_id = pairing.participant2_id if pairing.winner_id == pairing.participant1_id else pairing.participant1_id
                losers_in_order.append(p_dict[other_id])
            continue

        # Load game 1 and optional game 2 matches
        m1 = await db.get(Match, pairing.game1_match_id) if pairing.game1_match_id else None
        m2 = await db.get(Match, pairing.game2_match_id) if pairing.game2_match_id else None

        if not m1 or m1.state not in (MatchState.COMPLETED, MatchState.ABORTED):
            all_pairings_decided = False
            continue

        if pairing.game2_match_id and (not m2 or m2.state not in (MatchState.COMPLETED, MatchState.ABORTED)):
            all_pairings_decided = False
            continue

        # Adjudicate m1
        m1_dict = {
            "match_id": m1.id,
            "state": m1.state.value if hasattr(m1.state, "value") else str(m1.state),
            "result_outcome": m1.result_outcome.value if hasattr(m1.result_outcome, "value") else m1.result_outcome,
            "result_reason": m1.result_reason.value if hasattr(m1.result_reason, "value") else m1.result_reason,
            "failure_reason": m1.failure_reason.value if hasattr(m1.failure_reason, "value") else m1.failure_reason,
            "side_to_move": m1.side_to_move.value if hasattr(m1.side_to_move, "value") else str(m1.side_to_move),
            "white_participant_id": pairing.participant1_id,
            "black_participant_id": pairing.participant2_id,
        }
        adj1 = adjudicate_match_record(m1_dict)
        if adj1.is_infrastructure_failure:
            # Infrastructure failure: cannot advance, match must be replayed or resolved
            all_pairings_decided = False
            continue
        m1_out = adj1.result_outcome

        m2_out = None
        if m2:
            m2_dict = {
                "match_id": m2.id,
                "state": m2.state.value if hasattr(m2.state, "value") else str(m2.state),
                "result_outcome": m2.result_outcome.value if hasattr(m2.result_outcome, "value") else m2.result_outcome,
                "result_reason": m2.result_reason.value if hasattr(m2.result_reason, "value") else m2.result_reason,
                "failure_reason": m2.failure_reason.value if hasattr(m2.failure_reason, "value") else m2.failure_reason,
                "side_to_move": m2.side_to_move.value if hasattr(m2.side_to_move, "value") else str(m2.side_to_move),
                "white_participant_id": pairing.participant2_id,
                "black_participant_id": pairing.participant1_id,
            }
            adj2 = adjudicate_match_record(m2_dict)
            if adj2.is_infrastructure_failure:
                all_pairings_decided = False
                continue
            m2_out = adj2.result_outcome

        # Collect sudden-death match outcomes if any
        sd_outcomes = []
        sd_matches = []
        for sd_id_str in (pairing.sudden_death_match_ids or []):
            sd_match = await db.get(Match, uuid.UUID(sd_id_str))
            if sd_match:
                sd_matches.append(sd_match)
                if sd_match.state in (MatchState.COMPLETED, MatchState.ABORTED):
                    sd_dict = {
                        "match_id": sd_match.id,
                        "state": sd_match.state.value if hasattr(sd_match.state, "value") else str(sd_match.state),
                        "result_outcome": sd_match.result_outcome.value if hasattr(sd_match.result_outcome, "value") else sd_match.result_outcome,
                        "result_reason": sd_match.result_reason.value if hasattr(sd_match.result_reason, "value") else sd_match.result_reason,
                        "failure_reason": sd_match.failure_reason.value if hasattr(sd_match.failure_reason, "value") else sd_match.failure_reason,
                        "side_to_move": sd_match.side_to_move.value if hasattr(sd_match.side_to_move, "value") else str(sd_match.side_to_move),
                    }
                    sd_adj = adjudicate_match_record(sd_dict)
                    if sd_adj.result_outcome:
                        sd_outcomes.append(sd_adj.result_outcome)

        if sd_matches and sd_matches[-1].state not in (MatchState.COMPLETED, MatchState.ABORTED):
            all_pairings_decided = False
            continue

        p1_pts, p2_pts, winner_code, needs_sd = evaluate_pairing_outcome(m1_out, m2_out, sd_outcomes)
        pairing.p1_points = p1_pts
        pairing.p2_points = p2_pts

        if needs_sd:
            all_pairings_decided = False
            sd_count = len(sd_matches) + 1
            p1 = p_dict[pairing.participant1_id]
            p2 = p_dict[pairing.participant2_id]
            w_sub = p2.submission_version_id if sd_count % 2 == 1 else p1.submission_version_id
            b_sub = p1.submission_version_id if sd_count % 2 == 1 else p2.submission_version_id

            cfg_sd = configuration or default_official_match_configuration()
            sd_match, _ = await create_match(
                db,
                white_submission_version_id=w_sub,
                black_submission_version_id=b_sub,
                configuration=cfg_sd,
                purpose=MatchPurpose.OFFICIAL,
            )
            sd_ids = list(pairing.sudden_death_match_ids or [])
            sd_ids.append(str(sd_match.id))
            pairing.sudden_death_match_ids = sd_ids
            pairing.status = "IN_PROGRESS"
            await db.flush()
        elif winner_code:
            w_id = pairing.participant1_id if winner_code == "P1" else pairing.participant2_id
            l_id = pairing.participant2_id if winner_code == "P1" else pairing.participant1_id
            pairing.winner_id = w_id
            pairing.status = "COMPLETED"
            winners_in_order.append(p_dict[w_id])
            losers_in_order.append(p_dict[l_id])
            await db.flush()
        else:
            all_pairings_decided = False

    if not all_pairings_decided:
        await db.commit()
        return active_round

    # Stage completed!
    active_round.status = "COMPLETED"
    active_round.completed_at = datetime.now(timezone.utc)
    await db.flush()

    if active_round.round_number == 1 or "Quarterfinals" in active_round.round_name:
        # Quarterfinals finished -> Schedule Semifinals
        # QF1 (pairing_index 1) was Seed 4 vs 5 -> Winner is qf1_winner
        # QF2 (pairing_index 2) was Seed 3 vs 6 -> Winner is qf2_winner
        # Semifinal 1: Seed 1 faces Winner(QF1 - 4 vs 5)
        # Semifinal 2: Seed 2 faces Winner(QF2 - 3 vs 6)
        
        # Check if Round 2 already scheduled
        existing_sf = await db.scalar(
            select(TournamentRoundRecord).where(
                TournamentRoundRecord.tournament_id == tournament_id,
                TournamentRoundRecord.stage == "PLAYOFF",
                TournamentRoundRecord.round_number == 2,
            )
        )
        if existing_sf is not None:
            await db.commit()
            return existing_sf

        leaderboard = await get_tournament_leaderboard(db, tournament_id)
        qualifiers = select_top_six_qualifiers(leaderboard.standings, p_dict, allow_fallback=True)
        by_seed = {p.seed: p for p in qualifiers}
        s1 = by_seed[1]
        s2 = by_seed[2]

        qf1_p = next(p for p in pairings if p.pairing_index == 1)
        qf2_p = next(p for p in pairings if p.pairing_index == 2)
        qf1_winner = p_dict[qf1_p.winner_id]
        qf2_winner = p_dict[qf2_p.winner_id]

        next_round = TournamentRoundRecord(
            id=uuid.uuid4(),
            tournament_id=tournament_id,
            stage="PLAYOFF",
            round_number=2,
            round_name="Finals - Semifinals",
            status="IN_PROGRESS",
        )
        db.add(next_round)
        await db.flush()

        sf_matchups = [
            (s1, qf1_winner, 1),
            (s2, qf2_winner, 2),
        ]
        for p1, p2, p_idx in sf_matchups:
            cfg1 = configuration or default_official_match_configuration()
            m1, _ = await create_match(
                db,
                white_submission_version_id=p1.submission_version_id,
                black_submission_version_id=p2.submission_version_id,
                configuration=cfg1,
                purpose=MatchPurpose.OFFICIAL,
            )
            m2_id = None
            if games_per_matchup == 2:
                cfg2 = configuration or default_official_match_configuration()
                m2, _ = await create_match(
                    db,
                    white_submission_version_id=p2.submission_version_id,
                    black_submission_version_id=p1.submission_version_id,
                    configuration=cfg2,
                    purpose=MatchPurpose.OFFICIAL,
                )
                m2_id = m2.id

            p_record = TournamentPairingRecord(
                id=uuid.uuid4(),
                round_id=next_round.id,
                pairing_index=p_idx,
                stage_name="SEMIFINAL",
                participant1_id=p1.participant_id,
                participant2_id=p2.participant_id,
                is_bye=False,
                game1_match_id=m1.id,
                game2_match_id=m2_id,
                status="QUEUED",
                p1_points=0.0,
                p2_points=0.0,
            )
            db.add(p_record)

        await db.commit()
        await db.refresh(next_round)
        return next_round

    elif active_round.round_number == 2 or "Semifinals" in active_round.round_name:
        # Semifinals finished -> Schedule Grand Final and Third-Place Playoff
        # Grand Final: Winner(SF1) vs Winner(SF2) -> determines 1st & 2nd place
        # Third-Place Playoff: Loser(SF1) vs Loser(SF2) -> determines 3rd place
        existing_f = await db.scalar(
            select(TournamentRoundRecord).where(
                TournamentRoundRecord.tournament_id == tournament_id,
                TournamentRoundRecord.stage == "PLAYOFF",
                TournamentRoundRecord.round_number == 3,
            )
        )
        if existing_f is not None:
            await db.commit()
            return existing_f

        sf1_p = next(p for p in pairings if p.pairing_index == 1)
        sf2_p = next(p for p in pairings if p.pairing_index == 2)

        sf1_w = p_dict[sf1_p.winner_id]
        sf1_l_id = getattr(sf1_p, "loser_id", None) or (sf1_p.participant2_id if sf1_p.winner_id == sf1_p.participant1_id else sf1_p.participant1_id)
        sf1_l = p_dict[sf1_l_id]

        sf2_w = p_dict[sf2_p.winner_id]
        sf2_l_id = getattr(sf2_p, "loser_id", None) or (sf2_p.participant2_id if sf2_p.winner_id == sf2_p.participant1_id else sf2_p.participant1_id)
        sf2_l = p_dict[sf2_l_id]

        next_round = TournamentRoundRecord(
            id=uuid.uuid4(),
            tournament_id=tournament_id,
            stage="PLAYOFF",
            round_number=3,
            round_name="Finals - Championship & 3rd Place",
            status="IN_PROGRESS",
        )
        db.add(next_round)
        await db.flush()

        finals_matchups = [
            (sf1_w, sf2_w, 1, "FINAL"),
            (sf1_l, sf2_l, 2, "THIRD_PLACE_PLAYOFF"),
        ]
        for p1, p2, p_idx, stage_name in finals_matchups:
            cfg1 = configuration or default_official_match_configuration()
            m1, _ = await create_match(
                db,
                white_submission_version_id=p1.submission_version_id,
                black_submission_version_id=p2.submission_version_id,
                configuration=cfg1,
                purpose=MatchPurpose.OFFICIAL,
            )
            m2_id = None
            if games_per_matchup == 2:
                cfg2 = configuration or default_official_match_configuration()
                m2, _ = await create_match(
                    db,
                    white_submission_version_id=p2.submission_version_id,
                    black_submission_version_id=p1.submission_version_id,
                    configuration=cfg2,
                    purpose=MatchPurpose.OFFICIAL,
                )
                m2_id = m2.id

            p_record = TournamentPairingRecord(
                id=uuid.uuid4(),
                round_id=next_round.id,
                pairing_index=p_idx,
                stage_name=stage_name,
                participant1_id=p1.participant_id,
                participant2_id=p2.participant_id,
                is_bye=False,
                game1_match_id=m1.id,
                game2_match_id=m2_id,
                status="QUEUED",
                p1_points=0.0,
                p2_points=0.0,
            )
            db.add(p_record)

        await db.commit()
        await db.refresh(next_round)
        return next_round

    elif active_round.round_number == 3 or "Championship" in active_round.round_name:
        # Championship & 3rd Place finished -> Tournament concluded!
        await db.commit()
        return active_round

    await db.commit()
    return active_round


async def get_six_player_finals_bracket(
    db: AsyncSession,
    tournament_id: str = "official-v1",
) -> PlayoffBracketSchema:
    """Build and hydrate the 6-player Finals bracket schema from persisted database records."""
    participants = await get_frozen_official_participants(db, tournament_id)
    p_dict = {p.participant_id: p for p in participants}

    rounds = (
        await db.execute(
            select(TournamentRoundRecord)
            .where(
                TournamentRoundRecord.tournament_id == tournament_id,
                TournamentRoundRecord.stage == "PLAYOFF",
            )
            .order_by(TournamentRoundRecord.round_number.asc())
        )
    ).scalars().all()

    if not rounds:
        # Not yet initialized
        return PlayoffBracketSchema(
            tournament_id=tournament_id,
            status="PENDING",
            quarterfinals=[],
            semifinals=[],
            finals=[],
            total_participants=0,
        )

    qfs: list[PlayoffPairingSchema] = []
    sfs: list[PlayoffPairingSchema] = []
    finals: list[PlayoffPairingSchema] = []
    third_place: Optional[PlayoffPairingSchema] = None
    champion: Optional[TournamentParticipant] = None
    runner_up: Optional[TournamentParticipant] = None
    third_place_winner: Optional[TournamentParticipant] = None
    fourth_place: Optional[TournamentParticipant] = None

    for r in rounds:
        p_records = (
            await db.execute(
                select(TournamentPairingRecord)
                .where(TournamentPairingRecord.round_id == r.id)
                .order_by(TournamentPairingRecord.pairing_index.asc())
            )
        ).scalars().all()

        for pr in p_records:
            p1 = p_dict.get(pr.participant1_id)
            p2 = p_dict.get(pr.participant2_id)
            state_val = "COMPLETED" if pr.status == "COMPLETED" else ("IN_PROGRESS" if pr.status == "IN_PROGRESS" else "QUEUED")
            
            pairing_schema = PlayoffPairingSchema(
                pairing_id=str(pr.id),
                stage=pr.stage_name,
                pairing_index=pr.pairing_index,
                participant1=p1,
                participant2=p2,
                winner_id=pr.winner_id,
                loser_id=getattr(pr, "loser_id", None) or (pr.participant2_id if pr.winner_id == pr.participant1_id else pr.participant1_id),
                p1_points=pr.p1_points,
                p2_points=pr.p2_points,
                state=state_val,
            )

            if pr.stage_name == "QUARTERFINAL":
                qfs.append(pairing_schema)
            elif pr.stage_name == "SEMIFINAL":
                sfs.append(pairing_schema)
            elif pr.stage_name == "FINAL":
                finals.append(pairing_schema)
                if pr.status == "COMPLETED" and pr.winner_id:
                    champion = p_dict.get(pr.winner_id)
                    l_id = getattr(pr, "loser_id", None) or (pr.participant2_id if pr.winner_id == pr.participant1_id else pr.participant1_id)
                    runner_up = p_dict.get(l_id)
            elif pr.stage_name == "THIRD_PLACE_PLAYOFF":
                third_place = pairing_schema
                if pr.status == "COMPLETED" and pr.winner_id:
                    third_place_winner = p_dict.get(pr.winner_id)
                    l_id = getattr(pr, "loser_id", None) or (pr.participant2_id if pr.winner_id == pr.participant1_id else pr.participant1_id)
                    fourth_place = p_dict.get(l_id)

    # Determine byes (Seeds 1 and 2)
    byes: list[TournamentParticipant] = []
    leaderboard = await get_tournament_leaderboard(db, tournament_id)
    try:
        qualifiers = select_top_six_qualifiers(leaderboard.standings, p_dict, allow_fallback=True)
        by_seed = {p.seed: p for p in qualifiers}
        if 1 in by_seed and 2 in by_seed:
            byes = [by_seed[1], by_seed[2]]
    except Exception:
        byes = []

    is_completed = (champion is not None and third_place_winner is not None)

    return PlayoffBracketSchema(
        tournament_id=tournament_id,
        status="COMPLETED" if is_completed else "IN_PROGRESS",
        quarterfinals=qfs,
        semifinals=sfs,
        finals=finals,
        third_place=third_place,
        byes=byes,
        champion=champion,
        runner_up=runner_up,
        third_place_winner=third_place_winner,
        fourth_place=fourth_place,
        total_participants=6,
    )


async def resolve_cutoff_tie_playoff(
    db: AsyncSession,
    tournament_id: str = "official-v1",
    method: str = "FALLBACK",
    configuration: Optional[MatchConfiguration] = None,
) -> list[TournamentParticipant]:
    """Resolve an unresolved tie affecting 6th place qualification.
    
    If method == "FALLBACK": uses predefined deterministic fallback (seed/name).
    """
    participants = await get_frozen_official_participants(db, tournament_id)
    p_dict = {p.participant_id: p for p in participants}
    leaderboard = await get_tournament_leaderboard(db, tournament_id)
    return select_top_six_qualifiers(leaderboard.standings, p_dict, allow_fallback=True)


