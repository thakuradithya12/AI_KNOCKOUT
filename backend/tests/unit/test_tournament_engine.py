"""Unit tests for tournament bracket generation, scoring, and tie-breaking engine."""

import uuid

from app.tournaments.engine import (
    compute_leaderboard_standings,
    determine_match_winner,
    generate_single_elimination_bracket,
)
from app.tournaments.schemas import TournamentParticipant


def _make_participant(name: str, seed: int) -> TournamentParticipant:
    return TournamentParticipant(
        participant_id=f"part_{name.lower()}",
        name=name,
        team_name=f"Team {name}",
        submission_version_id=uuid.uuid4(),
        version=1,
        artifact_sha256="a" * 64,
        seed=seed,
    )


def test_bracket_generation_power_of_two():
    participants = [
        _make_participant("Alpha", 1),
        _make_participant("Beta", 2),
        _make_participant("Gamma", 3),
        _make_participant("Delta", 4),
    ]
    bracket = generate_single_elimination_bracket(participants)

    assert bracket.total_participants == 4
    assert bracket.status == "IN_PROGRESS"
    assert len(bracket.rounds) == 2  # Semifinals, Finals
    assert bracket.rounds[0].round_name == "Semifinals"
    assert len(bracket.rounds[0].pairings) == 2

    # Seeding: 1 vs 4, and 2 vs 3
    pairings = bracket.rounds[0].pairings
    p1 = pairings[0]
    p2 = pairings[1]

    pair1_names = {p1.white.name, p1.black.name}
    pair2_names = {p2.white.name, p2.black.name}

    assert pair1_names == {"Alpha", "Delta"}
    assert pair2_names == {"Beta", "Gamma"}


def test_bracket_generation_with_byes():
    # 3 participants in a 4-bracket -> 1 bye
    participants = [
        _make_participant("Alpha", 1),
        _make_participant("Beta", 2),
        _make_participant("Gamma", 3),
    ]
    bracket = generate_single_elimination_bracket(participants)

    assert bracket.total_participants == 3
    pairings = bracket.rounds[0].pairings
    assert len(pairings) == 2

    # High seed Alpha (1) faces seed 4 (missing, bye) -> automatically marked COMPLETED with Alpha as winner
    bye_pairing = next(p for p in pairings if p.result_reason == "BYE")
    assert bye_pairing.state == "COMPLETED"
    assert bye_pairing.winner_participant_id == "part_alpha"


def test_scoring_and_leaderboard_standings():
    p_alpha = _make_participant("Alpha", 1)
    p_beta = _make_participant("Beta", 2)
    p_gamma = _make_participant("Gamma", 3)
    p_delta = _make_participant("Delta", 4)
    participants = [p_alpha, p_beta, p_gamma, p_delta]

    matches = [
        # Alpha beats Beta
        {
            "match_id": str(uuid.uuid4()),
            "white_participant_id": "part_alpha",
            "black_participant_id": "part_beta",
            "state": "COMPLETED",
            "result_outcome": "WHITE_WIN",
            "result_reason": "CHECKMATE",
        },
        # Gamma draws with Delta
        {
            "match_id": str(uuid.uuid4()),
            "white_participant_id": "part_gamma",
            "black_participant_id": "part_delta",
            "state": "COMPLETED",
            "result_outcome": "DRAW",
            "result_reason": "STALEMATE",
        },
        # Alpha draws with Gamma
        {
            "match_id": str(uuid.uuid4()),
            "white_participant_id": "part_alpha",
            "black_participant_id": "part_gamma",
            "state": "COMPLETED",
            "result_outcome": "DRAW",
            "result_reason": "THREEFOLD_REPETITION",
        },
    ]

    standings = compute_leaderboard_standings(participants, matches)

    assert len(standings) == 4
    # Alpha: 1 win, 1 draw = 1.5 points
    assert standings[0].participant_id == "part_alpha"
    assert standings[0].points == 1.5
    assert standings[0].wins == 1
    assert standings[0].draws == 1
    assert standings[0].losses == 0

    # Gamma: 2 draws = 1.0 point
    gamma_standing = next(s for s in standings if s.participant_id == "part_gamma")
    assert gamma_standing.points == 1.0
    assert gamma_standing.draws == 2

    # Beta: 1 loss = 0.0 point
    beta_standing = next(s for s in standings if s.participant_id == "part_beta")
    assert beta_standing.points == 0.0
    assert beta_standing.losses == 1


def test_sonneborn_berger_tie_break():
    # Two players tied on points, but one defeated an opponent with higher total score
    p_a = _make_participant("A", 1)
    p_b = _make_participant("B", 2)
    p_c = _make_participant("C", 3)
    p_d = _make_participant("D", 4)
    participants = [p_a, p_b, p_c, p_d]

    matches = [
        # A defeats C (who later wins a match)
        {
            "match_id": str(uuid.uuid4()),
            "white_participant_id": "part_a",
            "black_participant_id": "part_c",
            "state": "COMPLETED",
            "result_outcome": "WHITE_WIN",
            "result_reason": "CHECKMATE",
        },
        # B defeats D (who never wins a match)
        {
            "match_id": str(uuid.uuid4()),
            "white_participant_id": "part_b",
            "black_participant_id": "part_d",
            "state": "COMPLETED",
            "result_outcome": "WHITE_WIN",
            "result_reason": "CHECKMATE",
        },
        # C defeats D
        {
            "match_id": str(uuid.uuid4()),
            "white_participant_id": "part_c",
            "black_participant_id": "part_d",
            "state": "COMPLETED",
            "result_outcome": "WHITE_WIN",
            "result_reason": "CHECKMATE",
        },
    ]

    standings = compute_leaderboard_standings(participants, matches)

    # Both A and B have 1 point and 1 win.
    # But A defeated C (who has 1 point) -> A's SB score = 1.0
    # B defeated D (who has 0 points) -> B's SB score = 0.0
    # Therefore A must rank ahead of B!
    a_standing = next(s for s in standings if s.participant_id == "part_a")
    b_standing = next(s for s in standings if s.participant_id == "part_b")

    assert a_standing.points == 1.0
    assert b_standing.points == 1.0
    assert a_standing.tie_break_score > b_standing.tie_break_score
    assert a_standing.rank < b_standing.rank


def test_determine_match_winner():
    assert determine_match_winner("WHITE_WIN", "w1", "b1") == "w1"
    assert determine_match_winner("BLACK_WIN", "w1", "b1") == "b1"
    assert determine_match_winner("DRAW", "w1", "b1", draw_tiebreak_preference="WHITE") == "w1"
    assert determine_match_winner("DRAW", "w1", "b1", draw_tiebreak_preference="BLACK") == "b1"
    assert determine_match_winner(None, "w1", "b1") is None


def test_bracket_generation_odd_and_even_counts():
    for count in (1, 2, 3, 4, 5, 6, 7, 8):
        parts = [_make_participant(f"P{i}", i) for i in range(1, count + 1)]
        bracket = generate_single_elimination_bracket(parts)
        assert bracket.total_participants == count
        if count <= 2:
            assert len(bracket.rounds) == 1
        elif count <= 4:
            assert len(bracket.rounds) == 2
        elif count <= 8:
            assert len(bracket.rounds) == 3


def test_repeated_and_duplicate_match_handling_in_scoring():
    p1 = _make_participant("P1", 1)
    p2 = _make_participant("P2", 2)
    m_id = str(uuid.uuid4())
    matches = [
        {
            "match_id": m_id,
            "white_participant_id": "part_p1",
            "black_participant_id": "part_p2",
            "state": "COMPLETED",
            "result_outcome": "WHITE_WIN",
            "result_reason": "CHECKMATE",
        },
        {
            "match_id": m_id,  # duplicate record
            "white_participant_id": "part_p1",
            "black_participant_id": "part_p2",
            "state": "COMPLETED",
            "result_outcome": "WHITE_WIN",
            "result_reason": "CHECKMATE",
        },
    ]
    standings = compute_leaderboard_standings([p1, p2], matches)
    p1_s = next(s for s in standings if s.participant_id == "part_p1")
    assert p1_s.points == 1.0
    assert p1_s.wins == 1
    assert p1_s.matches_played == 1
