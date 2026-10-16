"""Deterministic tournament bracket generation, Swiss-system pairing, scoring, tie-breakers, and playoff engine."""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Optional

from app.tournaments.adjudication import (
    AdjudicationCategory,
    adjudicate_match_record,
)
from app.tournaments.schemas import (
    ParticipantStanding,
    PlayoffBracketSchema,
    PlayoffPairingSchema,
    SwissRoundSchema,
    TournamentBracket,
    TournamentMatchPairing,
    TournamentParticipant,
    TournamentRound,
    TwoGamePairing,
)


class CutoffTieError(ValueError):
    """Raised when an unresolved tie at the qualification boundary prevents playoff initialization."""
    pass


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


# ============================================================================
# 1. STANDINGS, SCORING, AND APPROVED TIE-BREAK ENGINE
# ============================================================================

def compute_leaderboard_standings(
    participants: list[TournamentParticipant],
    matches: list[dict],
    byes_by_participant: Optional[dict[str, int]] = None,
    byes_points_by_participant: Optional[dict[str, float]] = None,
    points_per_bye: Optional[float] = None,
    cutoff_rank: Optional[int] = None,
    tie_break_order: str = "OFFICIAL",
) -> list[ParticipantStanding]:
    """Compute official tournament standings using the approved tie-breaking hierarchy:
    
    1. Total points: win = 1.0, draw = 0.5, loss = 0.0 (plus bye wins).
    2. Buchholz (Opponent strength: sum of all opponents' total qualifying points).
    3. Sonneborn-Berger score (sum of defeated opponents' points + 0.5 * drawn opponents' points).
    4. Head-to-head only where it resolves the tie.
    5. A predefined playoff for any unresolved tie affecting sixth place, with a deterministic fallback.
    """
    byes_map = byes_by_participant or {}
    byes_pts_map = byes_points_by_participant or {}

    stats: dict[str, dict] = {}
    for p in participants:
        initial_bye_pts = 0.0
        if p.participant_id in byes_pts_map:
            initial_bye_pts = float(byes_pts_map[p.participant_id])
        elif p.participant_id in byes_map:
            rate = points_per_bye if points_per_bye is not None else 2.0
            initial_bye_pts = float(byes_map[p.participant_id] * rate)

        stats[p.participant_id] = {
            "participant": p,
            "played": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "points": initial_bye_pts,
            "byes": byes_map.get(p.participant_id, 0),
            "opponents_faced": set(),
            "defeated_ids": set(),
            "drawn_ids": set(),
            "head_to_head": {},  # opponent_id -> points earned against them
        }

    seen_matches: set[str] = set()
    for m in matches:
        match_id = str(m.get("match_id", ""))
        if match_id:
            if match_id in seen_matches:
                continue
            seen_matches.add(match_id)

        if m.get("state") not in {"COMPLETED", "ABORTED"}:
            continue

        white_id = m.get("white_participant_id")
        black_id = m.get("black_participant_id")
        if white_id not in stats or black_id not in stats:
            continue

        # Adjudicate outcome explicitly handling normal finishes, participant faults, and infrastructure failures
        adj = adjudicate_match_record(m)
        if adj.is_infrastructure_failure:
            # Infrastructure failures (worker crash, sandbox fault, controller restart) are VOID;
            # neither participant is penalized or granted unearned points.
            continue

        stats[white_id]["played"] += 1
        stats[black_id]["played"] += 1
        stats[white_id]["opponents_faced"].add(black_id)
        stats[black_id]["opponents_faced"].add(white_id)

        outcome = adj.result_outcome or m.get("result_outcome")

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

    # 1. Compute Opponent Strength (Buchholz) & Sonneborn-Berger
    for pid, entry in stats.items():
        # Buchholz = sum of total points of all opponents faced
        buchholz_score = sum(stats[opp_id]["points"] for opp_id in entry["opponents_faced"])
        entry["opponent_strength"] = buchholz_score
        entry["buchholz"] = buchholz_score

        # Sonneborn-Berger = sum of points of defeated opponents + 0.5 * points of drawn opponents
        sb_score = sum(stats[opp_id]["points"] for opp_id in entry["defeated_ids"])
        sb_score += 0.5 * sum(stats[opp_id]["points"] for opp_id in entry["drawn_ids"])
        entry["sonneborn_berger"] = sb_score

    # 2. Compute Head-to-Head points within tied groups
    # Group by total points (for legacy fallback and grouping)
    points_groups: dict[float, list[str]] = {}
    for pid, data in stats.items():
        points_groups.setdefault(data["points"], []).append(pid)

    for pts, group in points_groups.items():
        if len(group) == 2:
            p1, p2 = group[0], group[1]
            stats[p1]["group_h2h_points"] = stats[p1]["head_to_head"].get(p2, 0.0)
            stats[p2]["group_h2h_points"] = stats[p2]["head_to_head"].get(p1, 0.0)
        elif len(group) > 2:
            group_set = set(group)
            all_pairs_played = True
            for p_a in group:
                for p_b in group:
                    if p_a != p_b and p_b not in stats[p_a]["opponents_faced"]:
                        all_pairs_played = False
                        break
                if not all_pairs_played:
                    break

            if all_pairs_played:
                for pid in group:
                    stats[pid]["group_h2h_points"] = sum(
                        stats[pid]["head_to_head"].get(other_id, 0.0)
                        for other_id in group_set
                        if other_id != pid
                    )
            else:
                for pid in group:
                    stats[pid]["group_h2h_points"] = 0.0
        else:
            stats[group[0]]["group_h2h_points"] = 0.0

    # 3. Compute "Head-to-head ONLY where it resolves the tie" (when tied on Points, Buchholz, SB)
    exact_tied_groups: dict[tuple[float, float, float], list[str]] = {}
    for pid, data in stats.items():
        key = (data["points"], round(data["opponent_strength"], 4), round(data["sonneborn_berger"], 4))
        exact_tied_groups.setdefault(key, []).append(pid)

    for key, group in exact_tied_groups.items():
        if len(group) == 2:
            p1, p2 = group[0], group[1]
            h1 = stats[p1]["head_to_head"].get(p2, 0.0)
            h2 = stats[p2]["head_to_head"].get(p1, 0.0)
            if h1 > h2:
                stats[p1]["h2h_resolves_score"] = 1.0
                stats[p2]["h2h_resolves_score"] = 0.0
            elif h2 > h1:
                stats[p1]["h2h_resolves_score"] = 0.0
                stats[p2]["h2h_resolves_score"] = 1.0
            else:
                # Tied or not played -> H2H does not resolve the tie
                stats[p1]["h2h_resolves_score"] = 0.0
                stats[p2]["h2h_resolves_score"] = 0.0
        elif len(group) > 2:
            group_set = set(group)
            all_played = all(
                p_b in stats[p_a]["opponents_faced"]
                for p_a in group for p_b in group if p_a != p_b
            )
            if all_played:
                scores = {
                    pid: sum(stats[pid]["head_to_head"].get(other, 0.0) for other in group_set if other != pid)
                    for pid in group
                }
                if len(set(scores.values())) > 1:
                    # Resolves at least partially
                    for pid in group:
                        stats[pid]["h2h_resolves_score"] = scores[pid]
                else:
                    for pid in group:
                        stats[pid]["h2h_resolves_score"] = 0.0
            else:
                for pid in group:
                    stats[pid]["h2h_resolves_score"] = 0.0
        else:
            stats[group[0]]["h2h_resolves_score"] = 0.0

    def sort_key(item: tuple[str, dict]):
        pid, data = item
        if tie_break_order == "OFFICIAL":
            # 1. Total Points
            # 2. Buchholz (Opponent Strength)
            # 3. Sonneborn-Berger
            # 4. Head-to-head only where it resolves the tie
            # 5. Deterministic fallback (Seed, Name)
            return (
                -data["points"],
                -data["opponent_strength"],
                -data["sonneborn_berger"],
                -data.get("h2h_resolves_score", 0.0),
                data["participant"].seed,
                data["participant"].name,
            )
        else:
            return (
                -data["points"],
                -data.get("group_h2h_points", 0.0),
                -data["opponent_strength"],
                data["participant"].seed,
                data["participant"].name,
            )

    sorted_entries = sorted(stats.items(), key=sort_key)

    standings: list[ParticipantStanding] = []
    for idx, (pid, data) in enumerate(sorted_entries):
        p: TournamentParticipant = data["participant"]

        # Preserve unresolved ties: if tied with predecessor on all approved criteria,
        # share the exact same rank!
        if idx > 0:
            prev_pid, prev_data = sorted_entries[idx - 1]
            if tie_break_order == "OFFICIAL":
                tied_prev = (
                    data["points"] == prev_data["points"]
                    and data["opponent_strength"] == prev_data["opponent_strength"]
                    and data["sonneborn_berger"] == prev_data["sonneborn_berger"]
                    and data.get("h2h_resolves_score", 0.0) == prev_data.get("h2h_resolves_score", 0.0)
                )
            else:
                tied_prev = (
                    data["points"] == prev_data["points"]
                    and data.get("group_h2h_points", 0.0) == prev_data.get("group_h2h_points", 0.0)
                    and data["opponent_strength"] == prev_data["opponent_strength"]
                )
            rank = standings[-1].rank if tied_prev else idx + 1
        else:
            rank = 1

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
                head_to_head_points=data.get("group_h2h_points", 0.0),
                opponent_strength=round(data["opponent_strength"], 2),
                buchholz=round(data["opponent_strength"], 2),
                sonneborn_berger=round(data["sonneborn_berger"], 2),
                tie_break_score=round(data["opponent_strength"], 2),
                byes=data["byes"],
                needs_playoff=False,
                status=status,
            )
        )

    # 4. Check for unresolved ties affecting:
    # A. Sixth Place Qualification Cutoff (rank 6 vs rank 7)
    total_participants = len(standings)
    if total_participants >= 6:
        # Check boundary around 6th place
        p6 = standings[5]
        for other in standings[6:]:
            is_tied_6 = (
                other.points == p6.points
                and other.opponent_strength == p6.opponent_strength
                and other.sonneborn_berger == p6.sonneborn_berger
                and stats[other.participant_id].get("h2h_resolves_score", 0.0) == stats[p6.participant_id].get("h2h_resolves_score", 0.0)
            )
            if is_tied_6:
                p6.needs_playoff = True
                other.needs_playoff = True

    # B. Optional or specific qualification cutoff (e.g. rank 8 vs rank 9 if cutoff_rank=8, or top 8)
    eff_cutoff = cutoff_rank if cutoff_rank is not None else (8 if total_participants >= 8 else (4 if total_participants >= 4 else 0))
    if eff_cutoff > 0 and total_participants > eff_cutoff:
        boundary_standing = standings[eff_cutoff - 1]
        for other in standings[eff_cutoff:]:
            if (
                other.points == boundary_standing.points
                and other.opponent_strength == boundary_standing.opponent_strength
                and other.sonneborn_berger == boundary_standing.sonneborn_berger
            ):
                boundary_standing.needs_playoff = True
                other.needs_playoff = True

    # C. Prize positions (Ranks 1, 2, 3)
    for prize_idx in range(min(3, total_participants - 1)):
        curr = standings[prize_idx]
        nxt = standings[prize_idx + 1]
        if (
            curr.points == nxt.points
            and curr.opponent_strength == nxt.opponent_strength
            and curr.sonneborn_berger == nxt.sonneborn_berger
            and stats[curr.participant_id].get("h2h_resolves_score", 0.0) == stats[nxt.participant_id].get("h2h_resolves_score", 0.0)
        ):
            curr.needs_playoff = True
            nxt.needs_playoff = True

    for s in standings:
        if s.rank == 1 and s.needs_playoff:
            s.status = "ACTIVE"

    return standings


# ============================================================================
# 2. DETERMINISTIC SWISS-SYSTEM PAIRING ALGORITHM (TWO GAMES PER PAIRING)
# ============================================================================

def generate_swiss_pairings(
    participants: list[TournamentParticipant],
    completed_matches: list[dict],
    round_number: int,
    existing_byes: Optional[dict[str, int]] = None,
) -> list[TwoGamePairing]:
    """Generate deterministic Swiss-system pairings for round_number.
    
    Rules enforced:
    - Avoids previously faced opponents across rounds.
    - Odd participant counts: exactly one participant receives a bye.
      - A participant cannot receive more than one bye in the tournament.
      - Lowest-ranked eligible participant who has not had a bye yet gets the bye.
    - Each pairing schedules TWO games:
      - Game 1: P1 White, P2 Black
      - Game 2: P2 White, P1 Black (colors reversed)
    - Deterministic order: identical inputs produce identical pairings.
    """
    if not participants:
        return []

    byes = dict(existing_byes or {})

    # Compute current standings to order participants by current points/score
    standings = compute_leaderboard_standings(participants, completed_matches, byes)
    participants_by_id = {p.participant_id: p for p in participants}

    # Build historical opponent set
    past_opponents: dict[str, set[str]] = {p.participant_id: set() for p in participants}
    for m in completed_matches:
        w_id = m.get("white_participant_id")
        b_id = m.get("black_participant_id")
        if w_id and b_id and w_id in past_opponents and b_id in past_opponents:
            past_opponents[w_id].add(b_id)
            past_opponents[b_id].add(w_id)

    ordered_pids = [s.participant_id for s in standings]
    pairings: list[TwoGamePairing] = []
    pairing_idx = 1

    # 1. Handle Odd Count Bye
    if len(ordered_pids) % 2 == 1:
        # Candidate for bye: lowest-ranked participant who hasn't had a bye
        bye_candidate = None
        for pid in reversed(ordered_pids):
            if byes.get(pid, 0) == 0:
                bye_candidate = pid
                break
        if bye_candidate is None:
            # Fallback if all had byes: lowest ranked
            bye_candidate = ordered_pids[-1]

        ordered_pids.remove(bye_candidate)
        p_bye = participants_by_id[bye_candidate]

        pairings.append(
            TwoGamePairing(
                pairing_id=f"R{round_number}-P{pairing_idx}-BYE",
                round_number=round_number,
                pairing_index=pairing_idx,
                stage_name="SWISS",
                participant1=p_bye,
                participant2=None,
                is_bye=True,
                state="COMPLETED",
                p1_points=2.0,  # Full 2-game set points
                p2_points=0.0,
                winner_id=p_bye.participant_id,
            )
        )
        pairing_idx += 1

    # 2. Pair Remaining Even Set of Participants
    # Recursive backtracking to match without repeat opponents
    def _backtrack_pair(pool: list[str]) -> Optional[list[tuple[str, str]]]:
        if not pool:
            return []

        p1_id = pool[0]
        # Candidates to pair with p1: try unplayed opponents first, sorted by standing
        candidates = [cid for cid in pool[1:] if cid not in past_opponents[p1_id]]
        for cand in candidates:
            remaining = [cid for cid in pool[1:] if cid != cand]
            sub_res = _backtrack_pair(remaining)
            if sub_res is not None:
                return [(p1_id, cand)] + sub_res

        # If strict unplayed matching is impossible (e.g. late rounds, small pool),
        # allow repeat opponent with minimum occurrences as fallback
        for cand in pool[1:]:
            remaining = [cid for cid in pool[1:] if cid != cand]
            sub_res = _backtrack_pair(remaining)
            if sub_res is not None:
                return [(p1_id, cand)] + sub_res

        return None

    matched_pairs = _backtrack_pair(ordered_pids) or []

    for p1_id, p2_id in matched_pairs:
        p1 = participants_by_id[p1_id]
        p2 = participants_by_id[p2_id]
        pairings.append(
            TwoGamePairing(
                pairing_id=f"R{round_number}-P{pairing_idx}",
                round_number=round_number,
                pairing_index=pairing_idx,
                stage_name="SWISS",
                participant1=p1,
                participant2=p2,
                is_bye=False,
                state="QUEUED",
                p1_points=0.0,
                p2_points=0.0,
                winner_id=None,
            )
        )
        pairing_idx += 1

    return pairings


def generate_qualification_swiss_pairings(
    participants: list[TournamentParticipant],
    completed_matches: list[dict],
    round_number: int,
    existing_byes: Optional[dict[str, int]] = None,
) -> list[TwoGamePairing]:
    """Generate deterministic Swiss-system pairings for round_number (1 to 5)
    with White/Black color balancing, zero rematches, and score-bracket pairing.
    
    Each pairing represents 1 scheduled qualifying game:
      - participant1 is assigned White.
      - participant2 is assigned Black.
      - is_bye=False (or True if odd field).
    """
    if not participants:
        return []

    byes = dict(existing_byes or {})
    participants_by_id = {p.participant_id: p for p in participants}

    # 1. Compute current standings using official tie-break order
    standings = compute_leaderboard_standings(
        participants,
        completed_matches,
        byes_by_participant=byes,
        points_per_bye=1.0,
        tie_break_order="OFFICIAL",
    )

    # 2. Reconstruct opponent history and White/Black history
    past_opponents: dict[str, set[str]] = {p.participant_id: set() for p in participants}
    color_history: dict[str, list[str]] = {p.participant_id: [] for p in participants}

    # Sort matches chronologically to track exact color sequences (stable sort preserves input order)
    sorted_matches = [
        pair[1]
        for pair in sorted(
            enumerate(completed_matches),
            key=lambda pair: (pair[1].get("created_at") or datetime.min, pair[0])
        )
    ]

    for m in sorted_matches:
        if m.get("state") not in {"COMPLETED", "ABORTED"}:
            continue
        w_id = m.get("white_participant_id")
        b_id = m.get("black_participant_id")
        if w_id and b_id and w_id in past_opponents and b_id in past_opponents:
            past_opponents[w_id].add(b_id)
            past_opponents[b_id].add(w_id)
            color_history[w_id].append("W")
            color_history[b_id].append("B")

    ordered_pids = [s.participant_id for s in standings]
    pairings: list[TwoGamePairing] = []
    pairing_idx = 1

    # 3. Handle Odd Count Bye if necessary
    if len(ordered_pids) % 2 == 1:
        bye_candidate = None
        for pid in reversed(ordered_pids):
            if byes.get(pid, 0) == 0:
                bye_candidate = pid
                break
        if bye_candidate is None:
            bye_candidate = ordered_pids[-1]

        ordered_pids.remove(bye_candidate)
        p_bye = participants_by_id[bye_candidate]
        pairings.append(
            TwoGamePairing(
                pairing_id=f"QUAL-R{round_number}-P{pairing_idx}-BYE",
                round_number=round_number,
                pairing_index=pairing_idx,
                stage_name="SWISS",
                participant1=p_bye,
                participant2=None,
                is_bye=True,
                state="COMPLETED",
                p1_points=1.0,  # 1 game = 1.0 point
                p2_points=0.0,
                winner_id=p_bye.participant_id,
            )
        )
        pairing_idx += 1

    # 4. Color preference helper:
    # color_balance = count(W) - count(B)
    def color_pref(pid: str) -> tuple[int, Optional[str]]:
        hist = color_history.get(pid, [])
        w_cnt = hist.count("W")
        b_cnt = hist.count("B")
        bal = w_cnt - b_cnt
        last = hist[-1] if hist else None
        return bal, last

    def assign_colors(p_a_id: str, p_b_id: str, current_pairing_num: int) -> tuple[str, str]:
        """Determine (white_id, black_id) balancing color counts and alternation."""
        hist_a = color_history.get(p_a_id, [])
        hist_b = color_history.get(p_b_id, [])

        two_w_a = (len(hist_a) >= 2 and hist_a[-1] == "W" and hist_a[-2] == "W")
        two_w_b = (len(hist_b) >= 2 and hist_b[-1] == "W" and hist_b[-2] == "W")
        two_b_a = (len(hist_a) >= 2 and hist_a[-1] == "B" and hist_a[-2] == "B")
        two_b_b = (len(hist_b) >= 2 and hist_b[-1] == "B" and hist_b[-2] == "B")

        # 1. Absolute rule: prevent 3 consecutive games of the same color
        if two_w_a and not two_w_b:
            return (p_b_id, p_a_id)
        if two_w_b and not two_w_a:
            return (p_a_id, p_b_id)
        if two_b_a and not two_b_b:
            return (p_a_id, p_b_id)
        if two_b_b and not two_b_a:
            return (p_b_id, p_a_id)

        bal_a, last_a = color_pref(p_a_id)
        bal_b, last_b = color_pref(p_b_id)

        # 2. If one player has played more Whites than the other:
        if bal_a > bal_b:
            return (p_b_id, p_a_id)  # B gets White, A gets Black
        elif bal_b > bal_a:
            return (p_a_id, p_b_id)  # A gets White, B gets Black

        # 3. Equal balance: check alternation of last played color
        if last_a == "W" and last_b == "B":
            return (p_b_id, p_a_id)
        elif last_a == "B" and last_b == "W":
            return (p_a_id, p_b_id)

        # Equal balance and same last color (or Round 1):
        p_a = participants_by_id[p_a_id]
        p_b = participants_by_id[p_b_id]
        higher_seed = p_a if p_a.seed < p_b.seed else p_b
        lower_seed = p_b if p_a.seed < p_b.seed else p_a

        if round_number == 1:
            if current_pairing_num % 2 == 1:
                return (higher_seed.participant_id, lower_seed.participant_id)
            else:
                return (lower_seed.participant_id, higher_seed.participant_id)
        else:
            hist_high = color_history.get(higher_seed.participant_id, [])
            if len(hist_high) >= 2 and hist_high[-1] == "W" and hist_high[-2] == "W":
                return (lower_seed.participant_id, higher_seed.participant_id)
            if len(hist_high) >= 2 and hist_high[-1] == "B" and hist_high[-2] == "B":
                return (higher_seed.participant_id, lower_seed.participant_id)

            if (round_number + current_pairing_num) % 2 == 1:
                return (higher_seed.participant_id, lower_seed.participant_id)
            else:
                return (lower_seed.participant_id, higher_seed.participant_id)

    # 5. Deterministic backtracking matching to pair without repeats
    points_by_id = {s.participant_id: s.points for s in standings}

    def _pair_pool(pool: list[str]) -> Optional[list[tuple[str, str]]]:
        if not pool:
            return []
        p1 = pool[0]
        candidates = [c for c in pool[1:] if c not in past_opponents[p1]]
        bal1, _ = color_pref(p1)
        hist1 = color_history.get(p1, [])
        two_w_1 = (len(hist1) >= 2 and hist1[-1] == "W" and hist1[-2] == "W")
        two_b_1 = (len(hist1) >= 2 and hist1[-1] == "B" and hist1[-2] == "B")

        def cand_key(c_id: str):
            bal_c, _ = color_pref(c_id)
            score_diff = abs(points_by_id.get(p1, 0.0) - points_by_id.get(c_id, 0.0))
            color_compat = bal1 * bal_c
            hist_c = color_history.get(c_id, [])
            two_w_c = (len(hist_c) >= 2 and hist_c[-1] == "W" and hist_c[-2] == "W")
            two_b_c = (len(hist_c) >= 2 and hist_c[-1] == "B" and hist_c[-2] == "B")
            streak_conflict = 1 if ((two_w_1 and two_w_c) or (two_b_1 and two_b_c)) else 0
            return (streak_conflict, score_diff, color_compat)

        candidates.sort(key=cand_key)

        # 1. Search candidates with zero streak conflict first
        safe_candidates = [c for c in candidates if cand_key(c)[0] == 0]
        for cand in safe_candidates:
            rem = [c for c in pool[1:] if c != cand]
            sub = _pair_pool(rem)
            if sub is not None:
                return [(p1, cand)] + sub

        # 2. Fallback to any candidate without prior rematch
        for cand in candidates:
            if cand in safe_candidates:
                continue
            rem = [c for c in pool[1:] if c != cand]
            sub = _pair_pool(rem)
            if sub is not None:
                return [(p1, cand)] + sub

        # 3. Last-resort fallback if strict no-rematch impossible
        for cand in pool[1:]:
            rem = [c for c in pool[1:] if c != cand]
            sub = _pair_pool(rem)
            if sub is not None:
                return [(p1, cand)] + sub
        return None

    matched = _pair_pool(ordered_pids) or []

    for p_x, p_y in matched:
        white_id, black_id = assign_colors(p_x, p_y, pairing_idx)
        w_part = participants_by_id[white_id]
        b_part = participants_by_id[black_id]

        pairings.append(
            TwoGamePairing(
                pairing_id=f"QUAL-R{round_number}-P{pairing_idx}",
                round_number=round_number,
                pairing_index=pairing_idx,
                stage_name="SWISS",
                participant1=w_part,  # White
                participant2=b_part,  # Black
                is_bye=False,
                state="QUEUED",
                p1_points=0.0,
                p2_points=0.0,
                winner_id=None,
            )
        )
        pairing_idx += 1

    return pairings


# ============================================================================
# 3. SIX-PLAYER FINALS QUALIFICATION AND BRACKET ENGINE
# ============================================================================

def select_top_six_qualifiers(
    standings: list[ParticipantStanding],
    participants_by_id: dict[str, TournamentParticipant],
    allow_fallback: bool = False,
) -> list[TournamentParticipant]:
    """Select the top six qualifiers for the Finals bracket by qualifying score and tie-break order.
    
    If there is an unresolved tie affecting sixth place (i.e. rank 6 has needs_playoff == True):
    - If allow_fallback is False: raises CutoffTieError to mandate a predefined playoff.
    - If allow_fallback is True: applies the deterministic fallback (initial seed / name) to award 6th place.
    
    Qualifiers are assigned seeds 1 through 6 corresponding to their qualifying rank.
    """
    total = len(standings)
    if total < 6:
        raise ValueError(f"INSUFFICIENT_QUALIFIERS: Expected at least 6 participants, found {total}")

    p6 = standings[5]
    # `needs_playoff` is also set for prize-position and legacy 4/8 cutoff ties, so it
    # alone does not mean the 6th-place boundary is contested. A cutoff tie exists only
    # when someone ranked below 6th is tied with 6th (impossible with exactly six).
    cutoff_tied = p6.needs_playoff and any(
        other.needs_playoff
        and other.points == p6.points
        and other.opponent_strength == p6.opponent_strength
        and other.sonneborn_berger == p6.sonneborn_berger
        for other in standings[6:]
    )
    if cutoff_tied and not allow_fallback:
        raise CutoffTieError("CUTOFF_TIE_AFFECTING_SIXTH_PLACE")

    selected_standings = standings[:6]
    qualifiers: list[TournamentParticipant] = []
    for seed, s in enumerate(selected_standings, start=1):
        orig = participants_by_id.get(s.participant_id)
        if orig:
            q = orig.model_copy()
            q.seed = seed
            qualifiers.append(q)
    return qualifiers


def generate_six_player_finals_bracket(
    qualifiers: list[TournamentParticipant],
) -> PlayoffBracketSchema:
    """Generate the 6-player Finals bracket:
    
    Structure:
    - Qualifiers: Seeds 1 to 6.
    - Seeds 1 and 2 earn BYES in the Quarterfinals.
    - Quarterfinals:
      - QF1: Seed 4 vs Seed 5
      - QF2: Seed 3 vs Seed 6
    - Semifinals:
      - SF1: Seed 1 faces Winner(QF1 - 4 vs 5)
      - SF2: Seed 2 faces Winner(QF2 - 3 vs 6)
    - Grand Final:
      - Final: Winner(SF1) vs Winner(SF2) -> determines 1st and 2nd place.
    - Third-Place Playoff:
      - Third-Place: Loser(SF1) vs Loser(SF2) -> determines 3rd place!
    """
    if len(qualifiers) < 6:
        raise ValueError(f"SIX_PLAYER_FINALS_REQUIRES_SIX_QUALIFIERS: Got {len(qualifiers)}")

    by_seed = {p.seed: p for p in qualifiers}
    s1 = by_seed[1]
    s2 = by_seed[2]
    s3 = by_seed[3]
    s4 = by_seed[4]
    s5 = by_seed[5]
    s6 = by_seed[6]

    qfs = [
        PlayoffPairingSchema(
            pairing_id="FINALS-QF1",
            stage="QUARTERFINAL",
            pairing_index=1,
            participant1=s4,
            participant2=s5,
            state="QUEUED",
        ),
        PlayoffPairingSchema(
            pairing_id="FINALS-QF2",
            stage="QUARTERFINAL",
            pairing_index=2,
            participant1=s3,
            participant2=s6,
            state="QUEUED",
        ),
    ]

    sfs = [
        PlayoffPairingSchema(
            pairing_id="FINALS-SF1",
            stage="SEMIFINAL",
            pairing_index=1,
            participant1=s1,
            participant2=None,  # Winner of QF1 (4 vs 5)
            state="PENDING",
        ),
        PlayoffPairingSchema(
            pairing_id="FINALS-SF2",
            stage="SEMIFINAL",
            pairing_index=2,
            participant1=s2,
            participant2=None,  # Winner of QF2 (3 vs 6)
            state="PENDING",
        ),
    ]

    finals = [
        PlayoffPairingSchema(
            pairing_id="FINALS-GRAND-FINAL",
            stage="FINAL",
            pairing_index=1,
            participant1=None,
            participant2=None,
            state="PENDING",
        ),
    ]

    third_place = PlayoffPairingSchema(
        pairing_id="FINALS-THIRD-PLACE",
        stage="THIRD_PLACE_PLAYOFF",
        pairing_index=2,
        participant1=None,
        participant2=None,
        state="PENDING",
    )

    return PlayoffBracketSchema(
        tournament_id="official-finals-v1",
        status="IN_PROGRESS",
        quarterfinals=qfs,
        semifinals=sfs,
        finals=finals,
        third_place=third_place,
        byes=[s1, s2],
        champion=None,
        runner_up=None,
        third_place_winner=None,
        fourth_place=None,
        total_participants=6,
    )


def evaluate_pairing_outcome(
    game1_outcome: Optional[str],
    game2_outcome: Optional[str] = None,
    sudden_death_outcomes: Optional[list[str]] = None,
) -> tuple[float, float, Optional[str], bool]:
    """Evaluate aggregate outcome across 1 or 2 games with sudden-death tiebreakers if needed.
    
    Returns:
        (p1_points, p2_points, winner, needs_sudden_death)
        winner: 'P1', 'P2', or None
    """
    if game2_outcome is None:
        if not game1_outcome:
            return (0.0, 0.0, None, False)
        if game1_outcome == "WHITE_WIN":
            return (1.0, 0.0, "P1", False)
        if game1_outcome == "BLACK_WIN":
            return (0.0, 1.0, "P2", False)
        if game1_outcome == "DRAW":
            sd_list = sudden_death_outcomes or []
            for idx, sd_outcome in enumerate(sd_list, start=1):
                if idx % 2 == 1:
                    if sd_outcome == "WHITE_WIN":
                        return (0.5, 1.5, "P2", False)
                    elif sd_outcome == "BLACK_WIN":
                        return (1.5, 0.5, "P1", False)
                else:
                    if sd_outcome == "WHITE_WIN":
                        return (1.5, 0.5, "P1", False)
                    elif sd_outcome == "BLACK_WIN":
                        return (0.5, 1.5, "P2", False)
            return (0.5, 0.5, None, True)
    return evaluate_pairing_two_games(game1_outcome, game2_outcome, sudden_death_outcomes)


# ============================================================================
# 3. TOP-EIGHT QUALIFICATION CUTOFF ENGINE (LEGACY)
# ============================================================================

def select_top_eight_qualifiers(
    standings: list[ParticipantStanding],
    participants_by_id: dict[str, TournamentParticipant],
) -> list[TournamentParticipant]:
    """Select qualifiers for the playoff bracket using the approved rules:
    
    - When >= 8 participants exist: select exactly the Top 8.
    - When 4 <= N < 8 participants exist: select Top 4 (Semifinals).
    - When 2 <= N < 4 participants exist: select Top 2 (Finals).
    - If fewer than 2 participants exist: return empty (no mock participants invented).
    - Qualifiers are assigned playoff seeds 1 through K corresponding to their final Swiss rank.
    """
    total = len(standings)
    if total < 2:
        return []

    target_count = 8 if total >= 8 else (4 if total >= 4 else 2)
    selected_standings = standings[:target_count]

    qualifiers: list[TournamentParticipant] = []
    for seed, s in enumerate(selected_standings, start=1):
        orig = participants_by_id.get(s.participant_id)
        if orig:
            qualifiers.append(
                TournamentParticipant(
                    participant_id=orig.participant_id,
                    name=orig.name,
                    team_name=orig.team_name,
                    submission_version_id=orig.submission_version_id,
                    version=orig.version,
                    artifact_sha256=orig.artifact_sha256,
                    seed=seed,
                )
            )

    return qualifiers


# ============================================================================
# 4. PLAYOFF BRACKET & SUDDEN-DEATH ENGINE (TWO GAMES PER PAIRING)
# ============================================================================

def generate_playoff_bracket(
    qualifiers: list[TournamentParticipant],
) -> PlayoffBracketSchema:
    """Generate seeded playoff bracket for top eight (or 4/2) participants:
    
    Quarterfinals pairings (Standard 8-player bracket):
      - QF1: Seed 1 vs Seed 8
      - QF2: Seed 4 vs Seed 5
      - QF3: Seed 2 vs Seed 7
      - QF4: Seed 3 vs Seed 6
    
    Semifinals pairings:
      - SF1: Winner(QF1) vs Winner(QF2)
      - SF2: Winner(QF3) vs Winner(QF4)
    
    Finals pairing:
      - Final: Winner(SF1) vs Winner(SF2)
    
    Each match specifies 2 games with reversed colors.
    """
    n = len(qualifiers)
    by_seed = {p.seed: p for p in qualifiers}

    qfs: list[PlayoffPairingSchema] = []
    sfs: list[PlayoffPairingSchema] = []
    finals: list[PlayoffPairingSchema] = []

    if n >= 8:
        # Quarterfinals
        qf_matchups = [(1, 8), (4, 5), (2, 7), (3, 6)]
        for idx, (s1, s2) in enumerate(qf_matchups, start=1):
            qfs.append(
                PlayoffPairingSchema(
                    pairing_id=f"PLAYOFF-QF{idx}",
                    stage="QUARTERFINAL",
                    pairing_index=idx,
                    participant1=by_seed.get(s1),
                    participant2=by_seed.get(s2),
                    state="QUEUED",
                )
            )
        # Empty Semifinals
        sfs.append(PlayoffPairingSchema(pairing_id="PLAYOFF-SF1", stage="SEMIFINAL", pairing_index=1, state="PENDING"))
        sfs.append(PlayoffPairingSchema(pairing_id="PLAYOFF-SF2", stage="SEMIFINAL", pairing_index=2, state="PENDING"))
        # Empty Finals
        finals.append(PlayoffPairingSchema(pairing_id="PLAYOFF-FINAL", stage="FINAL", pairing_index=1, state="PENDING"))
    elif n >= 4:
        # Start at Semifinals: 1 vs 4, 2 vs 3
        sf_matchups = [(1, 4), (2, 3)]
        for idx, (s1, s2) in enumerate(sf_matchups, start=1):
            sfs.append(
                PlayoffPairingSchema(
                    pairing_id=f"PLAYOFF-SF{idx}",
                    stage="SEMIFINAL",
                    pairing_index=idx,
                    participant1=by_seed.get(s1),
                    participant2=by_seed.get(s2),
                    state="QUEUED",
                )
            )
        finals.append(PlayoffPairingSchema(pairing_id="PLAYOFF-FINAL", stage="FINAL", pairing_index=1, state="PENDING"))
    elif n >= 2:
        # Start at Finals: 1 vs 2
        finals.append(
            PlayoffPairingSchema(
                pairing_id="PLAYOFF-FINAL",
                stage="FINAL",
                pairing_index=1,
                participant1=by_seed.get(1),
                participant2=by_seed.get(2),
                state="QUEUED",
            )
        )

    return PlayoffBracketSchema(
        tournament_id="official-v1",
        status="IN_PROGRESS" if n >= 2 else "PENDING",
        quarterfinals=qfs,
        semifinals=sfs,
        finals=finals,
        champion=None,
        total_participants=n,
    )


def evaluate_pairing_two_games(
    game1_outcome: Optional[str],
    game2_outcome: Optional[str],
    sudden_death_outcomes: Optional[list[str]] = None,
) -> tuple[float, float, Optional[str], bool]:
    """Evaluate aggregate outcome across 2 games (G1: P1 White, P2 Black; G2: P2 White, P1 Black).
    
    Returns:
        (p1_points, p2_points, winner, needs_sudden_death)
        winner: 'P1', 'P2', or None
    """
    p1_pts = 0.0
    p2_pts = 0.0

    if not game1_outcome or not game2_outcome:
        return (0.0, 0.0, None, False)

    # Game 1: P1 is White, P2 is Black
    if game1_outcome == "WHITE_WIN":
        p1_pts += 1.0
    elif game1_outcome == "BLACK_WIN":
        p2_pts += 1.0
    elif game1_outcome == "DRAW":
        p1_pts += 0.5
        p2_pts += 0.5

    # Game 2: P2 is White, P1 is Black (reversed colors)
    if game2_outcome == "WHITE_WIN":
        p2_pts += 1.0
    elif game2_outcome == "BLACK_WIN":
        p1_pts += 1.0
    elif game2_outcome == "DRAW":
        p1_pts += 0.5
        p2_pts += 0.5

    # Decisive 2-game outcome
    if p1_pts >= 1.5:
        return (p1_pts, p2_pts, "P1", False)
    if p2_pts >= 1.5:
        return (p1_pts, p2_pts, "P2", False)

    # Tied 1.0 - 1.0: Check sudden death games
    sd_list = sudden_death_outcomes or []
    for idx, sd_outcome in enumerate(sd_list, start=1):
        # Odd sudden death (Game 3, 5): P2 is White, P1 is Black
        # Even sudden death (Game 4, 6): P1 is White, P2 is Black
        if idx % 2 == 1:
            if sd_outcome == "WHITE_WIN":
                return (p1_pts, p2_pts + 1.0, "P2", False)
            elif sd_outcome == "BLACK_WIN":
                return (p1_pts + 1.0, p2_pts, "P1", False)
        else:
            if sd_outcome == "WHITE_WIN":
                return (p1_pts + 1.0, p2_pts, "P1", False)
            elif sd_outcome == "BLACK_WIN":
                return (p1_pts, p2_pts + 1.0, "P2", False)

    # Still tied, sudden death game needed!
    return (p1_pts, p2_pts, None, True)


# ============================================================================
# 5. LEGACY / HELPER WRAPPERS (PRESERVED FOR BACKWARDS COMPATIBILITY)
# ============================================================================

def determine_match_winner(
    result_outcome: Optional[str],
    white_participant_id: str,
    black_participant_id: str,
    *,
    draw_tiebreak_preference: str = "WHITE",
) -> Optional[str]:
    """Deterministically determine the advancing winner of an individual match."""
    if result_outcome == "WHITE_WIN":
        return white_participant_id
    if result_outcome == "BLACK_WIN":
        return black_participant_id
    if result_outcome == "DRAW":
        return white_participant_id if draw_tiebreak_preference == "WHITE" else black_participant_id
    return None


def generate_single_elimination_bracket(
    participants: list[TournamentParticipant],
) -> TournamentBracket:
    """Generate a balanced, seeded single-elimination bracket for N participants."""
    if not participants:
        return TournamentBracket(total_participants=0, status="PENDING")

    sorted_participants = sorted(participants, key=lambda p: (p.seed, p.name))
    n = len(sorted_participants)
    bracket_size = 1 << (n - 1).bit_length()
    if bracket_size < 2:
        bracket_size = 2

    pairings: list[TournamentMatchPairing] = []
    round_name = _round_name(bracket_size)

    for i in range(bracket_size // 2):
        seed_high = i + 1
        seed_low = bracket_size - i
        white = next((p for p in sorted_participants if p.seed == seed_high), None)
        black = next((p for p in sorted_participants if p.seed == seed_low), None)

        if i % 2 == 1 and white and black:
            white, black = black, white

        pairing_id = f"R1-M{i + 1}"
        state = "QUEUED"
        winner_id = None
        result_outcome = None
        result_reason = None

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


# ============================================================================
# 5. CATEGORY A & B ROUND-ROBIN AND FINAL CLASH ENGINE
# ============================================================================

def partition_categories(
    participants: list[TournamentParticipant],
    house_ai_filler: Optional[TournamentParticipant] = None,
    house_ai_filler_b: Optional[TournamentParticipant] = None,
) -> tuple[list[TournamentParticipant], list[TournamentParticipant]]:
    """Partition participants into Category A and Category B (up to 8 per category).
    
    Distribution:
    - Participants are sorted by seed (and name fallback).
    - Alternating distribution: Seeds 1, 3, 5, 7, ... -> Category A (up to 8).
    - Seeds 2, 4, 6, 8, ... -> Category B (up to 8).
    - If either category has an odd count and house_ai_filler is provided,
      the odd category is filled with the fixed House AI baseline to ensure even round-robin scheduling.
    - House AI is flagged with is_house_ai=True and is NOT prize-eligible.
    """
    sorted_p = sorted(participants, key=lambda p: (p.seed, p.name))
    cat_a: list[TournamentParticipant] = []
    cat_b: list[TournamentParticipant] = []

    for idx, p in enumerate(sorted_p):
        if idx % 2 == 0:
            if len(cat_a) < 8:
                p_copy = p.model_copy()
                p_copy.category = "A"
                cat_a.append(p_copy)
        else:
            if len(cat_b) < 8:
                p_copy = p.model_copy()
                p_copy.category = "B"
                cat_b.append(p_copy)

    # Balance odd categories with House AI filler if available
    if house_ai_filler:
        if len(cat_a) % 2 == 1 and len(cat_a) < 8:
            filler_a = house_ai_filler.model_copy()
            if not house_ai_filler_b and not filler_a.participant_id.endswith("-cat-a"):
                filler_a.participant_id = f"{house_ai_filler.participant_id}-cat-a"
            filler_a.name = f"{house_ai_filler.name} (Cat A Baseline)"
            filler_a.category = "A"
            filler_a.is_house_ai = True
            filler_a.seed = len(cat_a) + 1
            cat_a.append(filler_a)

        filler_for_b = house_ai_filler_b or house_ai_filler
        if len(cat_b) % 2 == 1 and len(cat_b) < 8:
            filler_b = filler_for_b.model_copy()
            if not house_ai_filler_b and not filler_b.participant_id.endswith("-cat-b"):
                filler_b.participant_id = f"{filler_for_b.participant_id}-cat-b"
            filler_b.name = f"{filler_for_b.name} (Cat B Baseline)"
            filler_b.category = "B"
            filler_b.is_house_ai = True
            filler_b.seed = len(cat_b) + 1
            cat_b.append(filler_b)

    return cat_a, cat_b


def generate_category_round_robin_pairings(
    participants: list[TournamentParticipant],
    round_number: int,
    category: str = "A",
) -> list[TwoGamePairing]:
    """Generate round-robin pairings for a category in round_number using the Berger circle algorithm.
    
    Rules enforced:
    - Every opponent pairing produces TWO games with reversed colors:
      - Game 1: P1 White, P2 Black
      - Game 2: P2 White, P1 Black
    - Total rounds for N participants (N even): N - 1 rounds.
    - Each round has N // 2 pairings.
    - If odd and no filler: lowest-seeded participant receives a bye (2.0 pts).
    - Deterministic: identical inputs produce identical pairings.
    """
    if not participants:
        return []

    pool = list(participants)
    n = len(pool)
    has_bye = (n % 2 == 1)
    if has_bye:
        # Virtual bye participant added at end
        pool.append(None)
        n += 1

    total_rounds = n - 1
    if round_number < 1 or round_number > total_rounds:
        raise ValueError(f"Round number {round_number} out of range [1, {total_rounds}]")

    # Berger circle rotation:
    # Fix pool[0], rotate pool[1:] clockwise by (round_number - 1)
    shift = (round_number - 1) % (n - 1)
    movable = pool[1:]
    rotated_movable = movable[-shift:] + movable[:-shift] if shift > 0 else movable
    current_pool = [pool[0]] + rotated_movable

    pairings: list[TwoGamePairing] = []
    pairing_idx = 1

    for i in range(n // 2):
        p1 = current_pool[i]
        p2 = current_pool[n - 1 - i]

        if p1 is None or p2 is None:
            # Bye pairing
            real_p = p1 or p2
            assert real_p is not None
            pairings.append(
                TwoGamePairing(
                    pairing_id=f"CAT-{category}-R{round_number}-P{pairing_idx}-BYE",
                    round_number=round_number,
                    pairing_index=pairing_idx,
                    stage_name=f"CATEGORY_{category}",
                    category=category,
                    participant1=real_p,
                    participant2=None,
                    is_bye=True,
                    state="COMPLETED",
                    p1_points=2.0,
                    p2_points=0.0,
                    winner_id=real_p.participant_id,
                )
            )
        else:
            pairings.append(
                TwoGamePairing(
                    pairing_id=f"CAT-{category}-R{round_number}-P{pairing_idx}",
                    round_number=round_number,
                    pairing_index=pairing_idx,
                    stage_name=f"CATEGORY_{category}",
                    category=category,
                    participant1=p1,
                    participant2=p2,
                    is_bye=False,
                    state="QUEUED",
                    p1_points=0.0,
                    p2_points=0.0,
                    winner_id=None,
                )
            )
        pairing_idx += 1

    return pairings


def compute_category_standings(
    participants: list[TournamentParticipant],
    completed_matches: list[dict],
    category: str,
    byes_by_participant: Optional[dict[str, int]] = None,
) -> list[ParticipantStanding]:
    """Compute official standings within Category A or B using standard chess scoring:
    
    1. Total points: win = 1.0, draw = 0.5, loss = 0.0 (Draws are draws!).
    2. Head-to-Head points between tied participants.
    3. Sonneborn-Berger score (defeated opponents' points + 0.5 * drawn opponents' points).
    4. Opponent Strength / Buchholz.
    5. Initial seed / name fallback.
    """
    standings = compute_leaderboard_standings(participants, completed_matches, byes_by_participant)
    p_map = {p.participant_id: p for p in participants}

    for s in standings:
        s.category = category
        p = p_map.get(s.participant_id)
        if p and p.is_house_ai:
            s.is_house_ai = True

    return standings


def select_final_clash_qualifiers(
    cat_a_standings: list[ParticipantStanding],
    cat_b_standings: list[ParticipantStanding],
    participants_by_id: dict[str, TournamentParticipant],
) -> tuple[list[TournamentParticipant], list[TournamentParticipant]]:
    """Select top 3 eligible participants from Category A and Category B to advance to the Final Clash.
    
    House AI Ineligibility Rule:
    - House AI is NOT prize-eligible.
    - If House AI qualifies in the top 3 of either category, promote the next eligible participant
      (rank 4, or next highest eligible) under the preannounced rule.
    - Promoted participants are flagged with `promoted=True` in standings.
    """
    def _select_top_three(standings: list[ParticipantStanding]) -> list[TournamentParticipant]:
        qualifiers: list[TournamentParticipant] = []
        for s in standings:
            if s.is_house_ai:
                continue  # House AI cannot qualify or receive prizes
            orig = participants_by_id.get(s.participant_id)
            if orig:
                q = orig.model_copy()
                q.seed = len(qualifiers) + 1
                qualifiers.append(q)
                if len(qualifiers) == 3:
                    break
        # Mark promoted participants in standings
        for rank_idx, s in enumerate(standings[:len(qualifiers) + 1]):
            if rank_idx >= 3 and any(q.participant_id == s.participant_id for q in qualifiers):
                s.promoted = True
        return qualifiers

    qualifiers_a = _select_top_three(cat_a_standings)
    qualifiers_b = _select_top_three(cat_b_standings)
    return qualifiers_a, qualifiers_b


def generate_final_clash_bracket(
    cat_a_qualifiers: list[TournamentParticipant],
    cat_b_qualifiers: list[TournamentParticipant],
) -> PlayoffBracketSchema:
    """Generate the Final Clash 6-player playoff bracket:
    
    Seeds:
    - Category A #1 (Seed A1) and Category B #1 (Seed B1) earn byes into the Semifinals.
    - Quarterfinals (2 matchups, 2 games each with colors reversed):
      - QF1: Category A #2 vs Category B #3
      - QF2: Category B #2 vs Category A #3
    - Semifinals (2 matchups, 2 games each with colors reversed):
      - SF1: Category A #1 vs Winner(QF2)
      - SF2: Category B #1 vs Winner(QF1)
    - Finals:
      - Final: Winner(SF1) vs Winner(SF2)
    """
    a1 = cat_a_qualifiers[0] if len(cat_a_qualifiers) > 0 else None
    a2 = cat_a_qualifiers[1] if len(cat_a_qualifiers) > 1 else None
    a3 = cat_a_qualifiers[2] if len(cat_a_qualifiers) > 2 else None

    b1 = cat_b_qualifiers[0] if len(cat_b_qualifiers) > 0 else None
    b2 = cat_b_qualifiers[1] if len(cat_b_qualifiers) > 1 else None
    b3 = cat_b_qualifiers[2] if len(cat_b_qualifiers) > 2 else None

    total_qualifiers = len(cat_a_qualifiers) + len(cat_b_qualifiers)

    qfs: list[PlayoffPairingSchema] = []
    sfs: list[PlayoffPairingSchema] = []
    finals: list[PlayoffPairingSchema] = []

    # If full 6 qualifiers:
    if a1 and a2 and a3 and b1 and b2 and b3:
        # QF1: Category A #2 vs Category B #3
        qfs.append(
            PlayoffPairingSchema(
                pairing_id="CLASH-QF1",
                stage="QUARTERFINAL",
                pairing_index=1,
                participant1=a2,
                participant2=b3,
                state="QUEUED",
            )
        )
        # QF2: Category B #2 vs Category A #3
        qfs.append(
            PlayoffPairingSchema(
                pairing_id="CLASH-QF2",
                stage="QUARTERFINAL",
                pairing_index=2,
                participant1=b2,
                participant2=a3,
                state="QUEUED",
            )
        )
        # SF1: A1 vs Winner(QF2)
        sfs.append(
            PlayoffPairingSchema(
                pairing_id="CLASH-SF1",
                stage="SEMIFINAL",
                pairing_index=1,
                participant1=a1,
                participant2=None,
                state="PENDING",
            )
        )
        # SF2: B1 vs Winner(QF1)
        sfs.append(
            PlayoffPairingSchema(
                pairing_id="CLASH-SF2",
                stage="SEMIFINAL",
                pairing_index=2,
                participant1=b1,
                participant2=None,
                state="PENDING",
            )
        )
        # Final
        finals.append(
            PlayoffPairingSchema(
                pairing_id="CLASH-FINAL",
                stage="FINAL",
                pairing_index=1,
                participant1=None,
                participant2=None,
                state="PENDING",
            )
        )
    elif a1 and b1:
        # Fewer than 6 qualifiers: start at Semifinals or Finals
        if a2 and b2:
            sfs.append(
                PlayoffPairingSchema(
                    pairing_id="CLASH-SF1", stage="SEMIFINAL", pairing_index=1,
                    participant1=a1, participant2=b2, state="QUEUED"
                )
            )
            sfs.append(
                PlayoffPairingSchema(
                    pairing_id="CLASH-SF2", stage="SEMIFINAL", pairing_index=2,
                    participant1=b1, participant2=a2, state="QUEUED"
                )
            )
            finals.append(
                PlayoffPairingSchema(pairing_id="CLASH-FINAL", stage="FINAL", pairing_index=1, state="PENDING")
            )
        else:
            finals.append(
                PlayoffPairingSchema(
                    pairing_id="CLASH-FINAL", stage="FINAL", pairing_index=1,
                    participant1=a1, participant2=b1, state="QUEUED"
                )
            )

    return PlayoffBracketSchema(
        tournament_id="official-clash-v1",
        status="IN_PROGRESS" if total_qualifiers >= 2 else "PENDING",
        quarterfinals=qfs,
        semifinals=sfs,
        finals=finals,
        total_participants=total_qualifiers,
    )

