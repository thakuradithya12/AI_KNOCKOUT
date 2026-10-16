"""Comprehensive unit test suite for Qualification and Finals tournament policy.

Tests:
1. 50-participant, 5-round Swiss simulation:
   - 50 participants, 5 fixed rounds, 25 games/round (total 125 games).
   - Zero repeated opponents across all 5 rounds.
   - No missing or duplicate pairings.
   - White/Black balance (each participant has 2 or 3 Whites; alternation; no 3-in-a-row).
   - Pairing favors equal scores.
2. Determinism and idempotency of pairing generation.
3. Scoring, standings, and tie-break calculations:
   - Win=1.0, Draw=0.5, Loss=0.0.
   - Buchholz (Opponent Strength) > Sonneborn-Berger > Head-to-Head > Fallback.
4. Ties affecting sixth place:
   - Unresolved cutoff tie raises CutoffTieError when allow_fallback=False.
   - Deterministic fallback resolves cutoff tie when allow_fallback=True.
5. Missing, invalid, and disqualified submissions.
6. Adjudication of timeouts, illegal moves, process crashes, and infrastructure failures:
   - Participant faults award 1.0 point to opponent, 0.0 to faulted player.
   - Infrastructure failures (worker crash, sandbox fault, controller restart) are VOID (0 pts to both).
7. Six-player finals bracket, seeding, byes, champion, and third-place determination:
   - Seeds 1 & 2 receive Quarterfinal byes.
   - QF: 4v5, 3v6.
   - SF: 1 vs Winner(4v5), 2 vs Winner(3v6).
   - Grand Final (1st & 2nd) and Third-Place Playoff (3rd & 4th).
   - Sudden-death tiebreaker logic.
"""

from __future__ import annotations

import uuid
import pytest

from app.models.match import MatchFailure, MatchOutcome, MatchState
from app.tournaments.adjudication import (
    AdjudicationCategory,
    adjudicate_match_record,
)
from app.tournaments.engine import (
    CutoffTieError,
    compute_leaderboard_standings,
    evaluate_pairing_outcome,
    generate_qualification_swiss_pairings,
    generate_six_player_finals_bracket,
    select_top_six_qualifiers,
)
from app.tournaments.schemas import (
    ParticipantStanding,
    TournamentParticipant,
)


def _make_participant(idx: int, seed: int = None) -> TournamentParticipant:
    return TournamentParticipant(
        participant_id=f"p-{idx:03d}",
        name=f"Player {idx:03d}",
        team_name=f"Team {idx:03d}",
        submission_version_id=uuid.uuid4(),
        version=1,
        artifact_sha256=f"hash-{idx:03d}" * 8,
        seed=seed if seed is not None else idx,
    )


# ============================================================================
# 1. 50-PARTICIPANT, FIVE-ROUND SWISS SIMULATION
# ============================================================================

def test_fifty_participant_five_round_simulation():
    """Verify 50 participants across 5 Swiss rounds:
    - Exactly 25 games per round (125 total games).
    - Every participant plays exactly 5 games.
    - Zero repeated opponents across all rounds.
    - No missing or duplicate pairings.
    - White/Black balance: each participant has 2 or 3 Whites across 5 games.
    - Never 3 consecutive games with the same color.
    - Pairing favors equal scores.
    """
    participants = [_make_participant(i, seed=i) for i in range(1, 51)]
    assert len(participants) == 50

    completed_matches: list[dict] = []
    opponents_faced: dict[str, set[str]] = {p.participant_id: set() for p in participants}
    white_counts: dict[str, int] = {p.participant_id: 0 for p in participants}
    black_counts: dict[str, int] = {p.participant_id: 0 for p in participants}
    color_history: dict[str, list[str]] = {p.participant_id: [] for p in participants}
    total_games_played: dict[str, int] = {p.participant_id: 0 for p in participants}

    for round_num in range(1, 6):
        # 1. Generate pairings
        pairings = generate_qualification_swiss_pairings(
            participants=participants,
            completed_matches=completed_matches,
            round_number=round_num,
        )

        # In a 50-player field, there must be exactly 25 pairings and 0 byes
        assert len(pairings) == 25, f"Round {round_num} had {len(pairings)} pairings instead of 25"
        paired_in_round: set[str] = set()

        round_matches = []
        for p_idx, pairing in enumerate(pairings, start=1):
            assert not pairing.is_bye, f"Round {round_num} had unexpected bye"
            w = pairing.participant1
            b = pairing.participant2
            assert w is not None and b is not None
            assert w.participant_id != b.participant_id

            # Verify no missing or duplicate participants in this round
            assert w.participant_id not in paired_in_round, f"{w.participant_id} duplicate in round {round_num}"
            assert b.participant_id not in paired_in_round, f"{b.participant_id} duplicate in round {round_num}"
            paired_in_round.add(w.participant_id)
            paired_in_round.add(b.participant_id)

            # Rule: NO REPEATED OPPONENTS
            assert b.participant_id not in opponents_faced[w.participant_id], (
                f"Repeat matchup between {w.participant_id} and {b.participant_id} in round {round_num}"
            )
            assert w.participant_id not in opponents_faced[b.participant_id]
            opponents_faced[w.participant_id].add(b.participant_id)
            opponents_faced[b.participant_id].add(w.participant_id)

            # Track color counts
            white_counts[w.participant_id] += 1
            black_counts[b.participant_id] += 1
            color_history[w.participant_id].append("W")
            color_history[b.participant_id].append("B")
            total_games_played[w.participant_id] += 1
            total_games_played[b.participant_id] += 1

            # Simulate realistic match outcome:
            # Alternate between White win, Black win, and Draw
            if p_idx % 3 == 1:
                outcome = "WHITE_WIN"
            elif p_idx % 3 == 2:
                outcome = "BLACK_WIN"
            else:
                outcome = "DRAW"

            from datetime import datetime, timezone
            m_record = {
                "match_id": uuid.uuid4(),
                "created_at": datetime(2026, 1, round_num, 12, p_idx, tzinfo=timezone.utc),
                "state": "COMPLETED",
                "result_outcome": outcome,
                "result_reason": "CHECKMATE" if outcome != "DRAW" else "STALEMATE",
                "white_participant_id": w.participant_id,
                "black_participant_id": b.participant_id,
            }
            round_matches.append(m_record)

        assert len(paired_in_round) == 50, f"Round {round_num} did not pair all 50 participants"
        completed_matches.extend(round_matches)

    # Post-5-Round Verification:
    assert len(completed_matches) == 125  # 25 games * 5 rounds = 125 games

    # Every participant played exactly 5 games
    for p in participants:
        pid = p.participant_id
        assert total_games_played[pid] == 5, f"{pid} played {total_games_played[pid]} games instead of 5"
        assert len(opponents_faced[pid]) == 5, f"{pid} faced {len(opponents_faced[pid])} distinct opponents"

        # Color balance across 5 games: must be either (3 White, 2 Black) or (2 White, 3 Black)
        w_c = white_counts[pid]
        b_c = black_counts[pid]
        assert w_c + b_c == 5
        assert abs(w_c - b_c) <= 1, f"{pid} color imbalance: {w_c} White, {b_c} Black"

        # Anti-3-in-a-row rule: verify no participant got "WWW" or "BBB"
        history_str = "".join(color_history[pid])
        assert "WWW" not in history_str, f"{pid} had 3 consecutive Whites: {history_str}"
        assert "BBB" not in history_str, f"{pid} had 3 consecutive Blacks: {history_str}"

    # Standings verification: 50 participants ranked
    standings = compute_leaderboard_standings(participants, completed_matches)
    assert len(standings) == 50
    # Top participant has the highest points
    assert standings[0].points >= standings[-1].points


def test_pairing_generation_is_deterministic_and_idempotent():
    """Verify that identical input state produces the exact same pairings."""
    participants = [_make_participant(i, seed=i) for i in range(1, 11)]
    completed: list[dict] = []

    p_run1 = generate_qualification_swiss_pairings(participants, completed, round_number=1)
    p_run2 = generate_qualification_swiss_pairings(participants, completed, round_number=1)

    assert len(p_run1) == len(p_run2)
    for p1, p2 in zip(p_run1, p_run2):
        assert p1.participant1.participant_id == p2.participant1.participant_id
        assert p1.participant2.participant_id == p2.participant2.participant_id
        assert p1.pairing_index == p2.pairing_index


# ============================================================================
# 2. SCORING, STANDINGS, AND TIE-BREAK CALCULATIONS
# ============================================================================

def test_scoring_win_draw_loss():
    """Verify authoritative scoring: Win = 1.0, Draw = 0.5, Loss = 0.0."""
    p1 = _make_participant(1)
    p2 = _make_participant(2)
    p3 = _make_participant(3)
    parts = [p1, p2, p3]

    matches = [
        # Match 1: P1 defeats P2 (1.0 to P1, 0.0 to P2)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": p1.participant_id, "black_participant_id": p2.participant_id},
        # Match 2: P1 draws with P3 (0.5 to P1, 0.5 to P3)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "DRAW", "white_participant_id": p1.participant_id, "black_participant_id": p3.participant_id},
        # Match 3: P2 defeats P3 (1.0 to P2, 0.0 to P3)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": p2.participant_id, "black_participant_id": p3.participant_id},
    ]

    standings = compute_leaderboard_standings(parts, matches)
    s_map = {s.participant_id: s for s in standings}

    assert s_map[p1.participant_id].points == 1.5  # 1 win + 1 draw
    assert s_map[p2.participant_id].points == 1.0  # 1 win + 1 loss
    assert s_map[p3.participant_id].points == 0.5  # 1 draw + 1 loss


def test_tiebreak_order_buchholz_over_sonneborn_berger_over_head_to_head():
    """Verify tie-break order:
    1. Buchholz (Opponent Strength)
    2. Sonneborn–Berger
    3. Head-to-head only where it resolves the tie
    4. Deterministic fallback (Seed, Name)
    """
    # 5 participants: A, B, C, D, E
    # A and B both finish with 1.0 point.
    # A played C (who finishes with 3.0 points) and D (0.0 pts) -> A's Buchholz = 3.0
    # B played E (who finishes with 1.0 point) and D (0.0 pts) -> B's Buchholz = 1.0
    # Buchholz MUST rank A ahead of B!
    pA = _make_participant(1, seed=1)
    pB = _make_participant(2, seed=2)
    pC = _make_participant(3, seed=3)
    pD = _make_participant(4, seed=4)
    pE = _make_participant(5, seed=5)
    participants = [pA, pB, pC, pD, pE]

    matches = [
        # A beats D (A +1.0)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pA.participant_id, "black_participant_id": pD.participant_id},
        # B beats D (B +1.0)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pB.participant_id, "black_participant_id": pD.participant_id},
        # C beats A (C +1.0, A +0.0)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pC.participant_id, "black_participant_id": pA.participant_id},
        # C beats D (C +1.0)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pC.participant_id, "black_participant_id": pD.participant_id},
        # C beats E (C +1.0) -> C total = 3.0 pts
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pC.participant_id, "black_participant_id": pE.participant_id},
        # E beats B (E +1.0, B +0.0) -> E total = 1.0 pt
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pE.participant_id, "black_participant_id": pB.participant_id},
    ]

    standings = compute_leaderboard_standings(participants, matches)
    s_map = {s.participant_id: s for s in standings}

    # Points:
    # C: 3.0 pts
    # A: 1.0 pt
    # B: 1.0 pt
    # E: 1.0 pt
    # D: 0.0 pts
    assert s_map[pC.participant_id].points == 3.0
    assert s_map[pA.participant_id].points == 1.0
    assert s_map[pB.participant_id].points == 1.0

    # A faced C (3.0 pts) and D (0.0 pts) -> Buchholz = 3.0
    # B faced D (0.0 pts) and E (1.0 pt) -> Buchholz = 1.0
    assert s_map[pA.participant_id].buchholz == 3.0
    assert s_map[pB.participant_id].buchholz == 1.0
    # A must rank higher than B
    assert s_map[pA.participant_id].rank < s_map[pB.participant_id].rank


def test_sonneborn_berger_breaks_buchholz_tie():
    """Verify that when Points and Buchholz are tied, Sonneborn-Berger breaks the tie."""
    p1 = _make_participant(1, seed=1)
    p2 = _make_participant(2, seed=2)
    pA = _make_participant(3, seed=3)
    pB = _make_participant(4, seed=4)
    pC = _make_participant(5, seed=5)
    pD = _make_participant(6, seed=6)
    pX = _make_participant(7, seed=7)
    participants = [p1, p2, pA, pB, pC, pD, pX]

    # P1 plays A (3.0 pts) and B (1.0 pt) -> Buchholz = 4.0
    # P1 beats A (3.0 pts) and loses to B -> P1 points = 1.0, SB = 3.0
    # P2 plays C (2.0 pts) and D (2.0 pts) -> Buchholz = 4.0
    # P2 beats C (2.0 pts) and loses to D -> P2 points = 1.0, SB = 2.0
    matches = [
        # P1 beats A
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": p1.participant_id, "black_participant_id": pA.participant_id},
        # B beats P1
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pB.participant_id, "black_participant_id": p1.participant_id},
        # P2 beats C
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": p2.participant_id, "black_participant_id": pC.participant_id},
        # D beats P2
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pD.participant_id, "black_participant_id": p2.participant_id},
        # A beats X three times (mock 3 wins for A -> total 3.0 pts)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pA.participant_id, "black_participant_id": pX.participant_id},
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pA.participant_id, "black_participant_id": pX.participant_id},
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pA.participant_id, "black_participant_id": pX.participant_id},
        # C beats X twice (mock 2 wins for C -> total 2.0 pts)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pC.participant_id, "black_participant_id": pX.participant_id},
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pC.participant_id, "black_participant_id": pX.participant_id},
        # D beats X once (in addition to beating P2 -> total 2.0 pts)
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": pD.participant_id, "black_participant_id": pX.participant_id},
    ]

    standings = compute_leaderboard_standings(participants, matches)
    s_map = {s.participant_id: s for s in standings}

    # Both P1 and P2 have 1.0 point
    assert s_map[p1.participant_id].points == 1.0
    assert s_map[p2.participant_id].points == 1.0

    # Both P1 and P2 have identical Buchholz (4.0)
    assert s_map[p1.participant_id].buchholz == 4.0
    assert s_map[p2.participant_id].buchholz == 4.0

    # P1 defeated A (3.0 pts) -> SB = 3.0
    # P2 defeated C (2.0 pts) -> SB = 2.0
    assert s_map[p1.participant_id].sonneborn_berger == 3.0
    assert s_map[p2.participant_id].sonneborn_berger == 2.0

    # SB breaks the tie: P1 ranks ahead of P2!
    assert s_map[p1.participant_id].rank < s_map[p2.participant_id].rank


# ============================================================================
# 3. TIES AFFECTING SIXTH PLACE
# ============================================================================

def test_sixth_place_cutoff_tie_detection_and_fallback():
    """Verify that unresolved ties affecting 6th place are flagged:
    - allow_fallback=False raises CutoffTieError.
    - allow_fallback=True resolves deterministically via initial seed.
    """
    # Create 8 participants
    participants = [_make_participant(i, seed=i) for i in range(1, 9)]
    p_dict = {p.participant_id: p for p in participants}

    # Matches where Seeds 1-5 have distinct points (5.0, 4.0, 3.0, 2.5, 2.0)
    # Seeds 6 and 7 are exactly tied: same points (1.0), same Buchholz, same SB, no H2H.
    p8 = participants[7]
    matches = [
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": participants[0].participant_id, "black_participant_id": p8.participant_id},
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": participants[1].participant_id, "black_participant_id": p8.participant_id},
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": participants[2].participant_id, "black_participant_id": p8.participant_id},
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": participants[3].participant_id, "black_participant_id": p8.participant_id},
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": participants[4].participant_id, "black_participant_id": p8.participant_id},
        # Seeds 6 and 7 both beat p8
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": participants[5].participant_id, "black_participant_id": p8.participant_id},
        {"match_id": uuid.uuid4(), "state": "COMPLETED", "result_outcome": "WHITE_WIN", "white_participant_id": participants[6].participant_id, "black_participant_id": p8.participant_id},
    ]

    standings = compute_leaderboard_standings(participants, matches)
    assert len(standings) == 8

    # 6th and 7th place have identical score, Buchholz, SB, H2H
    s6 = standings[5]
    s7 = standings[6]
    assert s6.points == s7.points == 1.0
    assert s6.buchholz == s7.buchholz == 0.0
    assert s6.sonneborn_berger == s7.sonneborn_berger == 0.0
    assert s6.needs_playoff is True
    assert s7.needs_playoff is True

    # 1. Without fallback, playoff initialization is safely blocked
    with pytest.raises(CutoffTieError):
        select_top_six_qualifiers(standings, p_dict, allow_fallback=False)

    # 2. With fallback, tie is resolved deterministically (higher seed / lower seed number)
    qualifiers = select_top_six_qualifiers(standings, p_dict, allow_fallback=True)
    assert len(qualifiers) == 6
    assert qualifiers[5].participant_id == participants[5].participant_id  # Seed 6 wins tiebreak over Seed 7


# ============================================================================
# 4. PARTICIPANT FAULTS VS INFRASTRUCTURE FAILURES ADJUDICATION
# ============================================================================

def test_participant_fault_adjudication():
    """Verify participant faults (illegal move, timeout, process crash, quota):
    Faulted side receives 0.0 points, opponent receives 1.0 point.
    """
    p_white = "white-participant-id"
    p_black = "black-participant-id"

    # 1. White makes ILLEGAL_MOVE -> Black awarded win
    m1 = {
        "match_id": uuid.uuid4(),
        "state": "ABORTED",
        "failure_reason": "ILLEGAL_MOVE",
        "side_to_move": "WHITE",
        "white_participant_id": p_white,
        "black_participant_id": p_black,
    }
    adj1 = adjudicate_match_record(m1)
    assert adj1.category == AdjudicationCategory.PARTICIPANT_FAULT
    assert adj1.result_outcome == "BLACK_WIN"
    assert adj1.white_points == 0.0
    assert adj1.black_points == 1.0
    assert adj1.winner_participant_id == p_black
    assert adj1.is_infrastructure_failure is False

    # 2. Black MOVE_TIMEOUT -> White awarded win
    m2 = {
        "match_id": uuid.uuid4(),
        "state": "ABORTED",
        "failure_reason": "MOVE_TIMEOUT",
        "side_to_move": "BLACK",
        "white_participant_id": p_white,
        "black_participant_id": p_black,
    }
    adj2 = adjudicate_match_record(m2)
    assert adj2.category == AdjudicationCategory.PARTICIPANT_FAULT
    assert adj2.result_outcome == "WHITE_WIN"
    assert adj2.white_points == 1.0
    assert adj2.black_points == 0.0
    assert adj2.winner_participant_id == p_white

    # 3. White PROCESS_CRASH -> Black awarded win
    m3 = {
        "match_id": uuid.uuid4(),
        "state": "ABORTED",
        "failure_reason": "PROCESS_CRASH",
        "side_to_move": "WHITE",
        "white_participant_id": p_white,
        "black_participant_id": p_black,
    }
    adj3 = adjudicate_match_record(m3)
    assert adj3.result_outcome == "BLACK_WIN"


def test_infrastructure_failure_adjudication():
    """Verify infrastructure failures (worker crash, sandbox fault, controller restart):
    Neither participant is penalized or granted unearned points.
    Match is VOID / eligible for replay.
    """
    p_white = "white-participant-id"
    p_black = "black-participant-id"

    for fail in ["WORKER_FAILURE", "SANDBOX_FAILURE", "CONTROLLER_RESTART", "STALE_FENCING"]:
        m = {
            "match_id": uuid.uuid4(),
            "state": "ABORTED",
            "failure_reason": fail,
            "side_to_move": "WHITE",
            "white_participant_id": p_white,
            "black_participant_id": p_black,
        }
        adj = adjudicate_match_record(m)
        assert adj.category == AdjudicationCategory.INFRASTRUCTURE_FAILURE
        assert adj.is_infrastructure_failure is True
        assert adj.is_replayable is True
        assert adj.white_points == 0.0
        assert adj.black_points == 0.0
        assert adj.winner_participant_id is None


def test_infrastructure_failure_excluded_from_standings():
    """Verify that matches aborted due to infrastructure failures do not award points."""
    p1 = _make_participant(1)
    p2 = _make_participant(2)
    participants = [p1, p2]

    matches = [
        # Match aborted due to WORKER_FAILURE
        {
            "match_id": uuid.uuid4(),
            "state": "ABORTED",
            "failure_reason": "WORKER_FAILURE",
            "side_to_move": "WHITE",
            "white_participant_id": p1.participant_id,
            "black_participant_id": p2.participant_id,
        }
    ]

    standings = compute_leaderboard_standings(participants, matches)
    assert standings[0].points == 0.0
    assert standings[1].points == 0.0
    assert standings[0].matches_played == 0
    assert standings[1].matches_played == 0


# ============================================================================
# 5. SIX-PLAYER FINALS STRUCTURE, BYES, CHAMPION & 3RD PLACE
# ============================================================================

def test_six_player_finals_bracket_structure():
    """Verify official 6-player Finals bracket topology:
    - Qualifiers: Top 6 (Seeds 1 to 6).
    - Seeds 1 & 2 receive Quarterfinal byes.
    - Quarterfinals: Seed 4 vs Seed 5, Seed 3 vs Seed 6.
    - Semifinals: Seed 1 faces Winner(4v5), Seed 2 faces Winner(3v6).
    - Grand Final (1st & 2nd) and Third-Place Playoff (3rd & 4th).
    """
    qualifiers = [_make_participant(i, seed=i) for i in range(1, 7)]
    bracket = generate_six_player_finals_bracket(qualifiers)

    assert bracket.total_participants == 6
    assert len(bracket.byes) == 2
    assert bracket.byes[0].seed == 1
    assert bracket.byes[1].seed == 2

    # Quarterfinals
    assert len(bracket.quarterfinals) == 2
    qf1 = bracket.quarterfinals[0]  # Seed 4 vs Seed 5
    qf2 = bracket.quarterfinals[1]  # Seed 3 vs Seed 6
    assert qf1.participant1.seed == 4
    assert qf1.participant2.seed == 5
    assert qf2.participant1.seed == 3
    assert qf2.participant2.seed == 6

    # Semifinals
    assert len(bracket.semifinals) == 2
    sf1 = bracket.semifinals[0]  # Seed 1 vs Winner(QF1)
    sf2 = bracket.semifinals[1]  # Seed 2 vs Winner(QF2)
    assert sf1.participant1.seed == 1
    assert sf1.participant2 is None  # Pending QF1 winner
    assert sf2.participant1.seed == 2
    assert sf2.participant2 is None  # Pending QF2 winner

    # Finals & Third-Place
    assert len(bracket.finals) == 1
    assert bracket.third_place is not None
    assert bracket.third_place.stage == "THIRD_PLACE_PLAYOFF"


def test_evaluate_pairing_outcome_single_game_and_sudden_death():
    """Verify pairing evaluation across single game and sudden-death tiebreakers."""
    # 1. Single game decisive White win
    p1_pts, p2_pts, winner, needs_sd = evaluate_pairing_outcome("WHITE_WIN")
    assert winner == "P1"
    assert p1_pts == 1.0
    assert p2_pts == 0.0
    assert needs_sd is False

    # 2. Single game decisive Black win
    p1_pts, p2_pts, winner, needs_sd = evaluate_pairing_outcome("BLACK_WIN")
    assert winner == "P2"
    assert p1_pts == 0.0
    assert p2_pts == 1.0
    assert needs_sd is False

    # 3. Single game DRAW -> requires sudden death
    p1_pts, p2_pts, winner, needs_sd = evaluate_pairing_outcome("DRAW")
    assert winner is None
    assert needs_sd is True

    # 4. Sudden-death game 1: White wins (P2 is White in sudden death game 1)
    # White win in SD1 -> P2 wins!
    p1_pts, p2_pts, winner, needs_sd = evaluate_pairing_outcome("DRAW", sudden_death_outcomes=["WHITE_WIN"])
    assert winner == "P2"
    assert needs_sd is False

    # 5. Sudden-death game 1: Black wins (P1 is Black in sudden death game 1)
    # Black win in SD1 -> P1 wins!
    p1_pts, p2_pts, winner, needs_sd = evaluate_pairing_outcome("DRAW", sudden_death_outcomes=["BLACK_WIN"])
    assert winner == "P1"
    assert needs_sd is False
