"""Unit tests for Category A & B Round-Robin, House AI Promotion, and Final Clash engine."""

import uuid
import pytest
from app.tournaments.engine import (
    compute_category_standings,
    compute_leaderboard_standings,
    generate_category_round_robin_pairings,
    generate_final_clash_bracket,
    partition_categories,
    select_final_clash_qualifiers,
)
from app.tournaments.schemas import ParticipantStanding, TournamentParticipant


def make_dummy_participant(p_id: str, seed: int, name: str, is_house_ai: bool = False) -> TournamentParticipant:
    return TournamentParticipant(
        participant_id=p_id,
        name=name,
        team_name=f"Team {name}",
        submission_version_id=uuid.uuid4(),
        version=1,
        artifact_sha256="a" * 64,
        seed=seed,
        is_house_ai=is_house_ai,
    )


def test_partition_categories_alternating_and_cap():
    """Test that participants are distributed alternating by seed into Category A and B, up to 8 each."""
    participants = [
        make_dummy_participant(f"p{i}", seed=i, name=f"Player {i}")
        for i in range(1, 19)  # 18 participants
    ]
    cat_a, cat_b = partition_categories(participants)

    assert len(cat_a) == 8
    assert len(cat_b) == 8
    # Category A should have seeds 1, 3, 5, 7, 9, 11, 13, 15
    assert [p.seed for p in cat_a] == [1, 3, 5, 7, 9, 11, 13, 15]
    assert all(p.category == "A" for p in cat_a)
    # Category B should have seeds 2, 4, 6, 8, 10, 12, 14, 16
    assert [p.seed for p in cat_b] == [2, 4, 6, 8, 10, 12, 14, 16]
    assert all(p.category == "B" for p in cat_b)


def test_partition_categories_house_ai_odd_filler():
    """Test that if a category has an odd participant count, House AI filler balances it."""
    # 5 participants: A gets 3 (seeds 1, 3, 5), B gets 2 (seeds 2, 4)
    participants = [
        make_dummy_participant(f"p{i}", seed=i, name=f"Player {i}")
        for i in range(1, 6)
    ]
    filler = make_dummy_participant("house-ai-baseline", seed=999, name="House AI Baseline", is_house_ai=True)

    cat_a, cat_b = partition_categories(participants, house_ai_filler=filler)

    # Cat A was odd (3), padded to 4 with House AI filler
    assert len(cat_a) == 4
    assert cat_a[-1].is_house_ai is True
    assert "house-ai-baseline" in cat_a[-1].participant_id

    # Cat B was even (2), no filler added
    assert len(cat_b) == 2
    assert all(not p.is_house_ai for p in cat_b)


def test_category_round_robin_pairings_reversed_colors():
    """Test that Berger circle generates complete round-robin pairings with two games per opponent."""
    participants = [
        make_dummy_participant(f"p{i}", seed=i, name=f"Player {i}")
        for i in range(1, 5)  # 4 participants -> 3 rounds
    ]

    all_pairings = []
    for r in range(1, 4):
        round_pairings = generate_category_round_robin_pairings(participants, round_number=r, category="A")
        assert len(round_pairings) == 2  # 4 participants / 2 = 2 pairings per round
        for p in round_pairings:
            assert p.category == "A"
            assert p.stage_name == "CATEGORY_A"
            assert p.is_bye is False
            assert p.participant1 is not None and p.participant2 is not None
        all_pairings.extend(round_pairings)

    # Verify each pair meets exactly once
    pairs = set()
    for p in all_pairings:
        key = frozenset([p.participant1.participant_id, p.participant2.participant_id])
        assert key not in pairs, f"Duplicate pairing detected: {key}"
        pairs.add(key)
    assert len(pairs) == 6  # 4 choose 2 = 6 total pairings


def test_standard_chess_scoring_and_draws():
    """Test that Draw awards 0.5 points to each side, Win awards 1.0, Loss awards 0.0."""
    p1 = make_dummy_participant("p1", seed=1, name="Player 1")
    p2 = make_dummy_participant("p2", seed=2, name="Player 2")

    matches = [
        {
            "match_id": str(uuid.uuid4()),
            "state": "COMPLETED",
            "white_participant_id": "p1",
            "black_participant_id": "p2",
            "result_outcome": "DRAW",
            "result_reason": "STALEMATE",
        },
        {
            "match_id": str(uuid.uuid4()),
            "state": "COMPLETED",
            "white_participant_id": "p2",
            "black_participant_id": "p1",
            "result_outcome": "WHITE_WIN",
            "result_reason": "CHECKMATE",
        },
    ]

    standings = compute_category_standings([p1, p2], matches, category="A")
    s_map = {s.participant_id: s for s in standings}

    # P2 won 1 (1.0 pt) and drew 1 (0.5 pt) = 1.5 pts
    assert s_map["p2"].points == 1.5
    assert s_map["p2"].wins == 1
    assert s_map["p2"].draws == 1
    assert s_map["p2"].losses == 0

    # P1 lost 1 (0.0 pt) and drew 1 (0.5 pt) = 0.5 pt
    assert s_map["p1"].points == 0.5
    assert s_map["p1"].wins == 0
    assert s_map["p1"].draws == 1
    assert s_map["p1"].losses == 1


def test_house_ai_ineligibility_and_promotion_rule():
    """Test that if House AI places in the top 3 of a category, rank 4 is promoted with promoted=True."""
    p1 = make_dummy_participant("p1", seed=1, name="Alice")
    house_ai = make_dummy_participant("house-ai-1", seed=2, name="House AI Baseline", is_house_ai=True)
    p2 = make_dummy_participant("p2", seed=3, name="Bob")
    p3 = make_dummy_participant("p3", seed=4, name="Charlie")  # Rank 4 should be promoted!
    p4 = make_dummy_participant("p4", seed=5, name="David")

    cat_a_standings = [
        ParticipantStanding(rank=1, participant_id="p1", name="Alice", points=4.0, is_house_ai=False),
        ParticipantStanding(rank=2, participant_id="house-ai-1", name="House AI", points=3.5, is_house_ai=True),
        ParticipantStanding(rank=3, participant_id="p2", name="Bob", points=3.0, is_house_ai=False),
        ParticipantStanding(rank=4, participant_id="p3", name="Charlie", points=2.5, is_house_ai=False),
        ParticipantStanding(rank=5, participant_id="p4", name="David", points=1.0, is_house_ai=False),
    ]

    b1 = make_dummy_participant("b1", seed=1, name="Bravo 1")
    b2 = make_dummy_participant("b2", seed=2, name="Bravo 2")
    b3 = make_dummy_participant("b3", seed=3, name="Bravo 3")
    cat_b_standings = [
        ParticipantStanding(rank=1, participant_id="b1", name="Bravo 1", points=4.0),
        ParticipantStanding(rank=2, participant_id="b2", name="Bravo 2", points=3.0),
        ParticipantStanding(rank=3, participant_id="b3", name="Bravo 3", points=2.0),
    ]

    p_dict = {
        p.participant_id: p
        for p in [p1, house_ai, p2, p3, p4, b1, b2, b3]
    }

    qual_a, qual_b = select_final_clash_qualifiers(cat_a_standings, cat_b_standings, p_dict)

    # Category A qualifiers must NOT contain House AI!
    qual_a_ids = [q.participant_id for q in qual_a]
    assert "house-ai-1" not in qual_a_ids
    # Top 3 eligible: Alice (p1), Bob (p2), and promoted Charlie (p3)
    assert qual_a_ids == ["p1", "p2", "p3"]

    # Charlie (rank 4) must be tagged promoted=True in standings
    charlie_standing = next(s for s in cat_a_standings if s.participant_id == "p3")
    assert charlie_standing.promoted is True

    # Category B qualifiers
    assert [q.participant_id for q in qual_b] == ["b1", "b2", "b3"]


def test_final_clash_bracket_generation_and_semifinal_byes():
    """Test that Final Clash bracket assigns Semifinal byes to Category A #1 and Category B #1."""
    a1 = make_dummy_participant("a1", seed=1, name="A-Seed1")
    a2 = make_dummy_participant("a2", seed=2, name="A-Seed2")
    a3 = make_dummy_participant("a3", seed=3, name="A-Seed3")

    b1 = make_dummy_participant("b1", seed=1, name="B-Seed1")
    b2 = make_dummy_participant("b2", seed=2, name="B-Seed2")
    b3 = make_dummy_participant("b3", seed=3, name="B-Seed3")

    bracket = generate_final_clash_bracket([a1, a2, a3], [b1, b2, b3])

    assert bracket.status == "IN_PROGRESS"
    assert len(bracket.quarterfinals) == 2
    assert len(bracket.semifinals) == 2
    assert len(bracket.finals) == 1

    # QF1: Category A #2 vs Category B #3
    qf1 = bracket.quarterfinals[0]
    assert qf1.participant1.participant_id == "a2"
    assert qf1.participant2.participant_id == "b3"

    # QF2: Category B #2 vs Category A #3
    qf2 = bracket.quarterfinals[1]
    assert qf2.participant1.participant_id == "b2"
    assert qf2.participant2.participant_id == "a3"

    # SF1 has Category A #1 (Seed A1 bye) waiting for QF2 winner
    sf1 = bracket.semifinals[0]
    assert sf1.participant1.participant_id == "a1"
    assert sf1.participant2 is None  # Waiting for QF2 winner

    # SF2 has Category B #1 (Seed B1 bye) waiting for QF1 winner
    sf2 = bracket.semifinals[1]
    assert sf2.participant1.participant_id == "b1"
    assert sf2.participant2 is None  # Waiting for QF1 winner


def test_partition_categories_zero_participants():
    """Test that partitioning an empty list returns empty categories."""
    cat_a, cat_b = partition_categories([])
    assert cat_a == []
    assert cat_b == []


def test_partition_categories_odd_in_both_categories():
    """Test partitioning when both categories have odd participant counts."""
    # 6 participants -> Cat A gets 3 (seeds 1, 3, 5), Cat B gets 3 (seeds 2, 4, 6)
    participants = [
        make_dummy_participant(f"p{i}", seed=i, name=f"Player {i}")
        for i in range(1, 7)
    ]
    filler_a = make_dummy_participant("house-a", seed=999, name="House A", is_house_ai=True)
    filler_b = make_dummy_participant("house-b", seed=999, name="House B", is_house_ai=True)

    # 1. With fillers: both odd pools become 4
    cat_a, cat_b = partition_categories(participants, house_ai_filler=filler_a, house_ai_filler_b=filler_b)
    assert len(cat_a) == 4
    assert len(cat_b) == 4
    assert cat_a[-1].is_house_ai is True
    assert cat_b[-1].is_house_ai is True

    # 2. Without fillers: both remain 3
    cat_a_nf, cat_b_nf = partition_categories(participants)
    assert len(cat_a_nf) == 3
    assert len(cat_b_nf) == 3


def test_partition_categories_even_in_both_categories():
    """Test partitioning when both categories have even participant counts."""
    # 8 participants -> Cat A gets 4, Cat B gets 4
    participants = [
        make_dummy_participant(f"p{i}", seed=i, name=f"Player {i}")
        for i in range(1, 9)
    ]
    filler_a = make_dummy_participant("house-a", seed=999, name="House A", is_house_ai=True)
    filler_b = make_dummy_participant("house-b", seed=999, name="House B", is_house_ai=True)

    cat_a, cat_b = partition_categories(participants, house_ai_filler=filler_a, house_ai_filler_b=filler_b)
    assert len(cat_a) == 4
    assert len(cat_b) == 4
    assert all(not p.is_house_ai for p in cat_a)
    assert all(not p.is_house_ai for p in cat_b)


def test_round_robin_pairings_odd_count_assigns_byes():
    """Test that round-robin with odd participants assigns exactly one bye per round."""
    # 3 participants in Category A (no filler)
    participants = [
        make_dummy_participant(f"p{i}", seed=i, name=f"Player {i}")
        for i in range(1, 4)
    ]

    bye_recipients = []
    for r in range(1, 4):
        pairings = generate_category_round_robin_pairings(participants, round_number=r, category="A")
        # 3 participants padded to 4 internally for Berger circle -> 2 pairings per round
        assert len(pairings) == 2
        active = [p for p in pairings if not p.is_bye]
        byes = [p for p in pairings if p.is_bye]
        assert len(active) == 1
        assert len(byes) == 1
        bye_recipients.append(byes[0].participant1.participant_id)

    # All 3 participants must receive exactly 1 bye across the 3 rounds
    assert set(bye_recipients) == {"p1", "p2", "p3"}


def test_select_final_clash_qualifiers_zero_eligible():
    """Test qualifier selection when a category has zero eligible participants."""
    # Cat A has only House AI
    cat_a_standings = [
        ParticipantStanding(rank=1, participant_id="house-1", name="House 1", points=4.0, is_house_ai=True),
        ParticipantStanding(rank=2, participant_id="house-2", name="House 2", points=3.0, is_house_ai=True),
    ]
    # Cat B has 3 eligible
    cat_b_standings = [
        ParticipantStanding(rank=1, participant_id="b1", name="Bravo 1", points=4.0, is_house_ai=False),
        ParticipantStanding(rank=2, participant_id="b2", name="Bravo 2", points=3.0, is_house_ai=False),
        ParticipantStanding(rank=3, participant_id="b3", name="Bravo 3", points=2.0, is_house_ai=False),
    ]

    p_dict = {
        "house-1": make_dummy_participant("house-1", 1, "House 1", is_house_ai=True),
        "house-2": make_dummy_participant("house-2", 2, "House 2", is_house_ai=True),
        "b1": make_dummy_participant("b1", 1, "Bravo 1"),
        "b2": make_dummy_participant("b2", 2, "Bravo 2"),
        "b3": make_dummy_participant("b3", 3, "Bravo 3"),
    }

    qual_a, qual_b = select_final_clash_qualifiers(cat_a_standings, cat_b_standings, p_dict)
    # Cat A must be empty since zero eligible human participants exist
    assert qual_a == []
    # Cat B has all 3
    assert [q.participant_id for q in qual_b] == ["b1", "b2", "b3"]


def test_house_ai_outside_top_three_no_promotion():
    """Test that when House AI finishes outside top 3, no promotion flag is set."""
    p1 = make_dummy_participant("p1", 1, "Alice")
    p2 = make_dummy_participant("p2", 2, "Bob")
    p3 = make_dummy_participant("p3", 3, "Charlie")
    p4 = make_dummy_participant("p4", 4, "David")
    house = make_dummy_participant("house-1", 5, "House AI", is_house_ai=True)

    cat_a_standings = [
        ParticipantStanding(rank=1, participant_id="p1", name="Alice", points=4.0, is_house_ai=False),
        ParticipantStanding(rank=2, participant_id="p2", name="Bob", points=3.0, is_house_ai=False),
        ParticipantStanding(rank=3, participant_id="p3", name="Charlie", points=2.0, is_house_ai=False),
        ParticipantStanding(rank=4, participant_id="p4", name="David", points=1.0, is_house_ai=False),
        ParticipantStanding(rank=5, participant_id="house-1", name="House AI", points=0.0, is_house_ai=True),
    ]
    p_dict = {p.participant_id: p for p in [p1, p2, p3, p4, house]}

    qual_a, _ = select_final_clash_qualifiers(cat_a_standings, [], p_dict)
    assert [q.participant_id for q in qual_a] == ["p1", "p2", "p3"]
    # No participant should have promoted=True
    assert all(not s.promoted for s in cat_a_standings)

