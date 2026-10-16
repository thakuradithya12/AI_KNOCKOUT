"""Unit tests for tournament bracket generation, scoring, and tie-breaking engine."""

import uuid

from app.tournaments.engine import (
    compute_leaderboard_standings,
    determine_match_winner,
    evaluate_pairing_two_games,
    generate_playoff_bracket,
    generate_single_elimination_bracket,
    generate_swiss_pairings,
    select_top_eight_qualifiers,
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


def test_swiss_pairings_even_participant_counts():
    parts = [_make_participant(f"P{i}", i) for i in range(1, 7)]
    pairings = generate_swiss_pairings(parts, [], 1)
    assert len(pairings) == 3
    # No byes
    assert not any(p.is_bye for p in pairings)
    # Exactly all 6 participants paired once
    paired_ids = set()
    for p in pairings:
        assert p.participant2 is not None
        paired_ids.add(p.participant1.participant_id)
        paired_ids.add(p.participant2.participant_id)
    assert len(paired_ids) == 6


def test_swiss_pairings_odd_participant_counts_bye_assignment():
    parts = [_make_participant(f"P{i}", i) for i in range(1, 6)]
    pairings = generate_swiss_pairings(parts, [], 1)
    assert len(pairings) == 3  # 2 regular + 1 bye
    bye_pairings = [p for p in pairings if p.is_bye]
    assert len(bye_pairings) == 1
    # Lowest seeded participant in round 1 (P5) gets the bye
    assert bye_pairings[0].participant1.participant_id == "part_p5"
    assert bye_pairings[0].p1_points == 2.0
    assert bye_pairings[0].state == "COMPLETED"


def test_bye_no_repeat_and_lowest_ranked():
    parts = [_make_participant(f"P{i}", i) for i in range(1, 6)]
    # In round 1, P5 got a bye
    byes = {"part_p5": 1}
    # Matches from round 1: P1 beat P2, P3 beat P4
    m1 = {
        "match_id": str(uuid.uuid4()),
        "white_participant_id": "part_p1",
        "black_participant_id": "part_p2",
        "state": "COMPLETED",
        "result_outcome": "WHITE_WIN",
    }
    m2 = {
        "match_id": str(uuid.uuid4()),
        "white_participant_id": "part_p3",
        "black_participant_id": "part_p4",
        "state": "COMPLETED",
        "result_outcome": "WHITE_WIN",
    }
    pairings_r2 = generate_swiss_pairings(parts, [m1, m2], 2, existing_byes=byes)
    bye_p = next(p for p in pairings_r2 if p.is_bye)
    # P5 cannot get a repeat bye! Candidate must be another participant
    assert bye_p.participant1.participant_id != "part_p5"
    # Among remaining (P2 and P4 with 0 points), lowest seed P4 gets the bye
    assert bye_p.participant1.participant_id in {"part_p4", "part_p2"}


def test_swiss_avoidance_of_repeat_opponents():
    parts = [_make_participant(f"P{i}", i) for i in range(1, 5)]
    # Round 1: P1 played P2, P3 played P4
    m1 = {
        "match_id": str(uuid.uuid4()),
        "white_participant_id": "part_p1",
        "black_participant_id": "part_p2",
        "state": "COMPLETED",
        "result_outcome": "WHITE_WIN",
    }
    m2 = {
        "match_id": str(uuid.uuid4()),
        "white_participant_id": "part_p3",
        "black_participant_id": "part_p4",
        "state": "COMPLETED",
        "result_outcome": "WHITE_WIN",
    }
    pairings_r2 = generate_swiss_pairings(parts, [m1, m2], 2)
    # Round 2 must NOT repeat (P1, P2) or (P3, P4)
    for p in pairings_r2:
        assert p.participant2 is not None
        pair = {p.participant1.participant_id, p.participant2.participant_id}
        assert pair != {"part_p1", "part_p2"}
        assert pair != {"part_p3", "part_p4"}


def test_deterministic_swiss_pairings_from_identical_inputs():
    parts = [_make_participant(f"P{i}", i) for i in range(1, 9)]
    pairings_1 = generate_swiss_pairings(parts, [], 1)
    pairings_2 = generate_swiss_pairings(parts, [], 1)
    for p1, p2 in zip(pairings_1, pairings_2):
        assert p1.participant1.participant_id == p2.participant1.participant_id
        assert (p1.participant2.participant_id if p1.participant2 else None) == (p2.participant2.participant_id if p2.participant2 else None)


def test_aggregation_of_two_game_pairing_results():
    # 1. 2-0 sweep: P1 wins both
    p1, p2, winner, sd = evaluate_pairing_two_games("WHITE_WIN", "BLACK_WIN")
    assert p1 == 2.0 and p2 == 0.0 and winner == "P1" and not sd

    # 2. 1.5 - 0.5: P1 wins Game 1, draws Game 2
    p1, p2, winner, sd = evaluate_pairing_two_games("WHITE_WIN", "DRAW")
    assert p1 == 1.5 and p2 == 0.5 and winner == "P1" and not sd

    # 3. 1.5 - 0.5: P2 draws Game 1, wins Game 2
    p1, p2, winner, sd = evaluate_pairing_two_games("DRAW", "WHITE_WIN")
    assert p1 == 0.5 and p2 == 1.5 and winner == "P2" and not sd

    # 4. 1.0 - 1.0 split: P1 wins Game 1, P2 wins Game 2 -> Sudden death needed!
    p1, p2, winner, sd = evaluate_pairing_two_games("WHITE_WIN", "WHITE_WIN")
    assert p1 == 1.0 and p2 == 1.0 and winner is None and sd

    # 5. 1.0 - 1.0 split: Both games drawn -> Sudden death needed!
    p1, p2, winner, sd = evaluate_pairing_two_games("DRAW", "DRAW")
    assert p1 == 1.0 and p2 == 1.0 and winner is None and sd


def test_head_to_head_and_opponent_strength_tie_breaks():
    # 4 participants: Alpha, Beta, Gamma, Delta
    p_a = _make_participant("Alpha", 1)
    p_b = _make_participant("Beta", 2)
    p_c = _make_participant("Gamma", 3)
    p_d = _make_participant("Delta", 4)
    parts = [p_a, p_b, p_c, p_d]

    # Scenario: Both Alpha and Beta finish with 2.0 points.
    # Alpha beat Beta in their direct game!
    # Alpha also played Gamma (lost). Beta played Delta (won).
    m1 = {"match_id": str(uuid.uuid4()), "white_participant_id": "part_alpha", "black_participant_id": "part_beta", "state": "COMPLETED", "result_outcome": "WHITE_WIN"}
    m2 = {"match_id": str(uuid.uuid4()), "white_participant_id": "part_alpha", "black_participant_id": "part_gamma", "state": "COMPLETED", "result_outcome": "WHITE_WIN"}
    m3 = {"match_id": str(uuid.uuid4()), "white_participant_id": "part_beta", "black_participant_id": "part_gamma", "state": "COMPLETED", "result_outcome": "WHITE_WIN"}
    m4 = {"match_id": str(uuid.uuid4()), "white_participant_id": "part_beta", "black_participant_id": "part_delta", "state": "COMPLETED", "result_outcome": "WHITE_WIN"}

    standings = compute_leaderboard_standings(parts, [m1, m2, m3, m4])
    a_s = next(s for s in standings if s.participant_id == "part_alpha")
    b_s = next(s for s in standings if s.participant_id == "part_beta")

    # Both have 2.0 points
    assert a_s.points == 2.0
    assert b_s.points == 2.0
    # Alpha won head-to-head vs Beta (1.0 vs 0.0) -> Alpha ranks higher!
    assert a_s.head_to_head_points > b_s.head_to_head_points
    assert a_s.rank < b_s.rank


def test_top_eight_cutoff_and_ties_boundary():
    # 10 participants: ranks 1 through 10
    parts = [_make_participant(f"P{i}", i) for i in range(1, 11)]
    p_dict = {p.participant_id: p for p in parts}

    # Matches giving ranks: P1..P8 clear scores, but P8 and P9 tied on points, h2h, and opponents
    # P1..P7 have 3 points each
    # P8 and P9 have 1 point each, P10 has 0
    matches = []
    for i in range(1, 8):
        matches.append({"match_id": str(uuid.uuid4()), "white_participant_id": f"part_p{i}", "black_participant_id": "part_p10", "state": "COMPLETED", "result_outcome": "WHITE_WIN"})
    # P8 beat P10
    matches.append({"match_id": str(uuid.uuid4()), "white_participant_id": "part_p8", "black_participant_id": "part_p10", "state": "COMPLETED", "result_outcome": "WHITE_WIN"})
    # P9 beat P10
    matches.append({"match_id": str(uuid.uuid4()), "white_participant_id": "part_p9", "black_participant_id": "part_p10", "state": "COMPLETED", "result_outcome": "WHITE_WIN"})

    standings = compute_leaderboard_standings(parts, matches)
    # Exactly top 8 selected
    qualifiers = select_top_eight_qualifiers(standings, p_dict)
    assert len(qualifiers) == 8

    # Because P8 and P9 are tied on points, h2h (didn't play each other = 0), and opponent strength (both played P10 who has 0),
    # needs_playoff must be flagged on the cutoff boundary!
    assert standings[7].needs_playoff is True
    assert standings[8].needs_playoff is True


def test_playoff_seeding_and_bracket_structure():
    qualifiers = [_make_participant(f"Seed_{i}", i) for i in range(1, 9)]
    bracket = generate_playoff_bracket(qualifiers)
    assert bracket.status == "IN_PROGRESS"
    assert len(bracket.quarterfinals) == 4

    # Seed 1 vs 8, 4 vs 5, 2 vs 7, 3 vs 6
    qf1 = bracket.quarterfinals[0]
    assert qf1.participant1.seed == 1 and qf1.participant2.seed == 8

    qf2 = bracket.quarterfinals[1]
    assert qf2.participant1.seed == 4 and qf2.participant2.seed == 5

    qf3 = bracket.quarterfinals[2]
    assert qf3.participant1.seed == 2 and qf3.participant2.seed == 7

    qf4 = bracket.quarterfinals[3]
    assert qf4.participant1.seed == 3 and qf4.participant2.seed == 6


def test_playoff_sudden_death_progression():
    # 1.0 - 1.0 split: Game 1 P1 wins, Game 2 P2 wins
    # First evaluation: needs sudden death
    p1, p2, winner, sd = evaluate_pairing_two_games("WHITE_WIN", "WHITE_WIN")
    assert sd is True and winner is None

    # Sudden death Game 3 played: P2 was White, and won!
    p1_f, p2_f, winner_f, sd_f = evaluate_pairing_two_games("WHITE_WIN", "WHITE_WIN", sudden_death_outcomes=["WHITE_WIN"])
    assert sd_f is False
    assert winner_f == "P2"
    assert p2_f == 2.0


def test_prize_position_unresolved_tie_needs_playoff():
    # 4 participants: P1, P2, P3, P4
    p1 = _make_participant("P1", 1)
    p2 = _make_participant("P2", 2)
    p3 = _make_participant("P3", 3)
    p4 = _make_participant("P4", 4)
    parts = [p1, p2, p3, p4]

    # P1 beats P3, P2 beats P4
    # P3 and P4 never score -> P1 and P2 both have 1.0 point, 0 H2H against each other,
    # and opponent strength = 0.0 (both played opponents with 0 points).
    # This is an unresolved tie for 1st place (prize position)!
    m1 = {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p1", "black_participant_id": "part_p3", "state": "COMPLETED", "result_outcome": "WHITE_WIN"}
    m2 = {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p2", "black_participant_id": "part_p4", "state": "COMPLETED", "result_outcome": "WHITE_WIN"}

    standings = compute_leaderboard_standings(parts, [m1, m2])
    assert standings[0].points == 1.0
    assert standings[1].points == 1.0
    assert standings[0].head_to_head_points == standings[1].head_to_head_points
    assert standings[0].opponent_strength == standings[1].opponent_strength

    # Unresolved tie for 1st place MUST have needs_playoff = True!
    assert standings[0].needs_playoff is True
    assert standings[1].needs_playoff is True


def test_two_way_tie_with_direct_encounter():
    p1 = _make_participant("P1", 1)
    p2 = _make_participant("P2", 2)
    p3 = _make_participant("P3", 3)
    p4 = _make_participant("P4", 4)
    parts = [p1, p2, p3, p4]

    # P1 beats P2 in their direct encounter (1.0 vs 0.0)
    # P1 loses to P3 (0.0) -> P1 total points = 1.0
    # P2 beats P4 (1.0) -> P2 total points = 1.0
    # P3 beats P4 (1.0) -> P3 total points = 2.0
    # P4 has 0.0 points
    # Note: P2 played P4 (0 pts) + P1 (1 pt) -> P2 opp strength = 1.0
    # P1 played P2 (1 pt) + P3 (2 pts) -> P1 opp strength = 3.0
    # Even if P2 had higher opponent strength, H2H is Tier 2 and decides before Opponent Strength (Tier 3).
    matches = [
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p1", "black_participant_id": "part_p2", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p3", "black_participant_id": "part_p1", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p2", "black_participant_id": "part_p4", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p3", "black_participant_id": "part_p4", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
    ]

    standings = compute_leaderboard_standings(parts, matches)
    s_p1 = next(s for s in standings if s.participant_id == "part_p1")
    s_p2 = next(s for s in standings if s.participant_id == "part_p2")

    assert s_p1.points == 1.0
    assert s_p2.points == 1.0
    # Direct encounter: P1 won vs P2
    assert s_p1.head_to_head_points == 1.0
    assert s_p2.head_to_head_points == 0.0
    assert s_p1.rank < s_p2.rank


def test_two_way_tie_without_direct_encounter():
    p1 = _make_participant("P1", 1)
    p2 = _make_participant("P2", 2)
    p3 = _make_participant("P3", 3)
    p4 = _make_participant("P4", 4)
    p5 = _make_participant("P5", 5)
    parts = [p1, p2, p3, p4, p5]

    # P1 and P2 NEVER play each other.
    # P1 beats P3 (1.0). P1 loses to P5. P1 total = 1.0.
    # P2 beats P4 (1.0). P2 loses to P5. P2 total = 1.0.
    # P3 beats P4 and P5 -> P3 total = 2.0. Opponent strength of P1: P3(2.0) + P5(2.0) = 4.0.
    # P4 loses all matches -> P4 total = 0.0. Opponent strength of P2: P4(0.0) + P5(2.0) = 2.0.
    # P5 beats P1 and P2 -> P5 total = 2.0.
    matches = [
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p1", "black_participant_id": "part_p3", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p5", "black_participant_id": "part_p1", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p2", "black_participant_id": "part_p4", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p5", "black_participant_id": "part_p2", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p3", "black_participant_id": "part_p4", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_p3", "black_participant_id": "part_p5", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
    ]

    standings = compute_leaderboard_standings(parts, matches)
    s_p1 = next(s for s in standings if s.participant_id == "part_p1")
    s_p2 = next(s for s in standings if s.participant_id == "part_p2")

    assert s_p1.points == 1.0
    assert s_p2.points == 1.0
    # No direct encounter -> both H2H points are 0.0
    assert s_p1.head_to_head_points == 0.0
    assert s_p2.head_to_head_points == 0.0
    # Opponent strength decides: P1 faced stronger opponents (P3=2.0, P5=2.0 -> 4.0) vs P2 (P4=0.0, P5=2.0 -> 2.0)
    assert s_p1.opponent_strength > s_p2.opponent_strength
    assert s_p1.rank < s_p2.rank


def test_three_way_tie_complete_mini_league():
    p_a = _make_participant("A", 1)
    p_b = _make_participant("B", 2)
    p_c = _make_participant("C", 3)
    p_d = _make_participant("D", 4)
    p_e = _make_participant("E", 5)
    parts = [p_a, p_b, p_c, p_d, p_e]

    # Mini-league among A, B, C:
    # A beats B (1.0 vs 0.0)
    # A beats C (1.0 vs 0.0)
    # B beats C (1.0 vs 0.0)
    # Internal points: A=2.0, B=1.0, C=0.0.
    # Non-internal matches to equalize total points to 2.0 each:
    # A loses to D (0.0) -> A total = 2.0.
    # B beats D (1.0) -> B total = 2.0.
    # C beats D (1.0) and beats E (1.0) -> C total = 2.0.
    matches = [
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_a", "black_participant_id": "part_b", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_a", "black_participant_id": "part_c", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_b", "black_participant_id": "part_c", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_d", "black_participant_id": "part_a", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_b", "black_participant_id": "part_d", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_c", "black_participant_id": "part_d", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_c", "black_participant_id": "part_e", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
    ]

    standings = compute_leaderboard_standings(parts, matches)
    s_a = next(s for s in standings if s.participant_id == "part_a")
    s_b = next(s for s in standings if s.participant_id == "part_b")
    s_c = next(s for s in standings if s.participant_id == "part_c")

    assert s_a.points == 2.0
    assert s_b.points == 2.0
    assert s_c.points == 2.0

    # Because all pairs in {A, B, C} played, complete mini-league applies:
    # A has 2.0 H2H, B has 1.0 H2H, C has 0.0 H2H.
    assert s_a.head_to_head_points == 2.0
    assert s_b.head_to_head_points == 1.0
    assert s_c.head_to_head_points == 0.0

    assert s_a.rank < s_b.rank < s_c.rank


def test_three_way_tie_incomplete_mini_league():
    p_a = _make_participant("A", 1)
    p_b = _make_participant("B", 2)
    p_c = _make_participant("C", 3)
    p_d = _make_participant("D", 4)
    p_e = _make_participant("E", 5)
    p_f = _make_participant("F", 6)
    parts = [p_a, p_b, p_c, p_d, p_e, p_f]

    # Tied on 1.0 point: A, B, C.
    # Incomplete mini-league: A played B (A beat B), but C never played A or B!
    # A beat B (1.0 vs 0.0). A lost to D (0.0). A total = 1.0. (Opponents: B, D)
    # B beat E (1.0 vs 0.0). B lost to A (0.0). B total = 1.0. (Opponents: A, E)
    # C beat F (1.0 vs 0.0). C lost to D (0.0). C total = 1.0. (Opponents: F, D)
    # Let D have high score: D beats A, C, and E -> D total = 3.0.
    # Let F have moderate score: F beats E -> F total = 1.0.
    # E has 0.0 points.
    matches = [
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_a", "black_participant_id": "part_b", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_d", "black_participant_id": "part_a", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_b", "black_participant_id": "part_e", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_c", "black_participant_id": "part_f", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_d", "black_participant_id": "part_c", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_d", "black_participant_id": "part_e", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
        {"match_id": str(uuid.uuid4()), "white_participant_id": "part_f", "black_participant_id": "part_e", "state": "COMPLETED", "result_outcome": "WHITE_WIN"},
    ]

    standings = compute_leaderboard_standings(parts, matches)
    s_a = next(s for s in standings if s.participant_id == "part_a")
    s_b = next(s for s in standings if s.participant_id == "part_b")
    s_c = next(s for s in standings if s.participant_id == "part_c")

    assert s_a.points == 1.0
    assert s_b.points == 1.0
    assert s_c.points == 1.0

    # Incomplete mini-league! Head-to-head MUST be disregarded (set to 0.0 for all 3):
    assert s_a.head_to_head_points == 0.0
    assert s_b.head_to_head_points == 0.0
    assert s_c.head_to_head_points == 0.0

    # Opponent strength decides:
    # Opponents of C: F(1.0) + D(3.0) = 4.0
    # Opponents of A: B(1.0) + D(3.0) = 4.0
    # Opponents of B: A(1.0) + E(0.0) = 1.0
    assert s_c.opponent_strength == 4.0
    assert s_a.opponent_strength == 4.0
    assert s_b.opponent_strength == 1.0
    # C and A share higher opponent strength than B -> both rank ahead of B:
    assert s_c.rank < s_b.rank
    assert s_a.rank < s_b.rank


