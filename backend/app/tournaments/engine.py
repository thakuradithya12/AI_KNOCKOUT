"""Deterministic tournament bracket generation, scoring, tie-breakers, and standings engine."""

from __future__ import annotations

import math
from typing import Optional

from app.tournaments.schemas import (
    ParticipantStanding,
    TournamentBracket,
    TournamentMatchPairing,
    TournamentParticipant,
    TournamentRound,
)


def _round_name(participants_count: int) -> str:
    if participants_count <= 2:
        return "Finals"
    if participants_count <= 4:
        return "Semifinals"
    if participants_count <= 8:
        return "Quarterfinals"
    if participants_count <= 16:
        return "Round of 16"
    if participants_count <= 32:
        return "Round of 32"
    return f"Round of {participants_count}"


def generate_single_elimination_bracket(
    participants: list[TournamentParticipant],
) -> TournamentBracket:
    """Generate a balanced, seeded single-elimination bracket for N participants."""
    if not participants:
        return TournamentBracket(total_participants=0, status="PENDING")

    # Sort participants deterministically by seed
    sorted_participants = sorted(participants, key=lambda p: (p.seed, p.name))
    n = len(sorted_participants)

    # Next power of 2 for bracket layout
    bracket_size = 1 << (n - 1).bit_length()
    if bracket_size < 2:
        bracket_size = 2

    # Standard seeding pairs: 1 vs bracket_size, 2 vs bracket_size - 1, etc.
    # If a participant is missing (bye), black or white is None
    pairings: list[TournamentMatchPairing] = []
    round_name = _round_name(bracket_size)

    for i in range(bracket_size // 2):
        seed_high = i + 1
        seed_low = bracket_size - i
        white = next((p for p in sorted_participants if p.seed == seed_high), None)
        black = next((p for p in sorted_participants if p.seed == seed_low), None)

        # Alternating color balance
        if i % 2 == 1 and white and black:
            white, black = black, white

        pairing_id = f"R1-M{i + 1}"
        state = "QUEUED"
        winner_id = None
        result_outcome = None
        result_reason = None

        # Automatic bye resolution if only one participant exists
        if white is not None and black is None:
            state = "COMPLETED"
            winner_id = white.participant_id
            result_outcome = "WHITE_WIN"
            result_reason = "BYE"
        elif black is not None and white is None:
            state = "COMPLETED"
            winner_id = black.participant_id
            result_outcome = "BLACK_WIN"
            result_reason = "BYE"

        pairings.append(
            TournamentMatchPairing(
                pairing_id=pairing_id,
                round_index=1,
                round_name=round_name,
                white=white,
                black=black,
                state=state,
                result_outcome=result_outcome,
                result_reason=result_reason,
                winner_participant_id=winner_id,
            )
        )

    round_1 = TournamentRound(round_index=1, round_name=round_name, pairings=pairings)
    num_rounds = int(math.log2(bracket_size))
    rounds = [round_1]

    # Pre-populate empty future rounds
    current_size = bracket_size // 2
    for r in range(2, num_rounds + 1):
        current_size //= 2
        r_name = _round_name(current_size * 2)
        r_pairings = [
            TournamentMatchPairing(
                pairing_id=f"R{r}-M{m + 1}",
                round_index=r,
                round_name=r_name,
                state="PENDING",
            )
            for m in range(current_size)
        ]
        rounds.append(TournamentRound(round_index=r, round_name=r_name, pairings=r_pairings))

    return TournamentBracket(
        tournament_id="official-knockout-v1",
        name="AI Knockout Official Championship",
        status="IN_PROGRESS" if n >= 2 else "PENDING",
        rounds=rounds,
        total_participants=n,
    )


def compute_leaderboard_standings(
    participants: list[TournamentParticipant],
    matches: list[dict],
) -> list[ParticipantStanding]:
    """Compute official tournament standings using standard chess scoring and Sonneborn-Berger tie-breaks.

    Match format expected:
    {
        "match_id": str,
        "white_participant_id": str,
        "black_participant_id": str,
        "state": str,  # "COMPLETED", "ABORTED", etc.
        "result_outcome": Optional[str],  # "WHITE_WIN", "BLACK_WIN", "DRAW"
        "result_reason": Optional[str],
    }
    """
    stats: dict[str, dict] = {
        p.participant_id: {
            "participant": p,
            "played": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "points": 0.0,
            "defeated_ids": set(),
            "drawn_ids": set(),
            "head_to_head": {},  # opponent_id -> points earned against them
        }
        for p in participants
    }

    for m in matches:
        if m.get("state") not in {"COMPLETED", "ABORTED"}:
            continue
        white_id = m.get("white_participant_id")
        black_id = m.get("black_participant_id")
        outcome = m.get("result_outcome")

        if white_id not in stats or black_id not in stats:
            continue

        stats[white_id]["played"] += 1
        stats[black_id]["played"] += 1

        if outcome == "WHITE_WIN":
            stats[white_id]["wins"] += 1
            stats[white_id]["points"] += 1.0
            stats[white_id]["defeated_ids"].add(black_id)
            stats[white_id]["head_to_head"][black_id] = stats[white_id]["head_to_head"].get(black_id, 0.0) + 1.0
            stats[black_id]["losses"] += 1
            stats[black_id]["head_to_head"][white_id] = stats[black_id]["head_to_head"].get(white_id, 0.0) + 0.0
        elif outcome == "BLACK_WIN":
            stats[black_id]["wins"] += 1
            stats[black_id]["points"] += 1.0
            stats[black_id]["defeated_ids"].add(white_id)
            stats[black_id]["head_to_head"][white_id] = stats[black_id]["head_to_head"].get(white_id, 0.0) + 1.0
            stats[white_id]["losses"] += 1
            stats[white_id]["head_to_head"][black_id] = stats[white_id]["head_to_head"].get(black_id, 0.0) + 0.0
        elif outcome == "DRAW":
            stats[white_id]["draws"] += 1
            stats[black_id]["draws"] += 1
            stats[white_id]["points"] += 0.5
            stats[black_id]["points"] += 0.5
            stats[white_id]["drawn_ids"].add(black_id)
            stats[black_id]["drawn_ids"].add(white_id)
            stats[white_id]["head_to_head"][black_id] = stats[white_id]["head_to_head"].get(black_id, 0.0) + 0.5
            stats[black_id]["head_to_head"][white_id] = stats[black_id]["head_to_head"].get(white_id, 0.0) + 0.5

    # Compute Sonneborn-Berger scores
    for pid, entry in stats.items():
        sb_score = 0.0
        for defeated_id in entry["defeated_ids"]:
            sb_score += stats[defeated_id]["points"]
        for drawn_id in entry["drawn_ids"]:
            sb_score += 0.5 * stats[drawn_id]["points"]
        entry["sonneborn_berger"] = sb_score

    # Sorting key: Points (desc), Head-to-Head (if applicable), Wins (desc), Sonneborn-Berger (desc), Seed (asc)
    def sort_key(item):
        pid, data = item
        return (
            -data["points"],
            -data["wins"],
            -data["sonneborn_berger"],
            data["participant"].seed,
            data["participant"].name,
        )

    sorted_entries = sorted(stats.items(), key=sort_key)

    standings: list[ParticipantStanding] = []
    for rank, (pid, data) in enumerate(sorted_entries, start=1):
        p: TournamentParticipant = data["participant"]
        status = "ACTIVE"
        if rank == 1 and data["played"] > 0:
            status = "CHAMPION"
        elif rank == 2 and data["played"] > 0:
            status = "FINALIST"

        standings.append(
            ParticipantStanding(
                rank=rank,
                participant_id=p.participant_id,
                name=p.name,
                team_name=p.team_name,
                matches_played=data["played"],
                wins=data["wins"],
                draws=data["draws"],
                losses=data["losses"],
                points=data["points"],
                tie_break_score=round(data["sonneborn_berger"], 2),
                status=status,
            )
        )

    return standings


def determine_match_winner(
    result_outcome: Optional[str],
    white_participant_id: str,
    black_participant_id: str,
    *,
    draw_tiebreak_preference: str = "WHITE",  # Default tiebreak policy if draw
) -> Optional[str]:
    """Deterministically determine the advancing winner of a tournament match."""
    if result_outcome == "WHITE_WIN":
        return white_participant_id
    if result_outcome == "BLACK_WIN":
        return black_participant_id
    if result_outcome == "DRAW":
        return white_participant_id if draw_tiebreak_preference == "WHITE" else black_participant_id
    return None
