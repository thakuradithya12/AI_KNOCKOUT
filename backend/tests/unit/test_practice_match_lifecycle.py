"""Unit tests for Practice/Development match lifecycle, deterministic failure attribution, and standings isolation."""

import uuid
from datetime import datetime, timezone
import pytest

from app.matches.controller import MatchConfiguration
from app.models.match import (
    Match,
    MatchOutcome,
    MatchPurpose,
    MatchSideColor,
    MatchSideKind,
    MatchState,
)
from app.tournaments.engine import compute_leaderboard_standings
from app.tournaments.schemas import TournamentParticipant


def test_practice_matches_never_alter_official_standings():
    """Verify that matches with purpose=DEVELOPMENT are completely ignored by the official standings engine."""
    p1 = TournamentParticipant(
        participant_id="part-1",
        name="Participant One",
        team_name="Team Alpha",
        submission_version_id=uuid.uuid4(),
        version=1,
        artifact_sha256="1" * 64,
        seed=1,
    )
    p2 = TournamentParticipant(
        participant_id="part-2",
        name="Participant Two",
        team_name="Team Beta",
        submission_version_id=uuid.uuid4(),
        version=1,
        artifact_sha256="2" * 64,
        seed=2,
    )

    # Official match: P1 beat P2
    official_match = {
        "match_id": str(uuid.uuid4()),
        "purpose": "OFFICIAL",
        "state": "COMPLETED",
        "white_participant_id": "part-1",
        "black_participant_id": "part-2",
        "result_outcome": "WHITE_WIN",
        "result_reason": "CHECKMATE",
    }

    # Practice / Development matches: P2 won against House AI, P1 lost against House AI
    practice_matches = [
        {
            "match_id": str(uuid.uuid4()),
            "purpose": "DEVELOPMENT",
            "state": "COMPLETED",
            "white_participant_id": "part-2",
            "black_participant_id": "house-ai-baseline",
            "result_outcome": "WHITE_WIN",
            "result_reason": "CHECKMATE",
        },
        {
            "match_id": str(uuid.uuid4()),
            "purpose": "DEVELOPMENT",
            "state": "COMPLETED",
            "white_participant_id": "part-1",
            "black_participant_id": "house-ai-baseline",
            "result_outcome": "BLACK_WIN",
            "result_reason": "TIMEOUT",
        },
    ]

    # Only official matches are supplied to standings engine
    standings = compute_leaderboard_standings([p1, p2], [official_match])
    s_map = {s.participant_id: s for s in standings}

    assert s_map["part-1"].rank == 1
    assert s_map["part-1"].points == 1.0
    assert s_map["part-1"].wins == 1
    assert s_map["part-1"].losses == 0

    assert s_map["part-2"].rank == 2
    assert s_map["part-2"].points == 0.0
    assert s_map["part-2"].wins == 0
    assert s_map["part-2"].losses == 1

    # If practice matches were accidentally passed to official tournament engine,
    # House AI is not even in participants list so stats for participants would not be corrupted
    standings_with_practice = compute_leaderboard_standings([p1, p2], [official_match] + practice_matches)
    s_map2 = {s.participant_id: s for s in standings_with_practice}

    # Points remain strictly identical!
    assert s_map2["part-1"].points == 1.0
    assert s_map2["part-2"].points == 0.0


def test_failure_attribution_outcomes():
    """Verify standard failure attribution for timeout, illegal move, and resignations."""
    # Timeout attribution: White ran out of time -> Black wins by TIMEOUT
    m_timeout = {
        "match_id": str(uuid.uuid4()),
        "state": "COMPLETED",
        "white_participant_id": "part-a",
        "black_participant_id": "part-b",
        "result_outcome": "BLACK_WIN",
        "result_reason": "TIMEOUT",
    }
    p_a = TournamentParticipant(
        participant_id="part-a", name="A", submission_version_id=uuid.uuid4(), version=1, artifact_sha256="a"*64, seed=1
    )
    p_b = TournamentParticipant(
        participant_id="part-b", name="B", submission_version_id=uuid.uuid4(), version=1, artifact_sha256="b"*64, seed=2
    )
    standings = compute_leaderboard_standings([p_a, p_b], [m_timeout])
    assert standings[0].participant_id == "part-b"
    assert standings[0].points == 1.0
    assert standings[1].participant_id == "part-a"
    assert standings[1].points == 0.0

    # Illegal move attribution: Black played illegal move -> White wins by ILLEGAL_MOVE
    m_illegal = {
        "match_id": str(uuid.uuid4()),
        "state": "COMPLETED",
        "white_participant_id": "part-a",
        "black_participant_id": "part-b",
        "result_outcome": "WHITE_WIN",
        "result_reason": "ILLEGAL_MOVE",
    }
    standings_illegal = compute_leaderboard_standings([p_a, p_b], [m_illegal])
    assert standings_illegal[0].participant_id == "part-a"
    assert standings_illegal[0].points == 1.0
    assert standings_illegal[1].participant_id == "part-b"
    assert standings_illegal[1].points == 0.0
